"""Check shared phases and masks with threaded retargeting and optional CUDA."""
import ctypes
import itertools
import math
from pathlib import Path
import random
import re
import subprocess
import sys
import tempfile

TVC, LLC, OPT, LINK = sys.argv[1:5]
CUDA = sys.argv[5] if len(sys.argv) > 5 else None
SM = int(sys.argv[6]) if len(sys.argv) > 6 else 120
HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
TRIPLE = re.search(r"Default target: (\S+)", subprocess.check_output([LLC, "--version"], text=True))[1]


def run(*args, code=0):
    result = subprocess.run([str(a) for a in args], capture_output=True, text=True, timeout=90)
    assert result.returncode == code, (args, result.returncode, result.stdout, result.stderr)
    return result


header = f'import "{ROOT / "src/lib/gpu/shared.tv"}";\n'
signature = '(t:GpuThread,input:*u64,output:*u64,limit:u64)'
start = ('gpu_shared_init_u64(1024); let index=gpu_global_index(t); '
         'let local=gpu_local_index(t); let active=index<limit; ')
scan = start + ('let value=gpu_masked_load_u64(input,index,active); '
                'gpu_shared_store_u64(local,value); gpu_block_barrier(); ')
for step in range(10):
    scan += (f'let a{step}=gpu_shared_load_u64(local); '
             f'let b{step}=gpu_shared_load_u64(local-{1 << step}); '
             f'gpu_block_barrier(); gpu_shared_store_u64(local,a{step}+b{step}); gpu_block_barrier(); ')
peer = start + ('var pair:[u64;2]=0; pair[0]=gpu_masked_load_u64(input,index,active); '
                'gpu_shared_store_u64(local,pair[0]); gpu_block_barrier(); '
                'pair[1]=gpu_shared_load_u64(local^1); '
                'gpu_masked_store_u64(output,index,pair[0]+pair[1],active);')
bodies = [peer, scan + 'gpu_masked_store_u64(output,index,gpu_shared_load_u64(local),active);',
          scan + 'gpu_masked_store_u64(output,index,gpu_shared_load_u64(t.block_dim_x*t.block_dim_y*t.block_dim_z-1),active);']
bodies.append('gpu_masked_store_u64(output,gpu_global_index(t),gpu_masked_load_u64(input,gpu_global_index(t),2)+1,2);')

with tempfile.TemporaryDirectory() as directory:
    temp = Path(directory)
    source = temp / "shared.tv"
    source.write_text(header + '\n'.join(f'#[kernel] fn shared_{i}{signature} {{ {body} }}' for i, body in enumerate(bodies)))
    ir = temp / "shared.ll"
    run(TVC, "--emit-gpu-nvptx", source, "-o", ir)
    text = ir.read_text()
    assert text.count('internal addrspace(3) global [1024 x i64]') == 3
    assert 'alloca' not in text and 'load i64, ptr addrspace(3)' in text
    assert text.count('call void @llvm.nvvm.barrier0()') == 46
    run(OPT, '-passes=verify', '-disable-output', ir)
    run(LLC, '-mcpu=sm_90', ir, '-o', temp / 'shared.ptx')
    native = text.replace('define ptx_kernel', 'define').replace('ptr addrspace(1)', 'ptr').replace('ptr addrspace(3)', 'ptr')
    native = native.replace('internal addrspace(3) global', 'internal global')
    native = re.sub(r'addrspacecast ptr (%\w+) to ptr', r'getelementptr i8, ptr \1, i64 0', native)
    for kind in ('tid', 'ctaid', 'ntid', 'nctaid'):
        for axis in 'xyz':
            native = native.replace(f'llvm.nvvm.read.ptx.sreg.{kind}.{axis}', f'test_{kind}_{axis}')
    native = native.replace('llvm.nvvm.barrier0', 'test_barrier')
    (temp / 'native.ll').write_text(native)
    run(OPT, '-passes=verify', '-disable-output', temp / 'native.ll')
    run(LLC, f'-mtriple={TRIPLE}', '-relocation-model=pic', '-filetype=obj', temp / 'native.ll', '-o', temp / 'native.o')
    run(LINK, '-shared', '-fPIC', '-pthread', '-Wall', '-Wextra', '-Werror', temp / 'native.o', HERE / 'shared_sim.c', '-o', temp / 'native.so')
    lib = ctypes.CDLL(str(temp / 'native.so'))
    simulate = lib.simulate
    simulate.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint64, ctypes.c_void_p]
    simulate.restype = None
    if CUDA:
        run(sys.executable, ROOT / 'tools/cuda_package.py', ir, '--llc', LLC, '--sm', SM, '-o', temp / 'shared.tvcp')
        run(TVC, HERE / 'cuda_shared_gate.tv', '--emit', 'obj', '-llc', LLC, '-o', temp / 'gate.o')
        run(LINK, '-no-pie', temp / 'gate.o', CUDA, f'-Wl,-rpath,{Path(CUDA).parent}', '-o', temp / 'gate')
    rng = random.Random(313)
    mask = (1 << 64) - 1
    shapes = [((2, 2, 1), (4, 3, 1)), ((3, 1, 1), (7, 1, 1)), ((2, 1, 2), (2, 3, 2))]
    shapes.extend(((2, 1, 1), (n, 1, 1)) for n in (1, 31, 32, 33))
    if CUDA:
        shapes.append(((2, 1, 1), (32, 8, 4)))
    for grid, block in shapes:
        count = math.prod(grid + block)
        values = [rng.getrandbits(64) for _ in range(count)]
        inp = (ctypes.c_uint64 * count)(*values)
        threads = math.prod(block)
        for active in sorted({0, 1, threads - 1, threads, min(count, threads + 1), count - 1, count}):
            for mode in range(3):
                expected = values.copy()
                for bz, by, bx in itertools.product(range(grid[2]), range(grid[1]), range(grid[0])):
                    indices = []
                    for tz, ty, tx in itertools.product(range(block[2]), range(block[1]), range(block[0])):
                        x, y, z = bx * block[0] + tx, by * block[1] + ty, bz * block[2] + tz
                        indices.append(x + grid[0] * block[0] * (y + grid[1] * block[1] * z))
                    live = [values[i] if i < active else 0 for i in indices]
                    prefixes = list(itertools.accumulate(live))
                    for local, index in enumerate(indices):
                        if index < active:
                            value = (live[local] + (live[local ^ 1] if local ^ 1 < len(live) else 0) if mode == 0
                                     else prefixes[local] if mode == 1 else sum(live))
                            expected[index] = value & mask
                if math.prod(block) <= 128:
                    out = (ctypes.c_uint64 * count)(*values)
                    simulate(getattr(lib, f'__traveler_kernel_{mode}'), inp if active else None,
                             out if active else None, active, (ctypes.c_uint32 * 6)(*grid, *block))
                    assert list(out) == expected, (grid, block, active, mode)
                if CUDA:
                    (temp / 'input').write_bytes(bytes(inp))
                    run(temp / 'gate', temp / 'shared.tvcp', f'shared_{mode}', temp / 'input', temp / 'output', *grid, *block, active)
                    assert (temp / 'output').read_bytes() == b''.join(v.to_bytes(8, 'little') for v in expected), (grid, block, active, mode)

    inp = (ctypes.c_uint64 * 7)(*range(7))
    out = (ctypes.c_uint64 * 7)(*range(7))
    simulate(lib.__traveler_kernel_3, inp, out, 0, (ctypes.c_uint32 * 6)(1, 1, 1, 7, 1, 1))
    assert list(out) == list(range(1, 8)), 'boolean masks use nonzero truth, not the low bit'
    cpu_source = temp / 'mask_cpu.tv'
    cpu_source.write_text(header + '#[export] fn mask_cpu(input:*u64,output:*u64) {'
                          'output[0]=gpu_masked_load_u64(input,0,2); gpu_masked_store_u64(output,1,99,2);}')
    run(TVC, cpu_source, '-o', temp / 'mask_cpu.ll')
    run(LLC, '-relocation-model=pic', '-filetype=obj', temp / 'mask_cpu.ll', '-o', temp / 'mask_cpu.o')
    run(LINK, '-shared', temp / 'mask_cpu.o', '-o', temp / 'mask_cpu.so')
    cpu_lib = ctypes.CDLL(str(temp / 'mask_cpu.so'))
    cpu_lib.mask_cpu.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    cpu_result = (ctypes.c_uint64 * 2)()
    cpu_lib.mask_cpu((ctypes.c_uint64 * 1)(7), cpu_result)
    assert list(cpu_result) == [7, 99]
    if CUDA:
        (temp / 'input').write_bytes(bytes(inp))
        run(temp / 'gate', temp / 'shared.tvcp', 'shared_3', temp / 'input', temp / 'output', 1, 1, 1, 7, 1, 1, 0)
        assert (temp / 'output').read_bytes() == bytes(out)

    failures = [
        'gpu_shared_init_u64(0);', 'gpu_shared_init_u64(1025);',
        'gpu_shared_init_u64(limit);', 'gpu_block_barrier(); gpu_shared_init_u64(32);',
        'gpu_shared_init_u64(32); gpu_shared_init_u64(32);',
        'let value=gpu_shared_load_u64(0);',
        start + 'gpu_shared_store_u64(local,1); let value=gpu_shared_load_u64(local);',
        start + 'let value=gpu_shared_load_u64(local); gpu_shared_store_u64(local,value);',
        start + 'gpu_shared_store_u64(local^1,1);',
        'if t.thread_x==0 {gpu_block_barrier();}',
        'gpu_masked_store_u64(output,gpu_global_index(t)+1,1,true);',
        'let value=gpu_masked_load_u64(input,gpu_global_index(t),limit);',
        'helper(t);',
    ]
    for body in failures:
        bad = temp / 'bad.tv'
        bad.write_text(header + 'fn helper(t:GpuThread) { gpu_block_barrier(); }\n'
                       f'#[kernel] fn bad{signature} {{ {body} output[gpu_global_index(t)]=input[gpu_global_index(t)]; }}\n'
                       f'#[kernel] fn good{signature} {{ output[gpu_global_index(t)]=input[gpu_global_index(t)]; }}')
        target = temp / 'bad.ll'; target.write_text('previous artifact')
        try:
            run(TVC, '--emit-gpu-nvptx', bad, '-o', target, code=1)
        except AssertionError as error:
            raise AssertionError(body) from error
        assert target.read_text() == 'previous artifact'
    print('shared block PASS: threaded visibility, initialization, masks, peer exchange, scan/reduction, phase refusals')
    if CUDA:
        print(f'shared block CUDA PASS: native SM{SM}, three kernels, {len(shapes)} shapes, block/warp boundaries, partial/all-masked tiles, repeated launches')
