"""Check pool pointer atomics in typed IR, with optional LLVM 14 execution."""
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys
import tempfile

tvc, llvm14, linker = sys.argv[1:4]
root = Path(__file__).resolve().parents[2]


def run(*args, threads='1'):
    result = subprocess.run(list(map(str, args)), capture_output=True, text=True,
                            timeout=90, env=dict(os.environ, TRAVELER_THREADS=threads))
    assert result.returncode == 0, (args, result.returncode, result.stdout, result.stderr)
    return result.stdout


with tempfile.TemporaryDirectory(prefix='typed-atomics-') as tmp:
    d = Path(tmp)
    for name in ('pfor_pool_stress', 'pfor_i64_bounds'):
        source = root / 'tests/pfor' / (name + '.tv')
        typed, opaque = d / 'typed.ll', d / 'opaque.ll'
        run(tvc, source, '-target', 'tpc', '-o', typed)
        run(tvc, source, '-o', opaque)
        text = typed.read_text()
        assert len(re.findall(r'load atomic i8\*, i8\*\* @__pfor_job_\w+ monotonic, align 8', text)) == 2
        assert len(re.findall(r'store atomic i8\* %\w+, i8\*\* @__pfor_job_\w+ monotonic, align 8', text)) == 2
        for instruction in ('fence release', 'fence acquire'):
            assert text.count(instruction) == opaque.read_text().count(instruction) > 0
        assert not re.search(r'(?<![\w.%@])ptr(?![\w])', text)
        if llvm14:
            tools = Path(llvm14)
            run(tools / 'llvm-as', typed, '-o', d / 'typed.bc')
            triple = re.search(r'Default target: (\S+)', run(tools / 'llc', '--version'))[1]
            flags = shlex.split(os.environ.get('TRAVELER_LINK_FLAGS', '-no-pie' if sys.platform == 'linux' else ''))
            for profile in ('raw', 'O3'):
                selected = typed
                if profile == 'O3':
                    selected = d / 'optimized.ll'
                    run(tools / 'opt', '-mtriple=' + triple, '-O3', '-S', typed, '-o', selected)
                run(tools / 'llc', '-mtriple=' + triple, '-filetype=obj', selected, '-o', d / 'job.o')
                run(linker, *flags, '-pthread', d / 'job.o', '-o', d / 'job')
                expected = (root / 'tests/pfor/expected' / (name + '.txt')).read_text().strip()
                for threads in ('1', '7', '32'):
                    assert run(d / 'job', threads=threads).strip() == expected, (name, profile, threads)
        print(name + ': typed pointer atomics PASS' + (' (LLVM 14 raw/O3, T=1/7/32)' if llvm14 else ' (emission)'))
