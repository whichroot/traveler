"""Exp rescaling must round without signed overflow and stay within scale 62."""
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile

tvc, llc, opt, linker = sys.argv[1:5]


def run(*args, success=True):
    p = subprocess.run(list(map(str, args)), capture_output=True, text=True, timeout=60)
    if success:
        assert p.returncode == 0, (args, p.returncode, p.stdout, p.stderr)
    return p


with tempfile.TemporaryDirectory(prefix='exp-limits-') as tmp:
    d = Path(tmp)
    tv, ir = d / 'scale.tv', d / 'scale.ll'
    statements, expected = [], ''
    index = 0
    for shift in (1, 2, 3, 16, 31, 61, 62):
        half = 1 << (shift - 1)
        for value in (-(1 << 63), -(1 << 63) + 1, -half - 1, -half, -1, 0,
                      half - 1, half, (1 << 63) - half, (1 << 63) - 1):
            statements.append(f'let x{index}: Exp<62> = {value}; let y{index}: Exp<{62 - shift}> = requant(x{index}); print(y{index});')
            expected += str((value + half) >> shift) + '\n'
            index += 1
    tv.write_text('fn main() {\n' + '\n'.join(statements) + '\n}')
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
        assert output == expected, (profile, output, expected)
    for scale, shift in ((40, 40), (62, 1)):
        tv.write_text(f'fn main() {{ let x: Exp<{scale}> = 1; let y = x << {shift}; print(y); }}')
        for mode in (['-o', ir], ['--eval']):
            ir.write_text('existing artifact\n')
            p = run(tvc, tv, *mode, success=False)
            assert p.returncode == 1 and 'Exp scale ceiling (62)' in p.stderr, p
            assert ir.read_text() == 'existing artifact\n'
    tv.write_text('fn main() { let x: Exp<40> = 1; let y: Exp<62> = x << 22; print(y); }')
    assert run(tvc, tv, '--eval').stdout == '4194304\n'
    print('Exp limits PASS: 70 full-range rounding cases raw/O1/O3, scale ceiling refusals and boundary control')
