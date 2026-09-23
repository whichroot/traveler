"""Check DAX staging and graph replay; report host wall-clock samples."""
from pathlib import Path
import statistics
import subprocess
import sys
import tempfile

TVC, LLC, LINK = sys.argv[1:4]
CUDA = sys.argv[4] if len(sys.argv) > 4 else None
SM = int(sys.argv[5]) if len(sys.argv) > 5 else 120
DAX = sys.argv[6] if len(sys.argv) > 6 else '-'
SUDO_DAX = len(sys.argv) > 7 and sys.argv[7] == '--sudo-dax'
HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]


def run(*args):
    p = subprocess.run([str(a) for a in args], capture_output=True, text=True, timeout=180)
    assert p.returncode == 0, (args, p.returncode, p.stdout, p.stderr)
    return p.stdout


with tempfile.TemporaryDirectory() as directory:
    temp = Path(directory)
    ir = temp / 'pipeline.ll'; package = temp / 'pipeline.tvcp'
    run(TVC, '--emit-gpu-nvptx', HERE / 'cuda_async_kernels.tv', '-o', ir)
    run(sys.executable, ROOT / 'tools/cuda_package.py', ir, '--llc', LLC, '--sm', SM if CUDA else 90, '-o', package)
    run(TVC, HERE / 'cuda_pipeline_gate.tv', '--emit', 'obj', '-llc', LLC, '-o', temp / 'gate.o')
    run(LINK, '-no-pie', '-Wall', '-Wextra', '-Werror', '-DMOCK_ASYNC_CLEAN', temp / 'gate.o', HERE / 'cuda_driver_mock.c', '-o', temp / 'mock')
    assert 'pipeline PASS' in run(temp / 'mock', package, '-', 4096, 8, 1, 0)
    mapped = temp / 'readonly.bin'
    contents = bytes((i*37+i//251) % 256 for i in range(8192))
    mapped.write_bytes(contents)
    assert 'pipeline PASS' in run(temp / 'mock', package, f'@{mapped}', 4096, 8, 1, 0)
    assert mapped.read_bytes() == contents
    print('pipeline mock PASS: checked staging, command snapshots, replay, and exact results')
    run(TVC, HERE / 'cuda_graph_faults.tv', '--emit', 'obj', '-llc', LLC, '-o', temp / 'faults.o')
    run(LINK, '-no-pie', '-Wall', '-Wextra', '-Werror', temp / 'faults.o', HERE / 'cuda_driver_mock.c', '-o', temp / 'faults')
    print(run(temp / 'faults', package).strip())
    run(TVC, HERE / 'cuda_graph_fork.tv', '--emit', 'obj', '-llc', LLC, '-o', temp / 'fork.o')
    run(LINK, '-no-pie', '-DMOCK_ASYNC_CLEAN', temp / 'fork.o', HERE / 'cuda_driver_mock.c', '-o', temp / 'fork-mock')
    print(run(temp / 'fork-mock', package, 0).strip())
    if CUDA:
        run(LINK, '-no-pie', temp / 'fork.o', CUDA, f'-Wl,-rpath,{Path(CUDA).parent}', '-o', temp / 'fork')
        print(run(temp / 'fork', package, SM).strip())
        run(LINK, '-no-pie', temp / 'gate.o', CUDA, f'-Wl,-rpath,{Path(CUDA).parent}', '-o', temp / 'native')
        for source in dict.fromkeys(('-', DAX)):
            for size in (4096, 1048576, 16777216):
                prefix = ('sudo', '-n') if source != '-' and SUDO_DAX else ()
                output = run(*prefix, temp / 'native', package, source, size, 32, 7, SM)
                lines = output.splitlines(); samples = {i: [] for i in range(3)}
                for i, line in enumerate(lines):
                    if line == 'sample':
                        samples[int(lines[i+1])].append(int(lines[i+2]))
                print(f'source={source} bytes={size} rounds=32 setup_ns={lines[1]}')
                for mode, name in enumerate(('serial', 'pipeline', 'graph')):
                    ns = samples[mode]
                    assert len(ns) == 7 and min(ns) > 0
                    median = statistics.median(ns)
                    print(f'{name}: median_ms={median/1e6:.3f} min_ms={min(ns)/1e6:.3f} max_ms={max(ns)/1e6:.3f} input_GB_s={size*32/median:.3f} samples_ns={ns}')
        print(f'pipeline native SM{SM} PASS')
