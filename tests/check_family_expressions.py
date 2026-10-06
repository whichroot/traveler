"""Family arithmetic dispatches on evaluated operands, with one evaluation each."""
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile

tvc, llc, opt, linker = sys.argv[1:5]
source = '''
struct Samples { a: Exp<16>, b: Exp<16> }
fn next(p: *i32) -> Exp<16> {
    p[0] = p[0] + 1;
    if p[0] == 1 { return 32768; }
    return 65536;
}
fn main() {
    let w: *Exp<16> = alloc(1); w[0] = 32768;
    let x: *Exp<16> = alloc(1); x[0] = 65536;
    let a: Exp<16> = requant(w[0] * x[0]); print(a);
    let s: Samples = Samples { a: 32768, b: 65536 };
    let b: Exp<16> = requant(s.a * s.b); print(b);
    let c: Exp<16> = requant((-s.a) * (-s.b)); print(c);
    let count: *i32 = alloc(1);
    let d: Exp<16> = requant(next(count) * next(count)); print(d); print(count[0]);
    free(count); free(w); free(x);
}
'''
expected = '32768\n32768\n32768\n32768\n2\n'
invalid = [
    ('struct S { a: Int<5>, b: Int<5> }\nfn main() { let s: S = S { a: 5, b: 5 }; let product = s.a * s.b; let small: Int<5> = product; }',
     'Int bound of the value exceeds'),
    ('struct S { a: Fix<5,4>, b: Fix<5,4> }\nfn main() { let s: S = S { a: 5, b: 5 }; let product = s.a * s.b; let small: Fix<5,4> = product; }',
     'Fix scale of the value differs'),
    ('fn main() { let a: Exp<16> = 1; let b: Exp<16> = 1; if (a+b) > (a*b) { print(1); } }',
     'scale mismatch in Exp comparison'),
    ('fn main() { let p: *Int<5> = alloc(2); let bad = p[0] & p[1]; free(p); }',
     'not defined on Int'),
]


def run(*args, success=True):
    p = subprocess.run(list(map(str, args)), capture_output=True, text=True, timeout=60)
    if success:
        assert p.returncode == 0, (args, p.returncode, p.stdout, p.stderr)
    return p


with tempfile.TemporaryDirectory(prefix='family-expressions-') as tmp:
    d = Path(tmp)
    tv, ir = d / 'expressions.tv', d / 'expressions.ll'
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
        output = run(d / 'job').stdout
        assert output == expected, (profile, output)
    for text, diagnostic in invalid:
        tv.write_text(text)
        for mode in (['-o', ir], ['--eval']):
            ir.write_text('existing artifact\n')
            p = run(tvc, tv, *mode, success=False)
            assert p.returncode == 1 and diagnostic in p.stderr, (text, p)
            assert ir.read_text() == 'existing artifact\n'
    print('family expressions PASS: indexed/member/unary/call operands, single evaluation, nested scale and bound refusals; raw/O1/O3')
