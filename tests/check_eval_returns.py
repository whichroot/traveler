"""Check declared return-width conversion against independent expected values."""
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile

tvc, llc, opt, linker = sys.argv[1:5]
source = '''
fn signed8(x: i8) -> i64 { return x; }
fn signed32(x: i32) -> i64 { return x; }
fn unsigned8(x: u8) -> i64 { return x; }
fn narrow(x: i64) -> i8 { return x; }
fn boolean(x: i32) -> bool { return x == 5; }
fn nested(x: i8) -> i64 { return signed8(x); }
fn indirect(f: fn(i32)->i64, x: i32) -> i64 { return f(x); }
fn main() {
    print(signed8(0 - 5));
    print(signed32(0 - 5));
    print(unsigned8(250));
    print(narrow(255));
    print(boolean(5) as i32);
    print(boolean(6) as i32);
    print(nested(0 - 128));
    print(indirect(&signed32, 0 - 7));
}
'''
expected = '-5\n-5\n250\n-1\n1\n0\n-128\n-7\n'


def run(*args):
    p = subprocess.run(list(map(str, args)), capture_output=True, text=True, timeout=60)
    assert p.returncode == 0, (args, p.returncode, p.stdout, p.stderr)
    return p.stdout


with tempfile.TemporaryDirectory(prefix='eval-returns-') as tmp:
    d = Path(tmp)
    tv, ir = d / 'returns.tv', d / 'returns.ll'
    tv.write_text(source.replace('    print(indirect(&signed32, 0 - 7));\n', ''))
    assert run(tvc, tv, '--eval') == expected.removesuffix('-7\n')
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
        assert run(d / 'job') == expected, profile
    print('return conversions PASS: signed/unsigned, narrowing, bool, nested calls; eval/raw/O1/O3; native indirect calls')
