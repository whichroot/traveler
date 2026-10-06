"""Check nested array dimensions, element extraction, and generic substitution."""
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile

tvc, llc, opt, linker = sys.argv[1:5]
source = '''
struct Grid<T> { rows: [[T; 3]; 5], last: i64 }
fn stride<T>(p: *[T; 3]) -> i64 {
    return ((&p[1]) as i64) - (p as i64);
}
fn main() {
    var grid: [[u8; 3]; 5] = 0;
    let first: *u8 = (&grid[0]) as *u8;
    let last: *u8 = (&grid[4]) as *u8;
    first[2] = 11; last[2] = 23;
    print((last as i64) - (first as i64));
    print(first[2] as i32); print(last[2] as i32);
    let row: *[u8; 3] = &grid[0];
    print(stride(row));
    let p: *Grid<i64> = alloc(2);
    p[1].last = 71;
    print(p[1].last);
    print(((&p[1]) as i64) - (p as i64));
    free(p);
    var cube: [[[u16; 2]; 3]; 5] = 0;
    print(((&cube[4]) as i64) - ((&cube[0]) as i64));
}
'''
expected = '12\n11\n23\n3\n71\n128\n48\n'


def run(*args):
    p = subprocess.run(list(map(str, args)), capture_output=True, text=True, timeout=60)
    assert p.returncode == 0, (args, p.returncode, p.stdout, p.stderr)
    return p.stdout


with tempfile.TemporaryDirectory(prefix='nested-arrays-') as tmp:
    d = Path(tmp)
    tv, ir = d / 'arrays.tv', d / 'arrays.ll'
    tv.write_text(source)
    flags = shlex.split(os.environ.get('TRAVELER_LINK_FLAGS', '-no-pie' if sys.platform == 'linux' else ''))
    run(tvc, tv, '-o', ir)
    for profile in ('raw', 'O1', 'O3'):
        selected = ir
        if profile != 'raw':
            selected = d / 'optimized.ll'
            run(opt, '-passes=default<' + profile + '>', '-S', ir, '-o', selected)
        run(llc, '-filetype=obj', selected, '-o', d / 'job.o')
        run(linker, *flags, d / 'job.o', '-o', d / 'job')
        output = run(d / 'job')
        assert output == expected, (profile, output)
    tv.write_text('''
struct Grid<T> { rows: [[T; 3]; 5], last: i64 }
fn main() {
    var p: *Grid<i64> = alloc(2);
    p[1].last = 71;
    p = realloc(p, 3);
    print(p[1].last);
    print(((&p[1]) as i64) - (p as i64));
    free(p);
}
''')
    evaluated = run(tvc, tv, '--eval')
    assert evaluated == '71\n128\n', evaluated
    print('nested arrays PASS: unequal dimensions, three levels, generic fields and inference; raw/O1/O3, evaluator heap layout')
