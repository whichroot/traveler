"""Rebound pointer parameters cannot retain their original read-footprint identity."""
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys
import tempfile

tvc, llc, opt, linker = sys.argv[1:5]
cases = {
    'direct': 'a = b; return a[0];',
    'branch': 'if flag != 0 { a = b; } return a[0];',
    'while': 'var once: i32 = 0; while once < 1 { a = b; once = once + 1; } return a[0];',
    'for': 'for once in 0..1 { a = b; } return a[0];',
    'match': 'match flag { 1 => { a = b; } _ => {} } return a[0];',
    'wrapper': 'a = b; return a[0];',
    'forward': 'a = b; return load_value(a);',
    'loop-carried': 'var once: i32 = 0; var value: i32 = 0; while once < 2 { value = a[0]; a = b; once = once + 1; } return value;',
    'control': 'return a[0];',
    'scalar-control': 'flag = flag + 1; return a[0];',
}


def run(*args, threads='1'):
    p = subprocess.run(list(map(str, args)), capture_output=True, text=True, timeout=60,
                       env=dict(os.environ, TRAVELER_THREADS=threads))
    assert p.returncode == 0, (args, p.returncode, p.stdout, p.stderr)
    return p.stdout


with tempfile.TemporaryDirectory(prefix='callee-rebinding-') as tmp:
    d = Path(tmp)
    stub = d / 'counter.c'
    stub.write_text('static int count; void note_dispatch(void) { ++count; } int dispatches(void) { return count; }\n')
    flags = shlex.split(os.environ.get('TRAVELER_LINK_FLAGS', '-no-pie' if sys.platform == 'linux' else ''))
    for name, body in cases.items():
        control = name.endswith('control')
        tv, ir = d / 'rebind.tv', d / 'rebind.ll'
        callee = 'wrapper' if name == 'wrapper' else 'peek'
        tv.write_text(f'''
extern "C" fn dispatches() -> i32;
fn load_value(p: *i32) -> i32 {{ return p[0]; }}
fn peek(a: *i32, b: *i32, flag: i32) -> i32 {{ {body} }}
fn wrapper(a: *i32, b: *i32, flag: i32) -> i32 {{ return peek(a, b, flag); }}
fn main() {{
    let n: i32 = 4096;
    let x: *i32 = alloc(n); let out: *i32 = alloc(n);
    var j: i32 = 0;
    while j < n {{ x[j] = 3; out[j] = 0; j = j + 1; }}
    for i in 0..n {{ out[i] = {callee}(x, out, 1) + 1; }}
    print(dispatches());
    var bad: i32 = 0; j = 0;
    while j < n {{
        var want: i32 = {4 if control else 2};
        if j == 0 {{ want = {4 if control else 1}; }}
        if out[j] != want {{ bad = bad + 1; }}
        j = j + 1;
    }}
    print(bad); free(out); free(x);
}}
''')
        run(tvc, tv, '-o', ir)
        text, count = re.subn(r'  call void @__parallel_for\(ptr @__pfor_worker_\d+,',
                             lambda m: '  call void @note_dispatch()\n' + m[0], ir.read_text())
        assert count == int(control), (name, count)
        ir.write_text(text + '\ndeclare void @note_dispatch()\n')
        for profile in ('raw', 'O1', 'O3'):
            selected = ir
            if profile != 'raw':
                selected = d / 'optimized.ll'
                run(opt, '-passes=default<' + profile + '>', '-S', ir, '-o', selected)
            run(llc, '-filetype=obj', selected, '-o', d / 'job.o')
            run(linker, *flags, d / 'job.o', stub, '-o', d / 'job')
            for threads in ('1', '8'):
                output = run(d / 'job', threads=threads)
                assert output == f'{int(control)}\n0\n', (name, profile, threads, output)
print('callee rebinding PASS: direct/branch/while/for/match/transitive/forwarded/loop-carried refusals, stable pointer controls; raw/O1/O3 T=1/8')
