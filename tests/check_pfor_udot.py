"""Resolve udot4 declarations before granting builtin purity."""
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile

tvc, llc, opt, linker = sys.argv[1:5]
cases = (
    ('mutating', 'var count: i32 = 0; fn udot4(a: i32, b: i32, c: i32) -> i32 { count = count + 1; return count; }', 0, 'i + 1'),
    ('extern', 'extern "C" fn udot4(a: i32, b: i32, c: i32) -> i32;', 0, 'i + 1'),
    ('pure', 'fn udot4(a: i32, b: i32, c: i32) -> i32 { return a; }', 1, 'i'),
    ('builtin', '', 1, '0'),
)


def run(*args, threads='1'):
    p = subprocess.run(list(map(str, args)), capture_output=True, text=True, timeout=60,
                       env=dict(os.environ, TRAVELER_THREADS=threads))
    assert p.returncode == 0, (args, p.returncode, p.stdout, p.stderr)
    return p.stdout


with tempfile.TemporaryDirectory(prefix='pfor-udot-') as tmp:
    d = Path(tmp)
    flags = shlex.split(os.environ.get('TRAVELER_LINK_FLAGS', '-no-pie' if sys.platform == 'linux' else ''))
    for name, declaration, dispatched, expected in cases:
        tv, ir = d / (name + '.tv'), d / (name + '.ll')
        tv.write_text(declaration + '''
fn fill(out: *i32, n: i32) { for i in 0..n { out[i] = udot4(i, 0, 0); } }
fn main() {
    let out: *i32 = alloc(4096);
    fill(out, 4096);
    var bad: i32 = 0;
    var i: i32 = 0;
    while i < 4096 { if out[i] != EXPECTED { bad = bad + 1; } i = i + 1; }
    print(bad);
    free(out);
}
'''.replace('EXPECTED', expected))
        reports = [json.loads(line) for line in run(tvc, tv, '--pfor-report').splitlines()]
        report = next(row for row in reports if row['fn'] == 'fill')
        assert report['dispatched'] == dispatched, (name, report)
        run(tvc, tv, '-o', ir)
        assert ('define internal void @__pfor_worker_0(' in ir.read_text()) == bool(dispatched), name
        extra = []
        if name == 'extern':
            stub = d / 'extern.c'
            stub.write_text('static int count; int udot4(int a, int b, int c) { (void)a; (void)b; (void)c; return ++count; }\n')
            extra = [stub]
        for profile in ('raw', 'O3'):
            selected = ir
            if profile == 'O3':
                selected = d / 'optimized.ll'
                run(opt, '-passes=default<O3>', '-S', ir, '-o', selected)
            run(llc, '-filetype=obj', selected, '-o', d / 'job.o')
            run(linker, *flags, d / 'job.o', *extra, '-o', d / 'job')
            for threads in ('1', '8'):
                assert run(d / 'job', threads=threads) == '0\n', (name, profile, threads)
        print(name + ': udot4 admission and execution PASS (raw/O3, T=1/8)')
