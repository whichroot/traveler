"""Poison stack arrays before initialization to expose short zero fills."""
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys
import tempfile

tvc, llc, opt, linker = sys.argv[1:5]
source = '''
struct Pair { a: i64, b: i64 }
extern "C" fn all_zero(p: *u8, n: i64) -> i32;
fn main() {
    var pointers: [*u8; 16] = 0;
    var functions: [fn(i32)->i32; 7] = 0;
    var pairs: [Pair; 5] = 0;
    var integers: [i64; 9] = 0;
    print(all_zero((&pointers) as *u8, 128));
    print(all_zero((&functions) as *u8, 56));
    print(all_zero((&pairs) as *u8, 80));
    print(all_zero((&integers) as *u8, 72));
}
'''


def run(*args):
    p = subprocess.run(list(map(str, args)), capture_output=True, text=True, timeout=90)
    assert p.returncode == 0, (args, p.returncode, p.stdout, p.stderr)
    return p.stdout


with tempfile.TemporaryDirectory(prefix='array-zero-') as tmp:
    d = Path(tmp)
    tv, ir = d / 'arrays.tv', d / 'arrays.ll'
    tv.write_text(source)
    run(tvc, tv, '-o', ir)
    text = ir.read_text()
    main = text.index('define i32 @main(')
    count = [0]
    def poison(match):
        count[0] += 1
        size = {'16': 128, '7': 56, '5': 80, '9': 72}[match[2]]
        return match[0] + f'  call void @llvm.memset.p0.i64(ptr {match[1]}, i8 165, i64 {size}, i1 false)\n'
    body = re.sub(r'^  (%\w+) = alloca \[(\d+) x [^\n]+\n', poison, text[main:], flags=re.M)
    assert count[0] == 4, count
    ir.write_text(text[:main] + body)
    stub = d / 'scan.c'
    stub.write_text('int all_zero(const unsigned char *p, long long n) { for (long long i=0; i<n; ++i) if (p[i]) return 0; return 1; }\n')
    flags = shlex.split(os.environ.get('TRAVELER_LINK_FLAGS', '-no-pie' if sys.platform == 'linux' else ''))
    for profile in ('raw', 'O1', 'O3'):
        selected = ir
        if profile != 'raw':
            selected = d / 'optimized.ll'
            run(opt, '-passes=default<' + profile + '>', '-S', ir, '-o', selected)
        run(llc, '-filetype=obj', selected, '-o', d / 'job.o')
        run(linker, *flags, d / 'job.o', stub, '-o', d / 'job')
        assert run(d / 'job') == '1\n' * 4, profile
    print('array zero initialization PASS: poisoned pointer/function/struct/integer arrays; raw/O1/O3')
