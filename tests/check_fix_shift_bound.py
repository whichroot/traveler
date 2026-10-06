"""Arithmetic right shift needs a ceiling bound for negative Fix values."""
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile

tvc, llc, opt, linker = sys.argv[1:5]
cases = [(0, 0), (1, 1), (3, 1), (4, 1), (7, 2), (1, 62),
         (9223372036854775807, 1), (9223372036854775807, 62)]


def run(*args, success=True):
    p = subprocess.run(list(map(str, args)), capture_output=True, text=True, timeout=60)
    if success:
        assert p.returncode == 0, (args, p.returncode, p.stdout, p.stderr)
    return p


with tempfile.TemporaryDirectory(prefix='fix-shift-') as tmp:
    d = Path(tmp)
    tv, ir = d / 'shift.tv', d / 'shift.ll'
    statements, expected = [], ''
    for i, (bound, shift) in enumerate(cases):
        ceiling = (bound + (1 << shift) - 1) >> shift
        prefix = f'let x{i}: Fix<{bound},62> = -{bound};'
        if ceiling:
            tv.write_text(f'fn main() {{ {prefix} let y: Fix<{ceiling - 1},{62 - shift}> = x{i} >> {shift}; print(y); }}')
            for mode in (['-o', ir], ['--eval']):
                ir.write_text('existing artifact\n')
                p = run(tvc, tv, *mode, success=False)
                assert p.returncode == 1 and 'Fix bound of the value exceeds' in p.stderr, (bound, shift, p)
                assert ir.read_text() == 'existing artifact\n'
        statements.append(f'{prefix} let y{i}: Fix<{ceiling},{62 - shift}> = x{i} >> {shift}; print(y{i});')
        expected += str((-bound) >> shift) + '\n'
    tv.write_text('fn main() {\n' + '\n'.join(statements) + '\n}')
    assert run(tvc, tv, '--eval').stdout == expected
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
    print('Fix shift bound PASS: tight-bound refusals, odd/even/zero/I64_MAX bounds, shifts 0/1/2/62; eval/raw/O1/O3')
