"""Check shared atomic phases and full matrix transpose with boundary tiles."""
from collections import Counter
import ctypes
import hashlib
import itertools
import json
import math
from pathlib import Path
import re
import subprocess
import sys
import tempfile

TVC, LLC, OPT, LINK = sys.argv[1:5]
CUDA = sys.argv[5] if len(sys.argv) > 5 else None
SM = int(sys.argv[6]) if len(sys.argv) > 6 else 120
HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
TRIPLE = re.search(r'Default target: (\S+)', subprocess.check_output([LLC, '--version'], text=True))[1]
SENTINEL = int.from_bytes(b'\xa5' * 8, 'little')


def run(*args, code=0):
    result = subprocess.run([str(a) for a in args], capture_output=True, text=True, timeout=90)
    assert result.returncode == code, (args, result.returncode, result.stdout, result.stderr)
    return result


def host_atomic(match):
    result, order, operation, pointer, value, replacement = match.groups()
    order = 'monotonic' if order == 'relaxed' else order
    if operation == 'cas':
        failure = 'acquire' if order in ('acquire', 'acq_rel') else 'monotonic'
        return (f'{result}pair = cmpxchg ptr {pointer}, i64 {value}, i64 {replacement} {order} {failure}, align 8\n'
                f'  {result} = extractvalue {{i64, i1}} {result}pair, 0')
    operation = 'xchg' if operation == 'exch' else 'add'
    return f'{result} = atomicrmw {operation} ptr {pointer}, i64 {value} {order}, align 8'


header = f'import "{ROOT / "src/lib/gpu/shared.tv"}"; import "{ROOT / "src/lib/gpu/atomic.tv"}";\n'
signature = '(t:GpuThread,input:*u64,output:*u64,limit:u64,slots:u64)'
helpers = '''
fn bucket(i:u64,limit:u64,slots:u64)->u64 { if i>=limit {return 18446744073709551615;} else {return slots-1;} }
fn columns(count:u64,rows:u64)->u64 { if rows==0 {return 0;} else {return count/rows;} }
fn checked_index<T>(row:T,col:T,rows:T,cols:T,bound:T)->T {
    if row>=rows {return bound;} else {if col>=cols {return bound;} else {return row*cols+col;}}
}
fn live(x:u64,y:u64,rows:u64,cols:u64)->bool {if x>=rows {return false;} else {return y<cols;}}
'''
bodies = []
owners = []
for dynamic in (False, True):
    for operation, name in enumerate(('add', 'exchange', 'compare_exchange')):
        for order in range(4):
            init = 'gpu_shared_dynamic_init_u64(slots);' if dynamic else 'gpu_shared_init_u64(1024);'
            # Static cases exercise the arena's last slot, independent of launch size.
            bound = 'slots' if dynamic else '1024'
            value = 'v' if operation == 0 else 'gpu_local_index(t)+v'
            expected = ',0' if operation == 2 else ''
            body = (init + 'let i=gpu_global_index(t); let active=i<limit; '
                    'let v=gpu_masked_load_u64(input,i,active); '
                    f'let old=gpu_shared_atomic_{name}_u64(bucket(i,limit,{bound}),{value},{order},0{expected}); '
                    f'gpu_block_barrier(); let final=gpu_shared_load_u64({bound}-1); '
                    'gpu_masked_store_u64(output,i,(final<<32)|old,active); '
                    'gpu_block_barrier(); gpu_shared_store_u64(gpu_local_index(t),final); gpu_block_barrier();')
            bodies.append(body); owners.append(f'block_{int(dynamic)}_{operation}_{order}')
base = ('let i=gpu_global_index(t); let cols=columns(limit,slots); '
        'let x=gpu_global_x(t); let row=gpu_global_y(t)+gpu_global_size_y(t)*gpu_global_z(t); ')
direct = base + ('let value=gpu_bounded_load_u64(input,limit,checked_index(x,row,slots,cols,limit)); '
                 'gpu_masked_store_u64(output,i,value,live(x,row,slots,cols));')
tiled = 'gpu_shared_init_u64(1024);' + base + (
    'let local=gpu_local_index(t); let height=t.block_dim_y*t.block_dim_z; '
    'let source_row=t.block_x*t.block_dim_x+local/height; let q=local%height; '
    'let source_col=t.block_y*t.block_dim_y+q%t.block_dim_y+'
    'gpu_global_size_y(t)*(t.block_z*t.block_dim_z+q/t.block_dim_y); '
    'let value=gpu_bounded_load_u64(input,limit,checked_index(source_row,source_col,slots,cols,limit)); '
    'gpu_shared_store_u64(local,value); gpu_block_barrier(); '
    'let transposed=gpu_shared_load_u64(t.thread_x*height+t.thread_y+t.block_dim_y*t.thread_z); '
    'gpu_masked_store_u64(output,i,transposed,live(x,row,slots,cols));')
bodies.extend([direct, tiled]); owners.extend(['transpose', 'transpose_tiled'])

with tempfile.TemporaryDirectory() as directory:
    temp = Path(directory)
    (temp / 'helpers.tv').write_text(helpers)
    source = temp / 'block.tv'
    source.write_text(header + 'import "helpers.tv";\n' + '\n'.join(
        f'#[kernel] fn {name}{signature} {{ {body} }}' for name, body in zip(owners, bodies)))
    ir = temp / 'block.ll'; run(TVC, '--emit-gpu-nvptx', source, '-o', ir)
    text = ir.read_text()
    entries = [json.loads(line.split(' ', 2)[2]) for line in text.splitlines() if line.startswith('; traveler.kernel.v1 ')]
    assert all(k['profile'] == 'cooperative-atomic-v1' for k in entries[:24])
    assert all(k['parameters'][0]['footprint'] == {'count_parameter': 2, 'byte_scale': 8} for k in entries[24:])
    run(OPT, '-passes=verify', '-disable-output', ir)
    for sm in (70, 90):
        run(LLC, f'-mcpu=sm_{sm}', ir, '-o', temp / 'block.ptx')
        ptx = (temp / 'block.ptx').read_text()
        for order, operation in itertools.product(('relaxed', 'acquire', 'release', 'acq_rel'), ('add.u64', 'exch.b64', 'cas.b64')):
            assert f'atom.{order}.cta.shared.{operation}' in ptx
    native = text.replace('define ptx_kernel', 'define').replace('ptr addrspace(1)', 'ptr').replace('ptr addrspace(3)', 'ptr')
    native = native.replace('internal addrspace(3) global', 'internal global')
    native = native.replace('external addrspace(3) global [0 x i64]', 'global [1024 x i64] zeroinitializer')
    native = re.sub(r'addrspacecast ptr (%\w+) to ptr', r'getelementptr i8, ptr \1, i64 0', native)
    native = re.sub(r'(%\w+) = call i64 asm sideeffect "atom\.(\w+)\.cta\.shared\.(add|exch|cas)\.[ub]64[^\n]+"\(ptr (%\w+), i64 (%\w+)(?:, i64 (%\w+))?\)', host_atomic, native)
    for kind, axis in itertools.product(('tid', 'ctaid', 'ntid', 'nctaid'), 'xyz'):
        native = native.replace(f'llvm.nvvm.read.ptx.sreg.{kind}.{axis}', f'test_{kind}_{axis}')
    native = native.replace('llvm.nvvm.barrier0', 'test_barrier')
    (temp / 'native.ll').write_text(native)
    run(OPT, '-passes=verify', '-disable-output', temp / 'native.ll')
    run(LLC, f'-mtriple={TRIPLE}', '-relocation-model=pic', '-filetype=obj', temp / 'native.ll', '-o', temp / 'native.o')
    run(LINK, '-shared', '-pthread', '-fPIC', temp / 'native.o', HERE / 'shared_sim.c', '-o', temp / 'native.so')
    lib = ctypes.CDLL(str(temp / 'native.so'))
    simulate = lib.simulate_dynamic
    simulate.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint64, ctypes.c_uint64, ctypes.c_void_p]
    run(sys.executable, ROOT / 'tools/cuda_package.py', ir, '--llc', LLC, '--sm', SM if CUDA else 90, '-o', temp / 'block.tvcp')
    result = subprocess.run([sys.executable, str(ROOT / 'tools/cuda_package.py'), str(ir), '--llc', LLC,
                             '--sm', '60', '-o', str(temp / 'old.tvcp')], capture_output=True)
    assert result.returncode != 0 and not (temp / 'old.tvcp').exists()
    run(TVC, HERE / 'cuda_package_gate.tv', '--emit', 'obj', '-llc', LLC, '-o', temp / 'loader.o')
    run(LINK, '-no-pie', temp / 'loader.o', '-o', temp / 'loader')
    run(temp / 'loader', temp / 'block.tvcp')
    image = (temp / 'block.tvcp').read_bytes(); length = int.from_bytes(image[8:12], 'little')
    manifest = json.loads(image[12:12+length])
    artifact = re.sub(rb'(?m)^\.target sm_\d+', b'.target sm_60', image[12+length:])
    manifest['sm'] = 60; manifest['ptx_bytes'] = len(artifact); manifest['ptx_sha256'] = hashlib.sha256(artifact).hexdigest()
    encoded = json.dumps(manifest).encode()
    (temp / 'old.tvcp').write_bytes(image[:8]+len(encoded).to_bytes(4, 'little')+encoded+artifact)
    assert subprocess.run([str(temp / 'loader'), str(temp / 'old.tvcp')], capture_output=True).returncode != 0
    if CUDA:
        run(TVC, HERE / 'cuda_boundary_gate.tv', '--emit', 'obj', '-llc', LLC, '-o', temp / 'gate.o')
        run(LINK, '-no-pie', temp / 'gate.o', CUDA, f'-Wl,-rpath,{Path(CUDA).parent}', '-o', temp / 'gate')

    def execute(mode, values, grid, block, limit, slots, shared=0):
        lanes = math.prod(grid + block)
        inp = (ctypes.c_uint64 * len(values))(*values)
        if math.prod(block) <= 128:
            out = (ctypes.c_uint64 * lanes)(*([SENTINEL] * lanes))
            simulate(getattr(lib, entries[mode]['symbol']), inp if values else None, out, limit, slots,
                     (ctypes.c_uint32 * 6)(*grid, *block))
            yield list(out)
        if CUDA:
            (temp / 'input').write_bytes(bytes(inp))
            run(temp / 'gate', temp / 'block.tvcp', owners[mode], temp / 'input', temp / 'output', *grid, *block, limit, slots, shared)
            data = (temp / 'output').read_bytes()
            assert data[:16] == data[-16:] == b'\xa5'*16
            yield [int.from_bytes(data[i:i+8], 'little') for i in range(16, len(data)-16, 8)]

    shapes = [((2, 2, 1), (3, 5, 1)), ((1, 2, 2), (4, 3, 2))]
    if CUDA:
        shapes.append(((2, 1, 1), (32, 8, 4)))
    for grid, block in shapes:
        lanes = math.prod(grid + block)
        for active in (0, 1, lanes-1, lanes):
            for mode in range(24):
                dynamic, operation = mode // 12, mode % 12 // 4
                slots = (1, 19, 1024)[mode % 3]
                for out in execute(mode, [1]*lanes, grid, block, active, slots, slots*8 if dynamic else 0):
                    assert all(out[i] == SENTINEL for i in range(active, lanes))
                    for bz, by, bx in itertools.product(range(grid[2]), range(grid[1]), range(grid[0])):
                        items = []
                        for tz, ty, tx in itertools.product(range(block[2]), range(block[1]), range(block[0])):
                            x, y, z = bx*block[0]+tx, by*block[1]+ty, bz*block[2]+tz
                            i = x+grid[0]*block[0]*(y+grid[1]*block[1]*z)
                            if i < active:
                                items.append((tx+block[0]*(ty+block[1]*tz)+1, out[i]))
                        if not items:
                            continue
                        final = items[0][1] >> 32
                        assert all(v >> 32 == final for _, v in items)
                        old = [v & 0xffffffff for _, v in items]
                        if operation == 0:
                            assert final == len(items) and sorted(old) == list(range(len(items)))
                        elif operation == 1:
                            assert Counter(old+[final]) == Counter([0]+[value for value, _ in items])
                        else:
                            assert final in [value for value, _ in items] and old.count(0) == 1
                            assert all(v in (0, final) for v in old)

    matrix_shapes = [(0, 0), (0, 7), (7, 0), (1, 1), (1, 33), (33, 1), (2, 3), (3, 2), (7, 11), (31, 33), (33, 31)]
    for rows, cols in matrix_shapes:
        values = [((i*6364136223846793005) ^ (i << 17)) & ((1 << 64)-1) for i in range(rows*cols)]
        for block in ((3, 5, 1), (4, 3, 2), (7, 1, 1)):
            grid = (max(1, (rows+block[0]-1)//block[0]), max(1, (cols+block[1]*block[2]-1)//(block[1]*block[2])), 1)
            sx, sy, sz = [grid[i]*block[i] for i in range(3)]
            expected = [SENTINEL]*(sx*sy*sz)
            for row in range(cols):
                for col in range(rows):
                    expected[col+sx*row] = values[col*cols+row]
            for mode in (24, 25):
                for out in execute(mode, values, grid, block, rows*cols, rows):
                    assert out == expected, (rows, cols, grid, block, mode)

    init = 'gpu_shared_init_u64(32); '
    atomic = 'let old=gpu_shared_atomic_add_u64(0,1,0,0); '
    failures = [atomic, init+atomic+'let v=gpu_shared_load_u64(0);',
                init+atomic+'gpu_shared_store_u64(gpu_local_index(t),1);',
                init+'gpu_shared_store_u64(gpu_local_index(t),1);'+atomic,
                init+'let v=gpu_shared_load_u64(0);'+atomic,
                init+atomic.replace(',0,0);', ',0,1);'), init+atomic.replace(',0,0);', ',4,0);'),
                init+'if t.thread_x==0 {'+atomic+'}',
                'let v=gpu_bounded_load_u64(input,limit+1,0);',
                'let v=gpu_bounded_load_u64(input,limit,0); let w=input[gpu_global_index(t)];']
    for body in failures:
        source.write_text(header+f'#[kernel] fn bad{signature} {{ {body} output[gpu_global_index(t)]=input[gpu_global_index(t)]; }}')
        (temp / 'bad.ll').write_text('previous')
        run(TVC, '--emit-gpu-nvptx', source, '-o', temp / 'bad.ll', code=1)
        assert (temp / 'bad.ll').read_text() == 'previous'
    print('block atomics/transpose PASS: 24 scope/order/arena kernels, phase refusals, direct/tiled transpose, empty and partial boundaries')
    if CUDA:
        print(f'block atomics/transpose CUDA PASS: native SM{SM}, repeated launches, 1024-thread blocks, rectangular and 3D tiles')
