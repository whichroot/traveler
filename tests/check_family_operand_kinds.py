"""A destination annotation cannot turn plain operands into family arithmetic."""
from pathlib import Path
import subprocess
import sys
import tempfile

tvc = sys.argv[1]
with tempfile.TemporaryDirectory(prefix='family-operand-kinds-') as tmp:
    d = Path(tmp)
    tv, ir = d / 'operands.tv', d / 'operands.ll'
    for family, expressions in (
        ('Fix<100,0>', ('n + m', 'n - m', 'n * m', 'n >> 0')),
        ('Fix<100,3>', ('n << 3',)),
        ('Exp<0>', ('n + m', 'n - m', 'n * m', 'n >> 0')),
        ('Exp<3>', ('n << 3',)),
    ):
        for expression in expressions:
            tv.write_text(f'fn main() {{ let n: i64 = 2; let m: i64 = 3; let x: {family} = {expression}; print(x); }}')
            for mode in (['-o', str(ir)], ['--eval']):
                ir.write_text('existing artifact\n')
                p = subprocess.run([tvc, str(tv), *mode], capture_output=True, text=True, timeout=60)
                assert p.returncode == 1 and 'arithmetic requires a' in p.stderr, (family, expression, mode, p)
                assert ir.read_text() == 'existing artifact\n'
    tv.write_text('''fn main() {
    let n: i64 = 2;
    let x: Fix<2,0> = n as Fix<2,0>;
    let y: Fix<4,0> = x + x;
    let a: Exp<0> = n as Exp<0>;
    let b: Exp<0> = a * n;
    print(y); print(b);
}''')
    p = subprocess.run([tvc, str(tv), '--eval'], capture_output=True, text=True, timeout=60)
    assert p.returncode == 0 and p.stdout == '4\n4\n', p
    print('family operand kinds PASS: ten plain-arithmetic refusals, explicit casts and family controls')
