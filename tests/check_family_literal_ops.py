"""Family literals have independent bounds; multiplication literals are dimensionless."""
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile

tvc, llc, opt, linker = sys.argv[1:5]
source = '''
struct Sample { x: Fix<10,8> }
fn main() {
    let x: Fix<10,8> = 10;
    let a: Fix<30,8> = x * 3; print(a);
    let b: Fix<3000,8> = x * 300; print(b);
    let c: Fix<110,8> = x + 100; print(c);
    print((x < 100) as i32);
    let d: Fix<30,8> = -3 * x; print(d);
    let e: Exp<16> = 65536;
    let f: Exp<16> = e * 2; print(f);
    let g: Exp<16> = 2 * e; print(g);
    let s: Sample = Sample { x: 10 };
    let product = 300 * s.x;
    let h: Fix<3000,8> = product; print(h);
    let sum = s.x + 100;
    let i: Fix<110,8> = sum; print(i);
    let integer: Int<5> = 5;
    let j: Int<105> = integer + 100; print(j);
}
'''
expected = '30\n3000\n110\n1\n-30\n131072\n131072\n3000\n110\n105\n'


def run(*args, success=True):
    p = subprocess.run(list(map(str, args)), capture_output=True, text=True, timeout=60)
    if success:
        assert p.returncode == 0, (args, p.returncode, p.stdout, p.stderr)
    return p


with tempfile.TemporaryDirectory(prefix='family-literal-ops-') as tmp:
    d = Path(tmp)
    tv, ir = d / 'literal-ops.tv', d / 'literal-ops.ll'
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
        assert run(d / 'job').stdout == expected, profile
    assert run(tvc, tv, '--eval').stdout == expected
    for expression in ('x * 300', 'x + 100'):
        tv.write_text(f'fn main() {{ let x: Fix<10,8> = 10; let bad: Fix<10,8> = {expression}; }}')
        p = run(tvc, tv, '-o', ir, success=False)
        assert p.returncode == 1 and 'Fix bound of the value exceeds' in p.stderr, p
    print('family literal operators PASS: independent bounds, dimensionless +/- factors, either operand order, field inference; eval/raw/O1/O3')
