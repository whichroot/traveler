"""Run the asynchronous DAX pipeline with paused workers and deferred DMA."""
from pathlib import Path
import runpy
import sys

sys.argv.append('nonblocking')
runpy.run_path(str(Path(__file__).with_name('check_cuda_dax_parallel.py')), run_name='__main__')
