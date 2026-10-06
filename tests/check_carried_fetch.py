"""Keep same-iteration reads live while retaining trailing carried fetches."""
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys
import tempfile

tvc, llc, opt, linker = sys.argv[1:5]
cases = (
    ('early', '0', 'cur = x[i*4+v]; acc = acc + cur*w[v];', False),
    ('nested', '0', 'cur = x[i*4+v]; for u in 0..1 { acc = acc + cur*w[v]; }', False),
    ('local', '0', 'cur = x[i*4+v]; var t: i64 = cur; acc = acc + t*w[v];', False),
    ('trailing', 'x[i*4]', 'acc = acc + cur*w[v]; cur = x[i*4+v+1];', True),
)


def run(*args, threads='1'):
    p = subprocess.run(list(map(str, args)), capture_output=True, text=True, timeout=60,
                       env=dict(os.environ, TRAVELER_THREADS=threads))
    assert p.returncode == 0, (args, p.returncode, p.stdout, p.stderr)
    return p.stdout


with tempfile.TemporaryDirectory(prefix='carried-fetch-') as tmp:
    d = Path(tmp)
    flags = shlex.split(os.environ.get('TRAVELER_LINK_FLAGS', '-no-pie' if sys.platform == 'linux' else ''))
    for name, initial, body, guarded in cases:
        tv, ir = d / (name + '.tv'), d / (name + '.ll')
        tv.write_text('''
fn dot(x: *i64, w: *i64, out: *i64, n: i32) {
    for i in 0..n {
        var acc: i64 = 0;
        var cur: i64 = INITIAL;
        for v in 0..4 { BODY }
        out[i] = acc;
    }
}
fn main() {
    let x: *i64 = alloc(4097);
    let w: *i64 = alloc(4);
    let out: *i64 = alloc(1024);
    var k: i32 = 0;
    while k < 4097 { x[k] = (k + 1) as i64; k = k + 1; }
    w[0] = 1; w[1] = 10; w[2] = 100; w[3] = 1000;
    dot(x, w, out, 1024);
    var bad: i32 = 0;
    k = 0;
    while k < 1024 {
        if out[k] != (k as i64)*4444 + 4321 { bad = bad + 1; }
        k = k + 1;
    }
    print(bad);
    free(out); free(w); free(x);
}
'''.replace('INITIAL', initial).replace('BODY', body))
        assert run(tvc, tv, '--eval') == '0\n', name
        run(tvc, tv, '-o', ir)
        text = ir.read_text()
        assert 'define internal void @__pfor_worker_0(' in text, name
        assert bool(re.search(r'icmp slt i32 %\w+, 3\n', text)) == guarded, name
        for profile in ('raw', 'O1', 'O3'):
            selected = ir
            if profile != 'raw':
                selected = d / 'optimized.ll'
                run(opt, '-passes=default<' + profile + '>', '-S', ir, '-o', selected)
            run(llc, '-filetype=obj', selected, '-o', d / 'job.o')
            run(linker, *flags, d / 'job.o', '-o', d / 'job')
            for threads in ('1', '8'):
                assert run(d / 'job', threads=threads) == '0\n', (name, profile, threads)
        print(name + ': carried-fetch PASS (eval/raw/O1/O3, T=1/8)')
