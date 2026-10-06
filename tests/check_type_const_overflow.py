"""Type-level arithmetic must reject intermediate overflow instead of wrapping."""
from pathlib import Path
import subprocess
import sys
import tempfile

tvc = sys.argv[1]
expressions = [
    '4294967296*4294967296', '9223372036854775807+1',
    '(0-9223372036854775807-1)-1',
    '(0-9223372036854775807-1)*(0-1)',
    '(1<<63)', '(1<<64)', '(1>>(0-1))',
    '9223372036854775808', '18446744073709551617',
]
with tempfile.TemporaryDirectory(prefix='type-const-overflow-') as tmp:
    d = Path(tmp)
    tv, ir = d / 'constant.tv', d / 'constant.ll'
    for expr in expressions:
        for declaration in (f'let x: Int<{expr}> = 0;', f'let x: Fix<{expr},0> = 0;',
                            f'var x: [u8; {expr}] = 0;'):
            tv.write_text('fn main() { ' + declaration + ' }')
            for mode in (['-o', str(ir)], ['--eval']):
                ir.write_text('existing artifact\n')
                p = subprocess.run([tvc, str(tv), *mode], capture_output=True, text=True, timeout=60)
                assert p.returncode == 1 and any(s in p.stderr for s in (
                    'type-level integer overflow', 'type constant literal exceeds 64 bits',
                    'type-level shift amount must be in 0..63')), (declaration, mode, p)
                assert ir.read_text() == 'existing artifact\n'
    for argument, bound in [('4294967296', 'N*N'), ('9223372036854775808', 'N'),
                            ('18446744073709551617', 'N')]:
        tv.write_text(f'struct Box<const N> {{ value: Int<{bound}> }}\n'
                      f'fn main() {{ let p: *Box<{argument}> = alloc(1); p.value = 0; free(p); }}')
        ir.write_text('existing artifact\n')
        p = subprocess.run([tvc, str(tv), '-o', str(ir)], capture_output=True, text=True, timeout=60)
        assert p.returncode == 1 and 'type-level integer overflow' in p.stderr, (argument, bound, p)
        assert ir.read_text() == 'existing artifact\n'
    tv.write_text('''fn main() {
    let a: Int<9223372036854775806+1> = 9223372036854775807;
    let b: Int<((0-3)*(0-4))> = 12;
    let c: Int<(1<<62)> = 4611686018427387904;
    let d: Int<((0-9223372036854775807-1)+9223372036854775807)+1> = 0;
    print(a); print(b); print(c); print(d);
}''')
    p = subprocess.run([tvc, str(tv), '--eval'], capture_output=True, text=True, timeout=60)
    assert p.returncode == 0 and p.stdout == '9223372036854775807\n12\n4611686018427387904\n0\n', p
    print('type constants PASS: signed arithmetic/shift/digit overflow refusals, valid negative intermediates and boundaries')
