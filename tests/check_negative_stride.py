"""Check reverse-index alias guards against sequential overlapping copies."""
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile

tvc, llc, opt, linker = sys.argv[1:5]


def run(*args, threads='1'):
    p = subprocess.run(list(map(str, args)), capture_output=True, text=True, timeout=60,
                       env=dict(os.environ, TRAVELER_THREADS=threads))
    assert p.returncode == 0, (args, p.returncode, p.stdout, p.stderr)
    return p.stdout


with tempfile.TemporaryDirectory(prefix='negative-stride-') as tmp:
    d = Path(tmp)
    flags = shlex.split(os.environ.get('TRAVELER_LINK_FLAGS', '-no-pie' if sys.platform == 'linux' else ''))
    for stride in (1, 2, 3):
        tv, ir = d / 'reverse.tv', d / 'reverse.ll'
        tv.write_text('''
fn copy(out: *i32, inp: *i32, n: i32) {
    for i in 0..n { out[i] = inp[TOP - i * STRIDE]; }
}
fn main() {
    let n: i32 = 1024;
    let inp: *i32 = alloc(4096);
    let out: *i32 = alloc(4096);
    let ref: *i32 = alloc(4096);
    var i: i32 = 0;
    while i < 4096 { inp[i] = i + 10; ref[i] = i + 10; i = i + 1; }
    copy(out, inp, n);
    var bad: i32 = 0;
    i = 0;
    while i < n {
        if out[i] != TOP - i * STRIDE + 10 { bad = bad + 1; }
        i = i + 1;
    }
    i = 0;
    while i < n { ref[i] = ref[TOP - i * STRIDE]; i = i + 1; }
    copy(inp, inp, n);
    i = 0;
    while i < n { if inp[i] != ref[i] { bad = bad + 1; } i = i + 1; }
    print(bad);
    free(ref); free(out); free(inp);
}
'''.replace('STRIDE', str(stride)).replace('TOP', str(1023 * stride)))
        run(tvc, tv, '-o', ir)
        text = ir.read_text()
        assert 'define internal void @__pfor_worker_0(' in text
        lower = -(9223372036854775807 // stride)
        upper = min(9223372036854775807, 9223372036854775808 // stride)
        assert f', {lower}\n' in text and f', {upper}\n' in text
        for profile in ('raw', 'O3'):
            selected = ir
            if profile == 'O3':
                selected = d / 'optimized.ll'
                run(opt, '-passes=default<O3>', '-S', ir, '-o', selected)
            run(llc, '-filetype=obj', selected, '-o', d / 'job.o')
            run(linker, *flags, d / 'job.o', '-o', d / 'job')
            for threads in ('1', '8'):
                assert run(d / 'job', threads=threads) == '0\n', (stride, profile, threads)
        print(f'negative stride -{stride}: bounds and disjoint/overlapping copies PASS')
