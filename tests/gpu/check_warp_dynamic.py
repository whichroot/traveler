"""Check launch-sized shared arenas and full-mask warp collectives."""
import copy
import ctypes
import hashlib
import importlib.util
import itertools
import json
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
spec = importlib.util.spec_from_file_location('package', ROOT / 'tools/cuda_package.py')
package = importlib.util.module_from_spec(spec)
spec.loader.exec_module(package)
TRIPLE = re.search(r'Default target: (\S+)', subprocess.check_output([LLC, '--version'], text=True))[1]


def run(*args, code=0):
    result = subprocess.run([str(a) for a in args], capture_output=True, text=True, timeout=90)
    assert result.returncode == code, (args, result.returncode, result.stdout, result.stderr)
    return result


header = f'import "{ROOT / "src/lib/gpu/shared.tv"}"; import "{ROOT / "src/lib/gpu/warp.tv"}";\n'
signature = '(t:GpuThread,input:*u64,output:*u64,limit:u64,slots:u64)'
start = ('let index=gpu_global_index(t); let local=gpu_local_index(t); let active=index<limit; '
         'let value=gpu_masked_load_u64(input,index,active); ')
arena = 'gpu_shared_dynamic_init_u64(slots); ' + start + 'gpu_shared_store_u64(local,value); gpu_block_barrier(); '
warp = ''
for step in range(5):
    warp += f'let r{step + 1}=r{step}+gpu_warp_shuffle_xor_u32(4294967295,r{step},{1 << step}); '
warp += ('let ballot=gpu_warp_ballot(4294967295,active); let any=gpu_warp_any(4294967295,active); '
         'let all=gpu_warp_all(4294967295,active); let broadcast=gpu_warp_shuffle_u32(4294967295,r0,0); '
         'gpu_warp_sync(4294967295); '
         'gpu_masked_store_u64(output,index,((r5 as u64)|((ballot as u64)<<32)) '
         '^ ((broadcast as u64)<<1) ^ ((any as u64)<<62) ^ ((all as u64)<<63),active);')
bodies = [arena + 'gpu_masked_store_u64(output,index,value+gpu_shared_load_u64(local^1),active);',
          start + 'let r0=value as u32; ' + warp,
          arena + 'let r0=gpu_shared_load_u64(local^1) as u32; ' + warp]
owners = ['dynamic_peer', 'warp_mix', 'dynamic_warp']

with tempfile.TemporaryDirectory() as directory:
    temp = Path(directory)
    source = temp / 'collectives.tv'
    source.write_text(header + '\n'.join(f'#[kernel] fn {owner}{signature} {{ {body} }}' for owner, body in zip(owners, bodies)))
    ir = temp / 'collectives.ll'
    run(TVC, '--emit-gpu-nvptx', source, '-o', ir)
    text = ir.read_text()
    entries = package.descriptors(text)
    assert [k['profile'] for k in entries] == ['cooperative-grid-v1', 'cooperative-warp-v1', 'cooperative-warp-v1']
    shared = {'parameter': 3, 'byte_scale': 8, 'max_count': 1024}
    assert [k['dynamic_shared_bytes'] for k in entries] == [shared, 0, shared]
    assert text.count('@__traveler_shared_dynamic = external addrspace(3) global [0 x i64]') == 1
    assert 'alloca' not in text
    run(OPT, '-passes=verify', '-disable-output', ir)
    run(LLC, '-mcpu=sm_90', ir, '-o', temp / 'collectives.ptx')
    ptx = (temp / 'collectives.ptx').read_text()
    for instruction in ('shfl.sync.bfly.b32', 'shfl.sync.idx.b32', 'vote.sync.ballot.b32', 'bar.warp.sync'):
        assert instruction in ptx, instruction
    native = text.replace('define ptx_kernel', 'define').replace('ptr addrspace(1)', 'ptr').replace('ptr addrspace(3)', 'ptr')
    native = native.replace('external addrspace(3) global [0 x i64]', 'global [1024 x i64] zeroinitializer')
    native = re.sub(r'addrspacecast ptr (%\w+) to ptr', r'getelementptr i8, ptr \1, i64 0', native)
    for kind in ('tid', 'ctaid', 'ntid', 'nctaid'):
        for axis in 'xyz':
            native = native.replace(f'llvm.nvvm.read.ptx.sreg.{kind}.{axis}', f'test_{kind}_{axis}')
    for old, new in [('barrier0', 'barrier'), ('bar.warp.sync', 'warp_sync'), ('shfl.sync.idx.i32', 'shuffle'),
                     ('shfl.sync.bfly.i32', 'shuffle_xor'), ('vote.ballot.sync', 'ballot'), ('vote.any.sync', 'any'), ('vote.all.sync', 'all')]:
        native = native.replace('llvm.nvvm.' + old, 'test_' + new)
    (temp / 'native.ll').write_text(native)
    run(OPT, '-passes=verify', '-disable-output', temp / 'native.ll')
    run(LLC, f'-mtriple={TRIPLE}', '-relocation-model=pic', '-filetype=obj', temp / 'native.ll', '-o', temp / 'native.o')
    run(LINK, '-shared', '-fPIC', '-pthread', '-Wall', '-Wextra', '-Werror', temp / 'native.o', HERE / 'shared_sim.c', '-o', temp / 'native.so')
    lib = ctypes.CDLL(str(temp / 'native.so'))
    simulate = lib.simulate_dynamic
    simulate.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint64, ctypes.c_uint64, ctypes.c_void_p]
    simulate.restype = None
    package.build(ir, temp / 'collectives.tvcp', LLC, SM if CUDA else 90)
    try:
        package.build(ir, temp / 'old.tvcp', LLC, 60)
    except ValueError:
        pass
    else:
        raise AssertionError('warp package admitted below SM70')
    run(TVC, HERE / 'cuda_collective_gate.tv', '--emit', 'obj', '-llc', LLC, '-o', temp / 'gate.o')
    run(LINK, '-no-pie', '-Wall', '-Wextra', '-Werror', temp / 'gate.o', HERE / 'cuda_driver_mock.c', '-o', temp / 'mock')
    run(TVC, HERE / 'cuda_collective_faults.tv', '--emit', 'obj', '-llc', LLC, '-o', temp / 'faults.o')
    run(LINK, '-no-pie', temp / 'faults.o', HERE / 'cuda_driver_mock.c', '-o', temp / 'faults')
    run(temp / 'faults', temp / 'collectives.tvcp')
    if CUDA:
        run(LINK, '-no-pie', temp / 'gate.o', CUDA, f'-Wl,-rpath,{Path(CUDA).parent}', '-o', temp / 'gate')
    rng = random.Random(314)
    mask = (1 << 64) - 1
    shapes = [((2, 1, 1), (32, 1, 1)), ((1, 2, 1), (8, 4, 2)), ((2, 1, 2), (4, 4, 2))]
    if CUDA:
        shapes.append(((1, 1, 1), (32, 8, 4)))
    for grid, block in shapes:
        count = math.prod(grid + block)
        values = [rng.getrandbits(64) for _ in range(count)]
        inp = (ctypes.c_uint64 * count)(*values)
        for capacity in (1, 19, 1024):
            for active in (0, 1, count - 1, count):
                for mode in range(3):
                    expected = values.copy()
                    for bz, by, bx in itertools.product(range(grid[2]), range(grid[1]), range(grid[0])):
                        indices = []
                        for tz, ty, tx in itertools.product(range(block[2]), range(block[1]), range(block[0])):
                            x, y, z = bx * block[0] + tx, by * block[1] + ty, bz * block[2] + tz
                            indices.append(x + grid[0] * block[0] * (y + grid[1] * block[1] * z))
                        live = [values[i] if i < active else 0 for i in indices]
                        peer = [live[j ^ 1] if (j ^ 1) < min(capacity, len(live)) else 0 for j in range(len(live))]
                        for local, index in enumerate(indices):
                            if index >= active:
                                continue
                            if mode == 0:
                                expected[index] = (live[local] + peer[local]) & mask
                            else:
                                base = local & ~31
                                registers = [v & 0xffffffff for v in (live if mode == 1 else peer)[base:base + 32]]
                                votes = sum(1 << lane for lane, i in enumerate(indices[base:base + 32]) if i < active)
                                expected[index] = (((sum(registers) & 0xffffffff) | (votes << 32)) ^ (registers[0] << 1)
                                                   ^ (int(votes != 0) << 62) ^ (int(votes == 0xffffffff) << 63)) & mask
                    if math.prod(block) <= 128:
                        out = (ctypes.c_uint64 * count)(*values)
                        simulate(getattr(lib, f'__traveler_kernel_{mode}'), inp if active else None,
                                 out if active else None, active, capacity, (ctypes.c_uint32 * 6)(*grid, *block))
                        assert list(out) == expected, (grid, block, active, capacity, mode)
                    (temp / 'input').write_bytes(bytes(inp))
                    for gate in ('mock', 'gate') if CUDA else ('mock',):
                        run(temp / gate, temp / 'collectives.tvcp', owners[mode], temp / 'input', temp / 'output',
                            *grid, *block, active, capacity, 0 if mode == 1 else capacity * 8)
                        assert (temp / 'output').read_bytes() == b''.join(v.to_bytes(8, 'little') for v in expected), (grid, block, active, capacity, mode)
    failures = ['gpu_shared_dynamic_init_u64(32);', 'gpu_shared_dynamic_init_u64(slots+1);',
                'gpu_shared_dynamic_init_u64(slots); gpu_shared_init_u64(32);',
                'gpu_warp_sync(0);', 'gpu_warp_sync(2147483647);',
                'if t.thread_x==0 {gpu_warp_sync(4294967295);}',
                'let x=gpu_warp_shuffle_u32(4294967295,1,32);',
                'let x=gpu_warp_shuffle_u32(4294967295,1,slots as u32);',
                'let x=gpu_warp_ballot(slots as u32,true);',
                'gpu_shared_init_u64(32); gpu_shared_store_u64(gpu_local_index(t),1); '
                'gpu_warp_sync(4294967295); let x=gpu_shared_load_u64(0);']
    for body in failures:
        bad = temp / 'bad.tv'
        bad.write_text(header + f'#[kernel] fn bad{signature} {{ {body} output[gpu_global_index(t)]=input[gpu_global_index(t)]; }}\n'
                       f'#[kernel] fn good{signature} {{ output[gpu_global_index(t)]=input[gpu_global_index(t)]; }}')
        target = temp / 'bad.ll'; target.write_text('previous artifact')
        run(TVC, '--emit-gpu-nvptx', bad, '-o', target, code=1)
        assert target.read_text() == 'previous artifact'
    image = (temp / 'collectives.tvcp').read_bytes()
    header_size = int.from_bytes(image[8:12], 'little')
    manifest = json.loads(image[12:12 + header_size])
    artifact = image[12 + header_size:]

    def refused_manifest(doc, ptx_bytes):
        encoded = json.dumps(doc, separators=(',', ':')).encode()
        path = temp / 'malformed.tvcp'
        path.write_bytes(image[:8] + len(encoded).to_bytes(4, 'little') + encoded + ptx_bytes)
        result = run(temp / 'mock', path, 'dynamic_peer', temp / 'input', temp / 'output',
                     2, 1, 1, 32, 1, 1, 1, 19, 152, code=1)
        assert result.stdout == '4\n2\n', result.stdout

    for value in ({'parameter': 0, 'byte_scale': 8, 'max_count': 1024}, {'parameter': 4, 'byte_scale': 8, 'max_count': 1024},
                  {'parameter': 3, 'byte_scale': 4, 'max_count': 1024}, {'parameter': 3, 'byte_scale': 8, 'max_count': 1025}):
        bad = copy.deepcopy(entries[0]); bad['dynamic_shared_bytes'] = value
        try:
            package.validate_kernel(bad)
        except ValueError:
            pass
        else:
            raise AssertionError(value)
        malformed = copy.deepcopy(manifest); malformed['kernels'][0] = bad
        refused_manifest(malformed, artifact)
    old_artifact = artifact.replace(f'.target sm_{SM if CUDA else 90}'.encode(), b'.target sm_60')
    old = copy.deepcopy(manifest); old['sm'] = 60
    old['ptx_bytes'] = len(old_artifact); old['ptx_sha256'] = hashlib.sha256(old_artifact).hexdigest()
    refused_manifest(old, old_artifact)
    print('dynamic/warp PASS: runtime-sized arenas, threaded shuffle/vote/sync, masks, phases, capability and schema refusals')
    if CUDA:
        print(f'dynamic/warp CUDA PASS: native SM{SM}, three kernels, four shapes, three arena sizes, partial/all-masked tiles')
