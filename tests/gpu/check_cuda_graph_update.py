"""Check capture and graph updates with registered sources at raw/O1/O3."""
from pathlib import Path
import subprocess
import sys
import tempfile

tvc, llc, opt, link = sys.argv[1:5]
here = Path(__file__).resolve().parent


def run(*args):
    subprocess.run([str(a) for a in args], check=True, timeout=120)


with tempfile.TemporaryDirectory() as directory:
    d = Path(directory)
    for name in ('cuda_graph_update', 'cuda_graph_update_faults'):
        run(tvc, here / f'{name}.tv', '-o', d / 'raw.ll')
        for profile in ('raw', 'O1', 'O3'):
            ir = d / f'{profile}.ll'
            if profile != 'raw':
                run(opt, f'-passes=default<{profile}>', '-verify-each', '-S', d / 'raw.ll', '-o', ir)
            run(llc, '-filetype=obj', ir, '-o', d / 'gate.o')
            flags = ['-DMOCK_ASYNC_CLEAN'] if name == 'cuda_graph_update' else []
            run(link, '-no-pie', '-Wall', '-Wextra', '-Werror', *flags,
                d / 'gate.o', here / 'cuda_driver_mock.c', '-o', d / 'gate')
            run(d / 'gate')
