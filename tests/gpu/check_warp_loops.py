"""Check warp-uniform row/token loops and divergent backedge refusals."""
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
HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
HEADER = (f'import "{ROOT / "src/lib/gpu/warp.tv"}";\n'
          f'import "{ROOT / "src/lib/gpu/shared.tv"}";\n')
SIGNATURE = '(t:GpuThread,input:*u64,output:*u64,limit:u64,slots:u64)'
SETUP = '''
    let index = gpu_global_index(t);
    let local = gpu_local_index(t);
    let warps = (t.block_dim_x * t.block_dim_y * t.block_dim_z) >> 5;
    let stride = t.grid_dim_x * t.grid_dim_y * t.grid_dim_z * warps;
    var row = gpu_block_index(t) * warps + (local >> 5);
'''
REDUCE = '\n'.join(f'v = v + gpu_warp_shuffle_xor_u32(4294967295,v,{d});'
                   for d in (16, 8, 4, 2, 1))
BODY = SETUP + '''
    let value = input[index];
    var total: u64 = 0;
    while row < limit {
        var token: u64 = 0;
        while token < slots {
            var v: u32 = (value + row + token) as u32;
            if (local & 1) == 0 { v = v + 1; } else { v = v + 2; }
''' + REDUCE + '''
            if (row & 1) == 0 {
                let votes = gpu_warp_ballot(4294967295,(local & 1) == 0);
                let first = gpu_warp_shuffle_u32(4294967295,v,0);
                gpu_warp_sync(4294967295);
                v = first ^ votes;
            }
            total = total + (v as u64);
            token = token + 1;
        }
        row = row + stride;
    }
    output[index] = total;
'''


def run(*args, code=0):
    result = subprocess.run(list(map(str, args)), capture_output=True, text=True, timeout=90)
    assert result.returncode == code, (args, result.returncode, result.stdout, result.stderr)
    return result


with tempfile.TemporaryDirectory() as directory:
    tmp = Path(directory)
    source = tmp / 'loops.tv'
    source.write_text(HEADER + f'#[kernel] fn rows{SIGNATURE} {{ {BODY} }}')
    ir = tmp / 'loops.ll'
    run(TVC, '--emit-gpu-nvptx', source, '-o', ir)
    text = ir.read_text()
    assert 'cooperative-warp-v1' in text
    assert 'phi i64' in text and 'alloca' not in text
    run(OPT, '-passes=verify', '-disable-output', ir)
    run(OPT, '-passes=default<O2>', '-verify-each', '-S', ir, '-o', tmp / 'optimized.ll')
    run(LLC, '-mcpu=sm_90', tmp / 'optimized.ll', '-o', tmp / 'loops.ptx')
    assert 'shfl.sync.bfly.b32' in (tmp / 'loops.ptx').read_text()

    native = text.replace('define ptx_kernel', 'define').replace('ptr addrspace(1)', 'ptr')
    native = re.sub(r'addrspacecast ptr (%\w+) to ptr', r'getelementptr i8, ptr \1, i64 0', native)
    for kind in ('tid', 'ctaid', 'ntid', 'nctaid'):
        for axis in 'xyz':
            native = native.replace(f'llvm.nvvm.read.ptx.sreg.{kind}.{axis}', f'test_{kind}_{axis}')
    for old, new in [('bar.warp.sync', 'warp_sync'), ('shfl.sync.idx.i32', 'shuffle'),
                     ('shfl.sync.bfly.i32', 'shuffle_xor'), ('vote.ballot.sync', 'ballot')]:
        native = native.replace('llvm.nvvm.' + old, 'test_' + new)
    (tmp / 'native.ll').write_text(native)
    run(OPT, '-passes=verify', '-disable-output', tmp / 'native.ll')
    triple = re.search(r'Default target: (\S+)', run(LLC, '--version').stdout)[1]
    run(LLC, f'-mtriple={triple}', '-relocation-model=pic', '-filetype=obj',
        tmp / 'native.ll', '-o', tmp / 'native.o')
    run(LINK, '-shared', '-fPIC', '-pthread', '-Wall', '-Wextra', '-Werror',
        tmp / 'native.o', HERE / 'shared_sim.c', '-o', tmp / 'native.so')
    lib = ctypes.CDLL(str(tmp / 'native.so'))
    simulate = lib.simulate_dynamic
    simulate.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p,
                         ctypes.c_uint64, ctypes.c_uint64, ctypes.c_void_p]
    simulate.restype = None
    rng = random.Random(829)
    cases = 0
    for grid, block in [((2, 1, 1), (64, 1, 1)), ((1, 2, 1), (8, 4, 2)),
                        ((2, 1, 2), (4, 4, 2))]:
        count = math.prod(grid + block)
        values = [rng.getrandbits(32) for _ in range(count)]
        inp = (ctypes.c_uint64 * count)(*values)
        warps = math.prod(block) // 32
        stride = math.prod(grid) * warps
        for rows, tokens in [(0, 3), (stride + 1, 0), (1, 2), (stride + 1, 3), (2 * stride - 1, 1)]:
            expected = [0] * count
            for bz, by, bx in itertools.product(range(grid[2]), range(grid[1]), range(grid[0])):
                indices = []
                for tz, ty, tx in itertools.product(range(block[2]), range(block[1]), range(block[0])):
                    x, y, z = bx * block[0] + tx, by * block[1] + ty, bz * block[2] + tz
                    indices.append(x + grid[0] * block[0] * (y + grid[1] * block[1] * z))
                for warp in range(warps):
                    lanes = indices[warp * 32:(warp + 1) * 32]
                    first = (bx + grid[0] * (by + grid[1] * bz)) * warps + warp
                    total = 0
                    for row in range(first, rows, stride):
                        for token in range(tokens):
                            value = sum(values[index] + row + token + 1 + lane % 2
                                        for lane, index in enumerate(lanes)) & 0xffffffff
                            total += value ^ (0x55555555 if row % 2 == 0 else 0)
                    for index in lanes:
                        expected[index] = total
            out = (ctypes.c_uint64 * count)(*([0xdeadbeef] * count))
            simulate(lib.__traveler_kernel_0, inp, out, rows, tokens,
                     (ctypes.c_uint32 * 6)(*grid, *block))
            assert list(out) == expected, (grid, block, rows, tokens)
            cases += 1

    shuffle = 'let v = gpu_warp_shuffle_xor_u32(4294967295,1,1);'
    failures = [
        f'while row < limit {{ {shuffle} row = row + local + 1; }}',
        f'while row < limit {{ row = row + stride; if (local & 1) == 0 {{ {shuffle} }} }}',
        f'while row < limit {{ var j:u64=0; while j<local {{ {shuffle} j=j+1; }} row=row+stride; }}',
        f'while local < limit {{ var j:u64=0; while j<slots {{ {shuffle} j=j+1; }} }}',
        f'var bound:u64=1; if local==0 {{ bound=2; }} while row<bound {{ {shuffle} row=row+stride; }}',
        f'var j=gpu_global_index(t)>>5; while j<limit {{ {shuffle} j=j+stride; }}',
        f'var j=t.thread_x>>5; while j<limit {{ {shuffle} j=j+1; }}',
        f'while row<limit {{ {shuffle} row=input[index]; }}',
        f'while row<limit {{ gpu_block_barrier(); row=row+stride; }}',
    ]
    for body in failures:
        source.write_text(HEADER + f'#[kernel] fn bad{SIGNATURE} {{ {SETUP} {body} output[index]=0; }}')
        ir.write_text('previous artifact')
        result = run(TVC, '--emit-gpu-nvptx', source, '-o', ir, code=1)
        assert 'conditional-effect' in result.stderr, result.stderr
        assert ir.read_text() == 'previous artifact'
    print(f'warp loops PASS: {cases} threaded row/token cases, {len(failures)} refusals, verified optimized PTX')
