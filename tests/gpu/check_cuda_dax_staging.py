"""Check a double-buffered DAX upload against the deferred-copy driver mock."""
from pathlib import Path
import os
import platform
import subprocess
import sys
import tempfile

TVC, LLC, OPT, LINK = sys.argv[1:5]
HERE = Path(__file__).resolve().parent


def run(*args, threads=1):
    p = subprocess.run(list(map(str, args)), capture_output=True, text=True,
                       env={**os.environ, 'TRAVELER_THREADS': str(threads)}, timeout=90)
    assert p.returncode == 0, (args, p.returncode, p.stdout, p.stderr)
    return p


with tempfile.TemporaryDirectory(prefix='traveler-dax-async-') as directory:
    temp = Path(directory)
    modes = ['none', 'o1'] if OPT else ['none']
    if OPT and platform.system() == 'Linux' and platform.machine() == 'x86_64':
        modes.append('o3')
    for mode in modes:
        cpu = ['-mcpu', 'x86-64'] if mode == 'o3' else []
        obj = temp/f'{mode}.o'
        run(TVC, HERE/'cuda_dax_staging.tv', '--emit', 'obj', '--opt-level', mode,
            '-opt', OPT, '-llc', LLC, *cpu, '-o', obj)
        flags = [] if platform.system() == 'Darwin' else ['-no-pie']
        run(LINK, *flags, '-Wall', '-Wextra', '-Werror', obj, HERE/'cuda_driver_mock.c', '-o', temp/mode)
        for threads in (1, 4):
            output = run(temp/mode, threads=threads).stdout.strip()
            assert 'DAX async PASS:' in output, output
    print(f'DAX async mock PASS: {"/".join(modes)}, threads 1/4; no CUDA hardware used')
