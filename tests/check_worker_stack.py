"""Worker-local storage must be allocated once per call, not per iteration."""
import os
from pathlib import Path
import re
import resource
import shlex
import subprocess
import sys
import tempfile

tvc, llc, opt, linker = sys.argv[1:5]


def limited_stack():
    _, hard = resource.getrlimit(resource.RLIMIT_STACK)
    resource.setrlimit(resource.RLIMIT_STACK, (min(1024 * 1024, hard) if hard != resource.RLIM_INFINITY else 1024 * 1024, hard))
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))


def run(*args, threads='1', bounded=False):
    p = subprocess.run(list(map(str, args)), capture_output=True, text=True, timeout=60,
                       env=dict(os.environ, TRAVELER_THREADS=threads),
                       preexec_fn=limited_stack if bounded else None)
    assert p.returncode == 0, (args, threads, p.returncode, p.stdout, p.stderr)
    return p.stdout


with tempfile.TemporaryDirectory(prefix='worker-stack-') as tmp:
    d = Path(tmp)
    flags = shlex.split(os.environ.get('TRAVELER_LINK_FLAGS', '-no-pie' if sys.platform == 'linux' else ''))
    for width in ('i32', 'i64'):
        source, ir = d / 'stack.tv', d / 'stack.ll'
        source.write_text(f'''
fn work(input: *i64, out: *i64, n: {width}) {{
    for i in 0..n {{
        var value: i64 = input[i];
        value = value + (i as i64);
        if value > 0 && value < 2000000 {{ out[i] = value; }}
    }}
}}
fn main() {{
    let n: {width} = 1048576;
    let input: *i64 = alloc(n); let out: *i64 = alloc(n);
    var j: {width} = 0;
    while j < n {{ input[j] = 3; out[j] = 0; j = j + 1; }}
    work(input, out, n);
    var bad: i32 = 0; j = 0;
    while j < n {{ if out[j] != (j as i64) + 3 {{ bad = bad + 1; }} j = j + 1; }}
    print(bad); free(out); free(input);
}}
''')
        run(tvc, source, '-o', ir)
        text = ir.read_text()
        workers = re.findall(r'define internal void @__pfor_worker_\d+\([^\n]*\) \{\n(.*?)\n\}', text, re.S)
        assert len(workers) == 1, (width, len(workers))
        misplaced = []
        block = ''
        for line in workers[0].splitlines():
            if line.endswith(':'):
                block = line[:-1]
            if ' = alloca ' in line and block != 'entry':
                misplaced.append((block, line))
        for profile in ('raw', 'O1', 'O3'):
            selected = ir
            if profile != 'raw':
                selected = d / 'optimized.ll'
                run(opt, '-passes=default<' + profile + '>', '-S', ir, '-o', selected)
            run(llc, '-O0' if profile == 'raw' else '-O2', '-filetype=obj', selected, '-o', d / 'job.o')
            run(linker, *flags, d / 'job.o', '-o', d / 'job')
            for threads in ('1', '8'):
                assert run(d / 'job', threads=threads, bounded=True) == '0\n', (width, profile, threads)
        assert not misplaced, (width, misplaced)
print('worker stack PASS: i32/i64 loops, per-iteration initialization, 1 MiB main stack; raw/O1/O3 T=1/8')
