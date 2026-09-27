"""Run cross-call DAX staging, completion-ticket, and backpressure checks."""
from pathlib import Path
import runpy
import sys

sys.argv.append('pipeline')
runpy.run_path(str(Path(__file__).with_name('check_cuda_dax_parallel.py')), run_name='__main__')
