"""Keep pointer-width payloads disjoint in enum constructors and matches."""
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile

tvc, llc, opt, linker = sys.argv[1:5]
source = '''
enum Pair { Functions(fn(i32)->i32, fn(i32)->i32) }
enum Mixed { Values(u8, fn(i32)->i32, u8) }
fn add1(x: i32) -> i32 { return x + 1; }
fn double(x: i32) -> i32 { return x * 2; }
fn make() -> Pair { return Pair::Functions(&add1, &double); }
fn main() {
    let pair: Pair = make();
    match pair { Pair::Functions(a, b) => { print(a(10)); print(b(10)); } }
    let mixed: Mixed = Mixed::Values(3, &double, 9);
    match mixed { Mixed::Values(a, f, b) => { print(a as i32); print(f(5)); print(b as i32); } }
}
'''


def run(*args):
    p = subprocess.run(list(map(str, args)), capture_output=True, text=True, timeout=60)
    assert p.returncode == 0, (args, p.returncode, p.stdout, p.stderr)
    return p.stdout


with tempfile.TemporaryDirectory(prefix='enum-fnptr-') as tmp:
    d = Path(tmp)
    tv, ir = d / 'payload.tv', d / 'payload.ll'
    tv.write_text(source)
    run(tvc, tv, '-o', ir)
    flags = shlex.split(os.environ.get('TRAVELER_LINK_FLAGS', '-no-pie' if sys.platform == 'linux' else ''))
    for profile in ('raw', 'O1', 'O3'):
        selected = ir
        if profile != 'raw':
            selected = d / 'optimized.ll'
            run(opt, '-passes=default<' + profile + '>', '-S', ir, '-o', selected)
        run(llc, '-filetype=obj', selected, '-o', d / 'job.o')
        run(linker, *flags, d / 'job.o', '-o', d / 'job')
        assert run(d / 'job') == '11\n20\n3\n10\n9\n', profile
    print('enum function-pointer payload PASS: adjacent pointers, mixed alignment, returned enum; raw/O1/O3')
