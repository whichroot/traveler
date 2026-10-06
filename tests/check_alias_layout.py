"""Alias guards must use LLVM element strides for extension-field pointers."""
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile

tvc, llc, opt, linker = sys.argv[1:5]
source = '''
type F = Field<18446744069414584321>;
type E = ExtField<Field<18446744069414584321>,2>;
extern "C" fn dispatches() -> i32;
fn copy(out: *E, inp: *E, n: i32) {
    for i in 0..n { out[i] = inp[i]; }
}
fn main() {
    let p: *E = alloc(4096);
    let q: *E = alloc(4096);
    let words: *u64 = p as *u64;
    let reference: *u64 = alloc(8192);
    var i: i32 = 0;
    while i < 8192 { words[i] = i as u64; reference[i] = i as u64; i = i + 1; }
    copy(q, p, 2048);
    print(dispatches());
    i = 0;
    while i < 2048 {
        reference[2048 + 2*i] = reference[2*i];
        reference[2049 + 2*i] = reference[2*i+1];
        i = i + 1;
    }
    copy(&p[1024], p, 2048);
    print(dispatches());
    var bad: i32 = 0;
    i = 0;
    while i < 8192 { if words[i] != reference[i] { bad = bad + 1; } i = i + 1; }
    print(bad);
    free(reference); free(q); free(p);
}
'''


def run(*args, threads='1'):
    p = subprocess.run(list(map(str, args)), capture_output=True, text=True, timeout=60,
                       env=dict(os.environ, TRAVELER_THREADS=threads))
    assert p.returncode == 0, (args, p.returncode, p.stdout, p.stderr)
    return p.stdout


with tempfile.TemporaryDirectory(prefix='alias-layout-') as tmp:
    d = Path(tmp)
    tv, ir = d / 'layout.tv', d / 'layout.ll'
    tv.write_text(source)
    run(tvc, tv, '-o', ir)
    text = ir.read_text()
    call = '  call void @__parallel_for(ptr @__pfor_worker_0,'
    assert text.count(call) == 1
    text = text.replace(call, '  call void @note_dispatch()\n' + call)
    ir.write_text(text + '\ndeclare void @note_dispatch()\n')
    stub = d / 'counter.c'
    stub.write_text('static int count; void note_dispatch(void) { ++count; } int dispatches(void) { return count; }\n')
    flags = shlex.split(os.environ.get('TRAVELER_LINK_FLAGS', '-no-pie' if sys.platform == 'linux' else ''))
    for profile in ('raw', 'O1', 'O3'):
        selected = ir
        if profile != 'raw':
            selected = d / 'optimized.ll'
            run(opt, '-passes=default<' + profile + '>', '-S', ir, '-o', selected)
        run(llc, '-filetype=obj', selected, '-o', d / 'job.o')
        run(linker, *flags, d / 'job.o', stub, '-o', d / 'job')
        for threads in ('1', '8'):
            output = run(d / 'job', threads=threads)
            assert output == '1\n1\n0\n', (profile, threads, output)
    print('alias layout PASS: disjoint dispatch, overlapping extension-field serial fallback, sequential values; raw/O1/O3 T=1/8')
