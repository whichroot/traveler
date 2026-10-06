"""Use allocation redzones to check aggregate alloc/realloc extents."""
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
struct Inner { a: i64, b: i64, c: i64 }
struct Nested { x: Inner, y: i32 }
struct Array { tag: u8, x: [Inner; 3], y: i64 }
struct Pointers { tag: u8, x: [*u8; 3], y: i64 }
struct Wide { tag: u8, x: u256, y: i64 }
struct Extension { tag: u8, x: E, y: i64 }
struct Polynomial { tag: u8, x: Poly<F,3>, y: i64 }
struct Registers { tag: u8, x: Register<F,4>, y: i64 }
fn main() {
    var p: *Nested = alloc(2);
    p[0].x = Inner { a: 11, b: 22, c: 33 };
    p[0].y = 44;
    p[1].y = 55;
    p = realloc(p, 4);
    p[3].y = 66;
    print(p[0].x.c + p[0].y + p[1].y + p[3].y);
    free(p);
    let a: *Array = alloc(2); a[1].y = 101; print(a[1].y); free(a);
    let b: *Pointers = alloc(2); b[1].y = 102; print(b[1].y); free(b);
    let c: *Wide = alloc(2); c[1].y = 103; print(c[1].y); free(c);
    let d: *Extension = alloc(2); d[1].y = 104; print(d[1].y); free(d);
    let e: *Polynomial = alloc(2); e[1].y = 105; print(e[1].y); free(e);
    let f: *Registers = alloc(2); f[1].y = 106; print(f[1].y); free(f);
}
'''
expected = '198\n101\n102\n103\n104\n105\n106\n'


def run(*args):
    p = subprocess.run(list(map(str, args)), capture_output=True, text=True, timeout=90)
    assert p.returncode == 0, (args, p.returncode, p.stdout, p.stderr)
    return p.stdout


with tempfile.TemporaryDirectory(prefix='aggregate-alloc-') as tmp:
    d = Path(tmp)
    tv, ir = d / 'aggregates.tv', d / 'aggregates.ll'
    tv.write_text(source)
    flags = shlex.split(os.environ.get('TRAVELER_LINK_FLAGS', '-no-pie' if sys.platform == 'linux' else ''))
    for debug in (False, True):
        run(tvc, tv, '-o', ir, *(['--alloc-debug'] if debug else []))
        for profile in ('raw', 'O1', 'O3'):
            selected = ir
            if profile != 'raw':
                selected = d / 'optimized.ll'
                run(opt, '-passes=default<' + profile + '>', '-S', ir, '-o', selected)
            run(llc, '-filetype=obj', selected, '-o', d / 'job.o')
            run(linker, *flags, d / 'job.o', '-o', d / 'job')
            assert run(d / 'job') == expected, (debug, profile)
    print('aggregate allocation PASS: nested, arrays, wide, extension, poly/register; alloc/realloc, redzones, raw/O1/O3')
