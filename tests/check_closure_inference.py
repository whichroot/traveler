"""Block-closure return inference follows inferred local initializer types."""
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile

tvc, llc, opt, linker = sys.argv[1:5]
SOURCE = '''
var calls:i32=0;
fn bump(x:i64)->i64 { calls=calls+1; return x+1; }
fn main() {
    let f=|x:i32| { let y=x+1; return y; }; print(f(3));
    let g=|x:i64| { let y=bump(x); let z=y*2; return z; };
    print(g(1099511627776)); print(calls);
    let h=|x:i32| { let y=x<5; return y; }; print(h(2)); print(h(8));
    let annotated=|x:i32| { let y:i64=x as i64; return y; }; print(annotated(7));
}
'''
EXPECTED = '4\n2199023255554\n1\n1\n0\n7\n'


def run(*args):
    p = subprocess.run(list(map(str, args)), capture_output=True, text=True, timeout=60)
    assert p.returncode == 0, (args, p.returncode, p.stdout, p.stderr)
    return p.stdout


with tempfile.TemporaryDirectory(prefix='closure-inference-') as tmp:
    d = Path(tmp)
    source, ir = d / 'case.tv', d / 'case.ll'
    source.write_text(SOURCE)
    run(tvc, source, '-o', ir)
    flags = shlex.split(os.environ.get('TRAVELER_LINK_FLAGS', '-no-pie' if sys.platform == 'linux' else ''))
    for profile in ('raw', 'O1', 'O3'):
        selected = ir
        if profile != 'raw':
            selected = d / 'opt.ll'
            run(opt, '-passes=default<' + profile + '>', '-S', ir, '-o', selected)
        run(llc, '-filetype=obj', selected, '-o', d / 'case.o')
        run(linker, *flags, d / 'case.o', '-o', d / 'case')
        actual = run(d / 'case')
        assert actual == EXPECTED, (profile, actual)
print('closure inference PASS: inferred locals/chains, high i64 bits, boolean, explicit local type and single execution; raw/O1/O3')
