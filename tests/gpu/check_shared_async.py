"""Check async-copy phases, bounded footprints, and native shared visibility."""
import argparse
import hashlib
import json
from pathlib import Path
import struct
import sys
import tempfile

from check_ieee_bits import ROOT, cuda_package, run


def native_source():
    lines = [f'import "{ROOT}/tests/gpu/cuda_resident_checks.tv";',
             'fn main(argc: i32, argv: **u8) -> i32 { if argc != 2 { return 1; }',
             'let device: u64 = resident_need(cuda_device_open(0)); print(resident_need(cuda_device_sm(device)));',
             'let module: u64 = resident_need(cuda_module_load(device, argv[1]));',
             'let kernel: u64 = resident_need(cuda_kernel_get_owner(module, "shared_async"));',
             'let input: *u64 = alloc(129); let output: *u64 = alloc(130);',
             'var j: u64 = 0; while j < 129 { input[j] = j * 13 + 7; j = j + 1; }',
             'let bi: u64 = resident_need(cuda_buffer_alloc(device, 1032));',
             'let bo: u64 = resident_need(cuda_buffer_alloc(device, 1040));',
             'let vi: CudaArgument = resident_typed_view(bi, 0, 1032, 0 - 64);',
             'let vo: CudaArgument = resident_typed_view(bo, 0, 1040, 0 - 64);',
             'let slice: CudaArgument = resident_typed_view(bo, 8, 1024, 0 - 64);',
             'resident_need(cuda_upload(vi, input as *u8, 1032));',
             'let args: *CudaArgument = alloc(3); resident_bind(kernel, args, "input", vi);',
             'resident_bind(kernel, args, "output", slice);']
    expected = []
    for case, (count, block) in enumerate((c, b) for c in (0, 1, 33, 127, 129) for b in (32, 64, 128)):
        lines += [f'resident_bind(kernel, args, "count", cuda_arg_u64({count}));',
                  'j = 0; while j < 130 { output[j] = 123456789; j = j + 1; }',
                  'resident_need(cuda_upload(vo, output as *u8, 1040));',
                  f'let geometry{case}: CudaGeometry = CudaGeometry {{ grid_x: {128//block}, grid_y: 1, grid_z: 1, block_x: {block}, block_y: 1, block_z: 1, shared_bytes: 0 }};',
                  f'resident_need(cuda_launch_grid_sync(kernel, geometry{case}, args, 3));',
                  'resident_need(cuda_download(output as *u8, vo, 1040));',
                  'j = 0; while j < 130 { print(output[j]); j = j + 1; }']
        expected += [123456789] + [(((i ^ 1)*13+7 if (i ^ 1) < count else 0) + ((i+1)*13+7 if i+1 < count else 0)) if i % block < 64 else 0 for i in range(128)] + [123456789]
    lines += ['resident_need(cuda_buffer_close(bi)); resident_need(cuda_buffer_close(bo));',
              'resident_need(cuda_module_close(module)); resident_need(cuda_device_close(device));',
              'free(args); free(input); free(output); return 0; }']
    return '\n'.join(lines), expected


def main():
    p = argparse.ArgumentParser()
    for name in ('tvc', 'llc', 'link'): p.add_argument(name)
    p.add_argument('--opt'); p.add_argument('--cuda'); a = p.parse_args()
    with tempfile.TemporaryDirectory(prefix='traveler-shared-async-') as directory:
        d = Path(directory)
        source = (ROOT/'tests/gpu/cuda_shared_async.tv').read_text().replace(
            '../../src/lib/gpu/shared.tv', str(ROOT/'src/lib/gpu/shared.tv'))
        (d/'source.tv').write_text(source)
        run(a.tvc, d/'source.tv', '--emit-gpu-nvptx', '-o', d/'source.ll')
        entry = cuda_package.descriptors((d/'source.ll').read_text())[0]
        assert entry['requires'] == ['async-shared-u64-v1']
        assert 'tensor' not in entry and 'numerical' not in entry
        assert cuda_package.kernel_requirements(entry) == (80, (7, 0))
        assert any('count_parameter' in v.get('footprint', {}) for v in entry['parameters'])
        for sm in (80, 90, 120): cuda_package.build(d/'source.ll', d/f'sm{sm}.tvcp', a.llc, sm)
        dynamic = source.replace('output: *u64)', 'output: *u64, capacity: u64)').replace(
            'gpu_shared_init_u64(64)', 'gpu_shared_dynamic_init_u64(capacity)')
        (d/'dynamic.tv').write_text(dynamic)
        run(a.tvc, d/'dynamic.tv', '--emit-gpu-nvptx', '-o', d/'dynamic.ll')
        cuda_package.build(d/'dynamic.ll', d/'dynamic.tvcp', a.llc, 80)
        if a.opt:
            run(a.opt, '-passes=default<O1>', '-S', d/'source.ll', '-o', d/'optimized.ll')
            run(a.llc, '-mtriple=nvptx64-nvidia-cuda', '-mcpu=sm_80', d/'optimized.ll', '-o', d/'optimized.ptx')
            text = (d/'optimized.ptx').read_text()
            for op in ('cp.async.ca.shared.global', 'cp.async.commit_group', 'cp.async.wait_group'):
                assert text.count(op) == 2, op
        issue = 'gpu_shared_async_copy_u64(input, count, i);'
        mutations = {
            'no-init': source.replace('gpu_shared_init_u64(64);', ''),
            'no-commit': source.replace('gpu_shared_async_commit();', '', 1),
            'no-wait': source.replace('gpu_shared_async_wait();', '', 1),
            'no-barrier': source.replace('gpu_shared_async_wait();\n    gpu_block_barrier();', 'gpu_shared_async_wait();', 1),
            'early-barrier': source.replace(issue, issue+' gpu_block_barrier();'),
            'duplicate-issue': source.replace(issue, issue+issue),
            'duplicate-commit': source.replace('gpu_shared_async_commit();', 'gpu_shared_async_commit(); gpu_shared_async_commit();', 1),
            'early-read': source.replace(issue, issue+' let bad: u64 = gpu_shared_load_u64(0);'),
            'early-store': source.replace(issue, issue+' gpu_shared_store_u64(local, 9);'),
            'read-reuse': source.replace('let first: u64 = gpu_shared_load_u64(local ^ 1);\n    gpu_block_barrier();', 'let first: u64 = gpu_shared_load_u64(local ^ 1);'),
            'unfinished': source[:source.index('    gpu_shared_async_commit();')]+'output[i] = 0; }\n',
            'unissued': source.replace(issue, ''),
        }
        for name, text in mutations.items():
            (d/f'{name}.tv').write_text(text)
            result = run(a.tvc, d/f'{name}.tv', '--emit-gpu-nvptx', '-o', d/f'{name}.ll', ok=False)
            assert '"status":"refused"' in result.stdout+result.stderr, (name, result)
            assert not (d/f'{name}.ll').exists() or cuda_package.PREFIX not in (d/f'{name}.ll').read_text(), name
        if a.cuda:
            text, expected = native_source(); (d/'native.tv').write_text(text)
            run(a.tvc, d/'native.tv', '-o', d/'native.ll')
            run(a.llc, '-filetype=obj', d/'native.ll', '-o', d/'native.o')
            run(a.link, '-no-pie', d/'native.o', a.cuda, f'-Wl,-rpath,{Path(a.cuda).resolve().parent}', '-o', d/'native')
            for name in ('sm80', 'sm90', 'sm120'):
                device, _, output = run(d/'native', d/f'{name}.tvcp').stdout.partition('\n')
                actual = [int(v) for v in output.splitlines()]
                assert actual == expected, (len(actual), len(expected))
                print(f'Async shared SM{device}, {name}: 15 launches, {len(expected)} output/guard checks PASS')
        print('Async shared portable: phases, reuse, bounded footprints, SM80/90/120 and O1 lowering PASS')


if __name__ == '__main__': main()
