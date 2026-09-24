"""Check opt-in MMA fragments, closed admission, and exact finite native cases."""
import argparse
import copy
import hashlib
import json
from pathlib import Path
import random
import struct
import tempfile

from check_ieee_bits import ROOT, cuda_package, run


def bits(value, fmt):
    if fmt == 'half':
        return int.from_bytes(struct.pack('<e', value), 'little')
    return int.from_bytes(struct.pack('<f', value), 'little') >> 16


def f32(value):
    return int.from_bytes(struct.pack('<f', value), 'little')


def native_source():
    rng = random.Random(5016)
    lines = [f'import "{ROOT}/tests/gpu/cuda_resident_checks.tv";']
    expected = []
    for fmt in ('half', 'bfloat'):
        values = {name: [] for name in ('al', 'ah', 'b', 'cl', 'ch', 'dl', 'dh')}
        for tile in range(4):
            a = [[rng.randrange(-4, 5) for _ in range(16)] for _ in range(16)]
            b = [[rng.randrange(-4, 5) for _ in range(8)] for _ in range(16)]
            c = [[rng.randrange(1, 17) for _ in range(8)] for _ in range(16)]
            if tile < 2:
                a = [[int(k == (r + tile * 7) % 16) for k in range(16)] for r in range(16)]
            d = [[c[r][s] + sum(a[r][k]*b[k][s] for k in range(16)) for s in range(8)] for r in range(16)]
            for lane in range(32):
                g, t = divmod(lane, 4)
                av = [a[g + (8 if j % 4 >= 2 else 0)][t*2 + j % 2 + (8 if j >= 4 else 0)] for j in range(8)]
                bv = [b[t*2 + j % 2 + (8 if j >= 2 else 0)][g] for j in range(4)]
                def pack(xs, width):
                    return sum(x << (width*j) for j, x in enumerate(xs))
                values['al'].append(pack([bits(v, fmt) for v in av[:4]], 16))
                values['ah'].append(pack([bits(v, fmt) for v in av[4:]], 16))
                values['b'].append(pack([bits(v, fmt) for v in bv], 16))
                for matrix, low, high in ((c, 'cl', 'ch'), (d, 'dl', 'dh')):
                    fragment = [f32(matrix[g + (8 if j >= 2 else 0)][2*t+j % 2]) for j in range(4)]
                    values[low].append(pack(fragment[:2], 32))
                    values[high].append(pack(fragment[2:], 32))
        lines.append(f'fn case_{fmt}(device: u64, module: u64) {{')
        lines.append(f'let kernel: u64 = resident_need(cuda_kernel_get_owner(module, "native_{fmt}"));')
        lines.append('let args: *CudaArgument = alloc(7);')
        for name, data in values.items():
            lines += [f'let {name}: *u64 = alloc(130);',
                      f'let buffer_{name}: u64 = resident_need(cuda_buffer_alloc(device, 1040));',
                      f'let view_{name}: CudaArgument = resident_typed_view(buffer_{name}, 0, 1040, 0 - 64);']
            for i, value in enumerate([123456789] + (data if name[0] != 'd' else [987654321]*128) + [123456789]):
                lines.append(f'{name}[{i}] = {value};')
            lines += [f'resident_need(cuda_upload(view_{name}, {name} as *u8, 1040));',
                      f'let slice_{name}: CudaArgument = resident_typed_view(buffer_{name}, 8, 1024, 0 - 64);',
                      f'resident_bind(kernel, args, "{name}", slice_{name});']
        for block in (32, 64):
            lines += [f'let geometry{block}: CudaGeometry = CudaGeometry {{ grid_x: {128//block}, grid_y: 1, grid_z: 1, block_x: {block}, block_y: 1, block_z: 1, shared_bytes: 0 }};',
                      f'resident_need(cuda_launch_grid_sync(kernel, geometry{block}, args, 7));']
            for name in ('dl', 'dh'):
                lines += [f'resident_need(cuda_download({name} as *u8, view_{name}, 1040));',
                          f'var i_{name}{block}: i32 = 0; while i_{name}{block} < 130 {{ print({name}[i_{name}{block}]); i_{name}{block} = i_{name}{block} + 1; }}']
                expected.extend([123456789] + values[name] + [123456789])
        lines += ['let invalid: CudaGeometry = CudaGeometry { grid_x: 1, grid_y: 1, grid_z: 1, block_x: 31, block_y: 1, block_z: 1, shared_bytes: 0 };',
                  'resident_refused(cuda_launch_grid_sync(kernel, invalid, args, 7));']
        for name in values:
            lines.append(f'resident_need(cuda_buffer_close(buffer_{name})); free({name});')
        lines.append('free(args); }')
    lines += ['fn main(argc: i32, argv: **u8) -> i32 { if argc != 2 { return 72; }',
              'let device: u64 = resident_need(cuda_device_open(0)); print(resident_need(cuda_device_sm(device)));',
              'let module: u64 = resident_need(cuda_module_load(device, argv[1]));',
              'case_half(device, module); case_bfloat(device, module);',
              'resident_need(cuda_module_close(module)); resident_need(cuda_device_close(device)); return 0; }']
    return '\n'.join(lines), expected


def main():
    p = argparse.ArgumentParser()
    p.add_argument('tvc'); p.add_argument('llc'); p.add_argument('link'); p.add_argument('--cuda'); p.add_argument('--opt')
    a = p.parse_args()
    with tempfile.TemporaryDirectory(prefix='traveler-native-tensor-') as directory:
        d = Path(directory)
        run(a.tvc, ROOT/'tests/gpu/cuda_native_tensor.tv', '--emit-gpu-nvptx', '-o', d/'device.ll')
        entries = cuda_package.descriptors((d/'device.ll').read_text())
        assert len(entries) == 2
        mixed = (ROOT/'tests/gpu/cuda_native_tensor.tv').read_text().replace(
            'import "../../src/lib/gpu/tensor.tv";', f'import "{ROOT}/src/lib/gpu/tensor.tv";').replace(
            'dl[i] = d as u64;', 'dl[i] = ieee32_add(d as u32, 0) as u64;')
        (d/'mixed.tv').write_text(mixed)
        run(a.tvc, d/'mixed.tv', '--emit-gpu-nvptx', '-o', d/'mixed.ll')
        for entry in cuda_package.descriptors((d/'mixed.ll').read_text()):
            assert entry['numerical'] == 'ieee-bits-rne-v1'
            assert entry['tensor'] == 'cuda-mma-native-v1'
            assert entry['requires'][0] == 'ieee32-rne-v1'
        for entry, fmt in zip(entries, ('f16', 'bf16')):
            assert entry['tensor'] == 'cuda-mma-native-v1' and 'numerical' not in entry
            assert entry['requires'] == ['warp-full32-v1', f'mma-{fmt}-m16n8k16-native-v1']
            assert cuda_package.kernel_requirements(entry) == (80, (7, 0))
        for sm in (80, 90, 120):
            cuda_package.build(d/'device.ll', d/f'sm{sm}.tvcp', a.llc, sm)
        assert (d/'device.ll').read_text().count('convergent noduplicate') == 2
        if a.opt:
            run(a.opt, '-passes=default<O1>', '-S', d/'device.ll', '-o', d/'optimized.ll')
            run(a.llc, '-mtriple=nvptx64-nvidia-cuda', '-mcpu=sm_80', d/'optimized.ll', '-o', d/'optimized.ptx')
            assert (d/'optimized.ptx').read_text().count('mma.sync.aligned.m16n8k16') == 2
        try:
            cuda_package.build(d/'device.ll', d/'bad.tvcp', a.llc, 75)
        except ValueError:
            pass
        else:
            raise AssertionError('SM75 accepted')
        run(a.tvc, ROOT/'tests/gpu/cuda_package_gate.tv', '-o', d/'gate.ll')
        run(a.llc, '-filetype=obj', d/'gate.ll', '-o', d/'gate.o')
        run(a.link, '-no-pie', d/'gate.o', '-o', d/'gate')
        run(d/'gate', d/'sm80.tvcp')
        blob = (d/'sm80.tvcp').read_bytes()
        length = struct.unpack('<I', blob[8:12])[0]
        original = json.loads(blob[12:12+length]); ptx = blob[12+length:]
        assert b'mma.sync.aligned.m16n8k16.row.col.f32.f16.f16.f32' in ptx
        assert b'mma.sync.aligned.m16n8k16.row.col.f32.bf16.bf16.f32' in ptx
        for change in ('missing-policy', 'unknown-policy', 'missing-warp', 'missing-mma', 'duplicate', 'wrong-profile'):
            header = copy.deepcopy(original); k = header['kernels'][0]
            if change == 'missing-policy': k.pop('tensor')
            if change == 'unknown-policy': k['tensor'] = 'ieee-bits-rne-v1'
            if change == 'missing-warp': k['requires'].pop(0)
            if change == 'missing-mma': k['requires'].pop()
            if change == 'duplicate': k['requires'].append(k['requires'][-1])
            if change == 'wrong-profile': k['profile'] = 'cooperative-grid-v1'
            try:
                cuda_package.validate_kernel(k)
            except ValueError:
                pass
            else:
                raise AssertionError(change)
            encoded = json.dumps(header, separators=(',', ':')).encode()
            (d/'bad.tvcp').write_bytes(blob[:8]+struct.pack('<I', len(encoded))+encoded+ptx)
            assert run(d/'gate', d/'bad.tvcp', ok=False).returncode != 0, change
        for field, old, new in [('sm', b'.target sm_80', b'.target sm_75'),
                                ('ptx_major', f'.version {original["ptx_major"]}.{original["ptx_minor"]}'.encode(), b'.version 6.0')]:
            header = copy.deepcopy(original); text = ptx.replace(old, new)
            assert text != ptx
            header[field] = 75 if field == 'sm' else 6
            if field == 'ptx_major': header['ptx_minor'] = 0
            header.update(ptx_bytes=len(text), ptx_sha256=hashlib.sha256(text).hexdigest())
            encoded = json.dumps(header, separators=(',', ':')).encode()
            (d/'bad.tvcp').write_bytes(blob[:8]+struct.pack('<I', len(encoded))+encoded+text)
            assert run(d/'gate', d/'bad.tvcp', ok=False).returncode != 0, field
        prefix = f'import "{ROOT}/src/lib/gpu/tensor.tv";\n'
        for name, source in {
            'independent': '#[kernel] fn bad(i: i32, o: *u64) { o[i] = gpu_native_mma_f16_m16n8k16(0 as u128, 0 as u64, 0 as u128) as u64; }',
            'helper': 'fn helper() -> u128 { return gpu_native_mma_f16_m16n8k16(0 as u128, 0 as u64, 0 as u128); } #[kernel] fn bad(t: GpuThread, o: *u64) { o[gpu_global_index(t)] = helper() as u64; }',
            'type': '#[kernel] fn bad(t: GpuThread, o: *u64) { o[gpu_global_index(t)] = gpu_native_mma_f16_m16n8k16(0 as u64, 0 as u64, 0 as u128) as u64; }',
            'branch': '#[kernel] fn bad(t: GpuThread, o: *u64) { if gpu_global_index(t) == 0 { let x: u128 = gpu_native_mma_f16_m16n8k16(0 as u128, 0 as u64, 0 as u128); o[gpu_global_index(t)] = x as u64; } }',
        }.items():
            (d/f'{name}.tv').write_text(prefix+source)
            result = run(a.tvc, d/f'{name}.tv', '--emit-gpu-nvptx', '-o', d/f'{name}.ll', ok=False)
            assert '"status":"refused"' in result.stdout + result.stderr, (name, result.stdout, result.stderr)
            assert not (d/f'{name}.ll').exists() or cuda_package.PREFIX not in (d/f'{name}.ll').read_text(), name
        run(a.tvc, ROOT/'tests/gpu/cuda_native_tensor.tv', '--emit-gpu', '-o', d/'amd.ll', ok=False)
        assert not (d/'amd.ll').exists() or 'mma.sync' not in (d/'amd.ll').read_text()
        if a.cuda:
            source, expected = native_source()
            (d/'native.tv').write_text(source)
            run(a.tvc, d/'native.tv', '-o', d/'native.ll')
            run(a.llc, '-filetype=obj', d/'native.ll', '-o', d/'native.o')
            run(a.link, '-no-pie', d/'native.o', a.cuda, f'-Wl,-rpath,{Path(a.cuda).resolve().parent}', '-o', d/'native')
            for sm in (80, 90, 120):
                device, _, output = run(d/'native', d/f'sm{sm}.tvcp').stdout.partition('\n')
                actual = [int(v) & ((1 << 64)-1) for v in output.splitlines()]
                assert len(actual) == len(expected)
                assert actual == expected, next((i, x, y) for i, (x, y) in enumerate(zip(actual, expected)) if x != y)
                print(f'Native tensor SM{device}, PTX SM{sm}: {len(expected)} exact finite packed output/guard checks PASS')
        print('Native tensor portable: SM80/90/120 lowering, opt-in metadata, closed admission and participation refusals PASS')


if __name__ == '__main__':
    main()
