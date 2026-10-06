"""Bitwise complement cannot retain bounded or scaled family types."""
from pathlib import Path
import subprocess
import sys
import tempfile

tvc = sys.argv[1]
with tempfile.TemporaryDirectory(prefix='family-bitnot-') as tmp:
    d = Path(tmp)
    tv, ir = d / 'not.tv', d / 'not.ll'
    for family in ('Int<5>', 'Exp<4>', 'Fix<5,4>'):
        tv.write_text(f'fn main() {{ let x: {family} = 5; let y = ~x; print(y); }}')
        for mode in (['-o', str(ir)], ['--eval']):
            ir.write_text('existing artifact\n')
            p = subprocess.run([tvc, str(tv), *mode], capture_output=True, text=True, timeout=60)
            assert p.returncode == 1 and 'bitwise complement is not defined for family types' in p.stderr, (family, mode, p)
            assert ir.read_text() == 'existing artifact\n'
    tv.write_text('fn main() { let x: Int<5> = 5; print(~(x as i64)); let y: i32 = 7; print(~y); }')
    p = subprocess.run([tvc, str(tv), '--eval'], capture_output=True, text=True, timeout=60)
    assert p.returncode == 0 and p.stdout == '-6\n-8\n', p
    print('family complement PASS: Int/Exp/Fix refusals, explicit cast and plain integer controls')
