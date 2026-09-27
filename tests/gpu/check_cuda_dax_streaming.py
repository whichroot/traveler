"""Verify streaming pool copies with paused workers and deferred CUDA copies."""
from pathlib import Path
import runpy
import sys

sys.argv.append('streaming')
runpy.run_path(str(Path(__file__).with_name('check_cuda_dax_parallel.py')), run_name='__main__')
