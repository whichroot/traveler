"""Run persistent DAX pool lifetime and worker-reuse checks."""
from pathlib import Path
import runpy
import sys

sys.argv.append('pool')
runpy.run_path(str(Path(__file__).with_name('check_cuda_dax_parallel.py')), run_name='__main__')
