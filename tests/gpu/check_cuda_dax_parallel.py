"""Check parallel DAX staging and CPU-copy retention without CUDA hardware."""
from pathlib import Path
import os
import platform
import subprocess
import sys
import tempfile

TVC, LLC, OPT, LINK = sys.argv[1:5]
FIXTURE = sys.argv[5] if len(sys.argv) > 5 else 'parallel'
HERE = Path(__file__).resolve().parent


def run(*args, threads=1):
    p = subprocess.run(list(map(str, args)), capture_output=True, text=True,
                       env={**os.environ, 'TRAVELER_THREADS': str(threads)}, timeout=120)
    assert p.returncode == 0, (args, p.returncode, p.stdout, p.stderr)
    return p


with tempfile.TemporaryDirectory(prefix='traveler-dax-parallel-') as directory:
    temp = Path(directory)
    # Run the same byte, validation, and driver-failure cases through the new API.
    source = (HERE/'cuda_dax_staging.tv').read_text()
    source = source.replace('"cuda_resident_checks.tv"', f'"{HERE}/cuda_resident_checks.tv"')
    source = source.replace('"../../src/lib/gpu/cuda_dax.tv"',
                            f'"{HERE.parents[1]}/src/lib/gpu/cuda_dax.tv"')
    source = source.replace('cuda_upload_dax_async(', 'dax_parallel_upload(')
    source = source.replace('fn main()', 'fn dax_staging_suite()')
    source = source.replace('    dax_faults();', '')
    source += '\n' + (HERE/'cuda_dax_parallel.tv').read_text()
    if FIXTURE != 'parallel':
        source = source.replace('fn main()', 'fn dax_parallel_suite()')
        source += '\n' + (HERE/f'cuda_dax_{FIXTURE}.tv').read_text()
    source = source.replace('"../../src/lib/gpu/cuda_graph.tv"',
                            f'"{HERE.parents[1]}/src/lib/gpu/cuda_graph.tv"')
    source = source.replace('"../../src/lib/gpu/cuda_dax_pipeline.tv"',
                            f'"{HERE.parents[1]}/src/lib/gpu/cuda_dax_pipeline.tv"')
    (temp/'parallel.tv').write_text(source)
    run(TVC, temp/'parallel.tv', '--emit', 'ir', '--opt-level', 'none', '-o', temp/'raw.ll')
    ir = (temp/'raw.ll').read_text()
    # Rename before optimization so libc folding cannot remove the pause points.
    for symbol in ('memcpy', 'pthread_create', 'pthread_join', 'pthread_mutex_init',
                   'pthread_cond_init', 'pthread_mutex_destroy', 'pthread_cond_destroy'):
        assert f'@{symbol}(' in ir, symbol
        ir = ir.replace(f'@{symbol}(', f'@dax_test_{symbol}(')
    (temp/'checked.ll').write_text(ir)
    modes = ['none', 'o1'] if OPT else ['none']
    if OPT and platform.system() == 'Linux' and platform.machine() == 'x86_64':
        modes.append('o3')
    for mode in modes:
        selected = temp/'checked.ll'
        if mode != 'none':
            selected = temp/f'{mode}.ll'
            run(OPT, f'-passes=default<{mode.upper()}>', '-verify-each', '-S',
                temp/'checked.ll', '-o', selected)
        cpu = ['-mcpu=x86-64'] if mode == 'o3' else []
        run(LLC, '-filetype=obj', *cpu, selected, '-o', temp/f'{mode}.o')
        flags = [] if platform.system() == 'Darwin' else ['-no-pie']
        run(LINK, *flags, '-pthread', '-Wall', '-Wextra', '-Werror', temp/f'{mode}.o',
            HERE/'cuda_driver_mock.c', HERE/'cuda_dax_parallel_mock.c', '-o', temp/mode)
        for threads in (1, 4):
            output = run(temp/mode, threads=threads).stdout
            assert f'DAX {FIXTURE} PASS:' in output, output
    print(f'DAX {FIXTURE} mock PASS: {"/".join(modes)}, runtime threads 1/4')
