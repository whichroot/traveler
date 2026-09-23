"""Check bounded contended atomic adds and launch footprint admission."""
from collections import Counter
import copy
import ctypes
import hashlib
import importlib.util
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
spec = importlib.util.spec_from_file_location('package', ROOT / 'tools/cuda_package.py')
package = importlib.util.module_from_spec(spec)
spec.loader.exec_module(package)
TRIPLE = re.search(r'Default target: (\S+)', subprocess.check_output([LLC, '--version'], text=True))[1]


def run(*args, code=0):
    result = subprocess.run([str(a) for a in args], capture_output=True, text=True, timeout=90)
    assert result.returncode == code, (args, result.returncode, result.stdout, result.stderr)
    return result


header = f'import "{ROOT / "src/lib/gpu/thread.tv"}"; import "{ROOT / "src/lib/gpu/atomic.tv"}";\n'
signature = '(t:GpuThread,buffer:*u{bits},output:*u64,count:u64,modulus:u64)'


def check_result(counters, output, bits, count, modulus, lanes, launches, operation=0):
    mask = (1 << bits) - 1
    initial = mask - 15
    for bucket in range(count):
        indices = [i for i in range(lanes) if i % modulus == bucket]
        if operation != 0:
            values = [i + 1 for i in indices]
            old = [output[i] for i in indices]
            if not indices:
                assert counters[bucket] == initial
            elif operation == 1:
                remainder = Counter(old + [counters[bucket]])
                remainder.subtract(values)
                assert all(v >= 0 for v in remainder.values()) and sum(remainder.values()) == 1
                start = next(k for k, v in remainder.items() if v)
                assert start == initial if launches == 1 else start in values
                assert counters[bucket] in values
            else:
                assert counters[bucket] in values
                assert old.count(initial) == (1 if launches == 1 else 0)
                assert all(v in (initial, counters[bucket]) for v in old)
            continue
        assert counters[bucket] == (initial + len(indices) * launches) & mask
        expected = sorted((initial + len(indices) * (launches - 1) + i) & mask for i in range(len(indices)))
        assert sorted(output[i] for i in indices) == expected, (bits, count, bucket)
    assert all(output[i] == 0 for i in range(lanes) if i % modulus >= count)


with tempfile.TemporaryDirectory() as directory:
    temp = Path(directory)
    source = temp / 'atomic.tv'
    bodies = []
    for operation, name in enumerate(('add', 'exchange', 'compare_exchange')):
        for bits, order in itertools.product((32, 64), range(4)):
            value = '1' if operation == 0 else f'(gpu_global_index(t)+1) as u{bits}'
            expected = f', {(1 << bits) - 16}' if operation == 2 else ''
            body = (f'let old=gpu_atomic_{name}_u{bits}(buffer,count,gpu_global_index(t)%modulus,{value},{order},1{expected}); '
                    'output[gpu_global_index(t)]=old as u64;')
            bodies.append(f'#[kernel] fn atomic_{name}_{bits}_{order}{signature.format(bits=bits)} {{ {body} }}')
    source.write_text(header + '\n'.join(bodies))
    ir = temp / 'atomic.ll'
    run(TVC, '--emit-gpu-nvptx', source, '-o', ir)
    text = ir.read_text()
    entries = package.descriptors(text)
    assert len(entries) == 24
    for i, entry in enumerate(entries):
        assert entry['parameters'][0]['footprint'] == {'count_parameter': 2, 'byte_scale': 4 if i % 8 < 4 else 8}
        assert entry['parameters'][0]['access'] == 'read-write'
        assert entry['disjoint'] == [[0, 1]]
    run(OPT, '-passes=verify', '-disable-output', ir)
    for sm in (70, 90):
        run(LLC, f'-mcpu=sm_{sm}', ir, '-o', temp / 'atomic.ptx')
        ptx = (temp / 'atomic.ptx').read_text()
        for order in ('relaxed', 'acquire', 'release', 'acq_rel'):
            for bits in (32, 64):
                assert re.search(rf'atom\.{order}\.gpu\.global\.add\.u{bits}', ptx), (order, bits)
                for operation in ('exch', 'cas'):
                    assert re.search(rf'atom\.{order}\.gpu\.global\.{operation}\.b{bits}', ptx)
    native = text.replace('define ptx_kernel', 'define').replace('ptr addrspace(1)', 'ptr')
    def host_atomic(match):
        result, bits, order, operation, pointer, value, replacement = match.groups()
        order = 'monotonic' if order == 'relaxed' else order
        if operation == 'cas':
            failure = 'acquire' if order in ('acquire', 'acq_rel') else 'monotonic'
            return (f'{result}pair = cmpxchg ptr {pointer}, i{bits} {value}, i{bits} {replacement} {order} {failure}, align {int(bits)//8}\n'
                    f'  {result} = extractvalue {{i{bits}, i1}} {result}pair, 0')
        operation = 'xchg' if operation == 'exch' else 'add'
        return f'{result} = atomicrmw {operation} ptr {pointer}, i{bits} {value} {order}, align {int(bits)//8}'
    native = re.sub(r'(%\w+) = call i(32|64) asm sideeffect "atom\.(\w+)\.gpu\.global\.(add|exch|cas)\.[ub]\d+[^\n]+"\(ptr (%\w+), i\d+ (%\w+)(?:, i\d+ (%\w+))?\)', host_atomic, native)
    native = re.sub(r'addrspacecast ptr (%\w+) to ptr', r'getelementptr i8, ptr \1, i64 0', native)
    for kind in ('tid', 'ctaid', 'ntid', 'nctaid'):
        for axis in 'xyz':
            native = native.replace(f'llvm.nvvm.read.ptx.sreg.{kind}.{axis}', f'test_{kind}_{axis}')
    (temp / 'native.ll').write_text(native)
    run(LLC, f'-mtriple={TRIPLE}', '-relocation-model=pic', '-filetype=obj', temp / 'native.ll', '-o', temp / 'native.o')
    run(LINK, '-shared', '-fPIC', '-pthread', '-Wall', '-Wextra', '-Werror', temp / 'native.o', HERE / 'shared_sim.c', '-o', temp / 'native.so')
    lib = ctypes.CDLL(str(temp / 'native.so'))
    simulate = lib.simulate_dynamic
    simulate.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint64, ctypes.c_uint64, ctypes.c_void_p]
    simulate.restype = None
    package.build(ir, temp / 'atomic.tvcp', LLC, SM if CUDA else 90)
    try:
        package.build(ir, temp / 'old.tvcp', LLC, 60)
    except ValueError:
        pass
    else:
        raise AssertionError('atomic package admitted below SM70')
    run(TVC, HERE / 'cuda_atomic_gate.tv', '--emit', 'obj', '-llc', LLC, '-o', temp / 'gate.o')
    run(LINK, '-no-pie', '-Wall', '-Wextra', '-Werror', temp / 'gate.o', HERE / 'cuda_driver_mock.c', '-o', temp / 'mock')
    if CUDA:
        run(LINK, '-no-pie', temp / 'gate.o', CUDA, f'-Wl,-rpath,{Path(CUDA).parent}', '-o', temp / 'gate')
    shapes = [((3, 1, 1), (31, 1, 1)), ((2, 2, 1), (4, 3, 2)), ((1, 2, 2), (8, 4, 2))]
    if CUDA:
        shapes.append(((2, 1, 1), (32, 8, 4)))
    for grid, block in shapes:
        lanes = math.prod(grid + block)
        for count in (0, 1, 3, 19):
            for index, entry in enumerate(entries):
                bits = 32 if index % 8 < 4 else 64
                operation = index // 8
                ctype = ctypes.c_uint32 if bits == 32 else ctypes.c_uint64
                modulus = max(1, count + 1)
                if math.prod(block) < 128:
                    counters = (ctype * count)(*([(1 << bits) - 16] * count))
                    out = (ctypes.c_uint64 * lanes)()
                    simulate(getattr(lib, entry['symbol']), counters if count else None, out, count, modulus,
                             (ctypes.c_uint32 * 6)(*grid, *block))
                    check_result(list(counters), list(out), bits, count, modulus, lanes, 1, operation)
                for gate in ('mock', 'gate') if CUDA else ('mock',):
                    run(temp / gate, temp / 'atomic.tvcp', entry['owner'], temp / 'result', bits, count, modulus, *grid, *block)
                    data = (temp / 'result').read_bytes()
                    width = bits // 8
                    end = 16 + count * width
                    offset = 16 + ((count * width + 7) // 8) * 8
                    assert data[:16] == b'\xa5' * 16 and data[end:offset] == b'\xa5' * (offset - end)
                    assert data[-16:] == b'\xa5' * 16
                    counters = [int.from_bytes(data[16+i*width:16+(i+1)*width], 'little') for i in range(count)]
                    output = [int.from_bytes(data[offset+i*8:offset+(i+1)*8], 'little') for i in range(lanes)]
                    check_result(counters, output, bits, count, modulus, lanes, 2, operation)
    good = 'let old=gpu_atomic_add_u64(buffer,count,0,1,0,1); output[gpu_global_index(t)]=old;'
    failures = [good.replace(',0,1);', ',4,1);'), good.replace(',0,1);', ',0,0);'),
                good.replace(',0,1);', ',0,2);'), good.replace('buffer,count,', 'buffer,1,'),
                good.replace('buffer,count,', 'buffer,count+1,'),
                good.replace('let old=', 'let read=buffer[gpu_global_index(t)]; let old='),
                good.replace('output[', 'let other=gpu_atomic_add_u64(buffer,modulus,0,1,0,1); output['),
                good.replace(',0,1);', ',count as i32,1);'),
                good.replace(',0,1);', ',0,18446744073709551617);'),
                good.replace('output[', 'let read=buffer[gpu_global_index(t)]; output['),
                'if t.thread_x==0 {' + good + '}']
    for body in failures:
        source.write_text(header + f'#[kernel] fn bad{signature.format(bits=64)} {{ {body} }}\n' + bodies[0])
        target = temp / 'bad.ll'; target.write_text('previous')
        run(TVC, '--emit-gpu-nvptx', source, '-o', target, code=1)
        assert target.read_text() == 'previous'
    source.write_text(header + '#[kernel] fn discarded(t:GpuThread,buffer:*u64,count:u64) { gpu_atomic_add_u64(buffer,count,0,1,0,1); }')
    run(TVC, '--emit-gpu-nvptx', source, '-o', temp / 'discarded.ll')
    package.descriptors((temp / 'discarded.ll').read_text())
    run(OPT, '-passes=default<O2>', '-S', temp / 'discarded.ll', '-o', temp / 'discarded-opt.ll')
    assert 'asm sideeffect "atom.relaxed.gpu.global.add.u64' in (temp / 'discarded-opt.ll').read_text()
    run(TVC, HERE / 'cuda_package_gate.tv', '--emit', 'obj', '-llc', LLC, '-o', temp / 'loader.o')
    run(LINK, '-no-pie', temp / 'loader.o', '-o', temp / 'loader')
    image = (temp / 'atomic.tvcp').read_bytes()
    length = int.from_bytes(image[8:12], 'little')
    manifest = json.loads(image[12:12+length]); artifact = image[12+length:]
    run(temp / 'loader', temp / 'atomic.tvcp')
    for slot in (-1, 0, 1, 4, 5, 99, True):
        bad = copy.deepcopy(manifest)
        bad['kernels'][0]['parameters'][0]['footprint']['count_parameter'] = slot
        try:
            package.validate_kernel(bad['kernels'][0])
        except ValueError:
            pass
        else:
            raise AssertionError(slot)
        encoded = json.dumps(bad).encode()
        (temp / 'bad.tvcp').write_bytes(image[:8] + len(encoded).to_bytes(4, 'little') + encoded + artifact)
        assert subprocess.run([str(temp / 'loader'), str(temp / 'bad.tvcp')], capture_output=True).returncode != 0
    bad = copy.deepcopy(manifest)
    old_ptx = re.sub(rb'(?m)^\.target sm_\d+', b'.target sm_60', artifact)
    bad['sm'] = 60; bad['ptx_bytes'] = len(old_ptx); bad['ptx_sha256'] = hashlib.sha256(old_ptx).hexdigest()
    encoded = json.dumps(bad).encode()
    (temp / 'old.tvcp').write_bytes(image[:8] + len(encoded).to_bytes(4, 'little') + encoded + old_ptx)
    assert subprocess.run([str(temp / 'loader'), str(temp / 'old.tvcp')], capture_output=True).returncode != 0
    print('bounded atomics PASS: u32/u64 add/exchange/CAS, four orders, device scope, contention, overflow, bounds and alias refusals')
    if CUDA:
        print(f'bounded atomics CUDA PASS: native SM{SM}, 24 kernels, four shapes, small/empty buffers and repeated launches')
