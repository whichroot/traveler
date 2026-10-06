"""Reject literals whose unsigned magnitude cannot fit a family bound."""
from pathlib import Path
import subprocess
import sys
import tempfile

tvc = sys.argv[1]
with tempfile.TemporaryDirectory(prefix='family-literals-') as tmp:
    d = Path(tmp)
    tv, ir = d / 'literal.tv', d / 'literal.ll'
    for family in ('Int<5>', 'Fix<5,4>', 'Int<9223372036854775807>', 'Fix<9223372036854775807,4>'):
        for literal in ('9223372036854775808', '-9223372036854775808', '18446744073709551615'):
            tv.write_text(f'fn main() {{ let x: {family} = {literal}; print(x); }}')
            for mode in (['-o', str(ir)], ['--eval']):
                ir.write_text('existing artifact\n')
                p = subprocess.run([tvc, str(tv), *mode], capture_output=True, text=True, timeout=60)
                assert p.returncode == 1 and 'literal magnitude exceeds family bound capacity' in p.stderr, (family, literal, mode, p)
                assert ir.read_text() == 'existing artifact\n'
    tv.write_text('''fn main() {
    let a: Int<9223372036854775807> = 9223372036854775807;
    let b: Fix<9223372036854775807,4> = -9223372036854775807;
    let c: Int<5> = -5;
    print(a); print(b); print(c);
}''')
    p = subprocess.run([tvc, str(tv), '--eval'], capture_output=True, text=True, timeout=60)
    assert p.returncode == 0 and p.stdout == '9223372036854775807\n-9223372036854775807\n-5\n', p
    print('family literals PASS: unsigned magnitude refusals, signed maximum boundaries, artifact preservation')
