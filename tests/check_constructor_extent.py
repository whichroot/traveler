"""Poly/Register constructors own and initialize the full declared extent."""
import math
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys
import tempfile

tvc, llc, opt, linker = sys.argv[1:5]


def run(*args):
    p = subprocess.run(list(map(str, args)), capture_output=True, text=True, timeout=60)
    assert p.returncode == 0, (args, p.returncode, p.stdout, p.stderr)
    return p.stdout


with tempfile.TemporaryDirectory(prefix='constructor-extent-') as tmp:
    d = Path(tmp)
    source, ir = d / 'case.tv', d / 'case.ll'
    flags = shlex.split(os.environ.get('TRAVELER_LINK_FLAGS', '-no-pie' if sys.platform == 'linux' else ''))

    def check(text, expected, evaluator=True):
        source.write_text(text)
        run(tvc, source, '-o', ir)
        if evaluator:
            assert run(tvc, '--eval', source) == expected, text
        for profile in ('raw', 'O1', 'O3'):
            selected = ir
            if profile != 'raw':
                selected = d / 'opt.ll'
                run(opt, '-passes=default<' + profile + '>', '-S', ir, '-o', selected)
            run(llc, '-filetype=obj', selected, '-o', d / 'case.o')
            run(linker, *flags, d / 'case.o', '-o', d / 'case')
            actual = run(d / 'case')
            assert actual == expected, (text, profile, actual, expected)

    for prime in (251, 18446744069414584321):
        for degree, coeffs in ((0, []), (0, [7]), (3, []), (3, [1]), (3, [1, 2]), (3, [1, 2, 3, 4])):
            args = ','.join(map(str, coeffs))
            values = [sum(c * 2**k for k, c in enumerate(coeffs))]
            values += [sum(c * math.comb(t, k) for k, c in enumerate(coeffs) if k <= t) for t in range(5)]
            expected = ''.join(f'{v}\n' for v in values) * 2
            check(f'''
type F = Field<{prime}>;
fn probe() {{
    let p: Poly<F,{degree}> = poly({args}); print(eval(p,2));
    var r: Register<F,{degree}> = register({args});
    print(advance(r)); print(advance(r)); print(advance(r)); print(advance(r)); print(advance(r));
}}
fn main() {{ var j: i32 = 0; while j < 2 {{ probe(); j = j + 1; }} }}
''', expected, evaluator=prime < 2**64)
            if degree == 3 and len(coeffs) == 2:
                assert not re.search(r'= alloca \[2 x i\d+\]', ir.read_text()), 'undersized constructor'

    for args, expected in (('', '0\n0\n0\n0\n'), ('1,2', '1\n3\n5\n7\n'), ('1,2,3,4', '1\n3\n8\n20\n')):
        check(f'''
fn implicit<G: Field>() {{
    var r: Register<G,3> = register({args});
    print(advance(r)); print(advance(r)); print(advance(r)); print(advance(r));
}}
instantiate implicit<dyn>;
fn main() {{
    let f: Field = field(251);
    var r: Register<dyn,3> = register(f{',' + args if args else ''});
    print(advance(r)); print(advance(r)); print(advance(r)); print(advance(r));
    implicit_dyn(f);
}}
''', expected * 2)

    for declaration in (
        'let p: Poly<F,1> = poly(1,2,3);',
        'var r: Register<F,1> = register(1,2,3);',
        'let f: Field = field(251); var r: Register<dyn,1> = register(f,1,2,3);',
    ):
        source.write_text('type F = Field<251>; fn main() { ' + declaration + ' }')
        for mode in ([], ['--eval']):
            p = subprocess.run([tvc, *mode, str(source), *([] if mode else ['-o', str(ir)])],
                               capture_output=True, text=True, timeout=60)
            assert p.returncode != 0 and 'too many constructor coefficients' in p.stderr, p.stderr
print('constructor extent PASS: empty/short/full/degree-zero, 32/64-bit fields, dynamic explicit/implicit, excess refusals; eval and raw/O1/O3')
