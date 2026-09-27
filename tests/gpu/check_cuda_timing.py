"""Check non-waiting event elapsed queries and their binary32 ABI."""
from pathlib import Path
import subprocess
import sys
import tempfile

tvc, llc, opt, link = sys.argv[1:5]
here = Path(__file__).resolve().parent


def run(*args):
    subprocess.run([str(a) for a in args], check=True, timeout=90)


with tempfile.TemporaryDirectory() as directory:
    d = Path(directory)
    run(tvc, here / 'cuda_timing_gate.tv', '-o', d / 'raw.ll')
    for profile in ('raw', 'O1', 'O3'):
        ir = d / f'{profile}.ll'
        if profile != 'raw':
            run(opt, f'-passes=default<{profile}>', '-S', d / 'raw.ll', '-o', ir)
        run(llc, '-filetype=obj', ir, '-o', d / 'gate.o')
        run(link, '-no-pie', d / 'gate.o', here / 'cuda_driver_mock.c', '-o', d / 'gate')
        run(d / 'gate')
