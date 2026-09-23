"""Check resident stream/event fork/join and completion ownership."""
from pathlib import Path
import subprocess
import sys
import tempfile

TVC, LLC, LINK = sys.argv[1:4]
CUDA = sys.argv[4] if len(sys.argv) > 4 else None
SM = int(sys.argv[5]) if len(sys.argv) > 5 else 120
HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]


def run(*args):
    p = subprocess.run([str(a) for a in args], capture_output=True, text=True, timeout=90)
    assert p.returncode == 0, (args, p.returncode, p.stdout, p.stderr)
    return p


with tempfile.TemporaryDirectory() as directory:
    temp = Path(directory)
    source = HERE / 'cuda_async_kernels.tv'
    ir = temp / 'async.ll'; package = temp / 'async.tvcp'
    run(TVC, '--emit-gpu-nvptx', source, '-o', ir)
    run(sys.executable, ROOT / 'tools/cuda_package.py', ir, '--llc', LLC, '--sm', SM if CUDA else 90, '-o', package)
    for name in ('gate', 'faults'):
        run(TVC, HERE / f'cuda_async_{name}.tv', '--emit', 'obj', '-llc', LLC, '-o', temp / f'{name}.o')
    run(LINK, '-no-pie', '-Wall', '-Wextra', '-Werror', temp / 'faults.o', HERE / 'cuda_driver_mock.c', '-o', temp / 'mock')
    print(run(temp / 'mock', package).stdout.strip())
    if CUDA:
        run(LINK, '-no-pie', temp / 'gate.o', CUDA, f'-Wl,-rpath,{Path(CUDA).parent}', '-o', temp / 'gate')
        print(run(temp / 'gate', package, SM).stdout.strip())
        print(f'CUDA async PASS: native SM{SM}, eight event-ordered fork/join rounds')
