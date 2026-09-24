"""Check packed binary16/bfloat16 projections with two ordered f32 accumulators."""
import argparse
from pathlib import Path
import random
import tempfile

from check_ieee_bits import ROOT, cuda_package, oracle, run
from check_low_precision import half_widen, bfloat_widen
from check_numeric_projection import SENTINEL


def generate(native):
    rng = random.Random(5036)
    lines = [f'import "{ROOT}/tests/gpu/cuda_low_projection.tv";']
    if native:
        lines.append(f'import "{ROOT}/tests/gpu/cuda_resident_checks.tv";')
    expected, count = [], 0
    for fmt, widen, infinity in [(0, half_widen, 0x7C00), (1, bfloat_widen, 0x7F80)]:
        for rows, tokens, columns in [(1, 1, 1), (5, 3, 3), (17, 4, 8)]:
            case = count; count += 1
            cells = rows*tokens; capacity = ((cells+31)//32)*32+2
            weights = [[rng.getrandbits(16) for _ in range(rows*8)] for _ in range(2)]
            inputs = [rng.getrandbits(16) for _ in range(tokens*8)]
            for row in range(rows):
                for k in range(columns, 8):
                    weights[0][row*8+k] = weights[1][row*8+k] = infinity+1
            for token in range(tokens):
                for k in range(columns, 8):
                    inputs[token*8+k] = infinity
            def pack(values):
                return [sum(values[i+j] << (j*16) for j in range(4)) for i in range(0, len(values), 4)]
            packed = {'w0': pack(weights[0]), 'w1': pack(weights[1]), 'input': pack(inputs)}
            lines.append(f'fn case{case}(device: u64, kernel: u64) {{' if native else f'fn case{case}() {{')
            for name, values in packed.items():
                lines.append(f'let {name}: *u64 = alloc({len(values)});')
                lines.extend(f'{name}[{i}] = {value};' for i, value in enumerate(values))
            lines.append(f'let output: *u64 = alloc({capacity});')
            if native:
                for name, size in [('w0', rows*2), ('w1', rows*2), ('input', tokens*2), ('output', capacity)]:
                    lines += [f'let b_{name}: u64 = resident_need(cuda_buffer_alloc(device, {size*8}));',
                              f'let v_{name}: CudaArgument = resident_typed_view(b_{name}, 0, {size*8}, 0 - 64);']
                    if name != 'output':
                        lines.append(f'resident_need(cuda_upload(v_{name}, {name} as *u8, {size*8}));')
                lines.append('let args: *CudaArgument = alloc(10);')
                for name, value in [('weights_count', rows*2), ('input_count', tokens*2), ('rows', rows), ('tokens', tokens), ('columns', columns), ('format', fmt)]:
                    lines.append(f'resident_bind(kernel, args, "{name}", cuda_arg_u64({value}));')
                lines.append('resident_bind(kernel, args, "input", v_input);')
            for turn, block in enumerate((7, 32)):
                lanes = ((cells+block-1)//block)*block
                bias = 0xBF800000 if turn == 0 else 0x3F800000
                lines += [f'if low_projection_valid({rows}, {tokens}, {columns}, {rows*2}, {tokens*2}, {fmt}) {{ }} else {{ exit(51); }}',
                          f'var j{turn}: i32 = 0; while j{turn} < {capacity} {{ output[j{turn}] = {SENTINEL}; j{turn} = j{turn} + 1; }}']
                values = [SENTINEL]*capacity
                for i in range(lanes):
                    result = 0
                    if i < cells:
                        token, row = divmod(i, rows)
                        accumulators = [bias, 0]
                        for k in range(columns):
                            accumulators[k % 2] = oracle('fma', 32, 32, (widen(weights[turn][row*8+k]), widen(inputs[token*8+k]), accumulators[k % 2]))
                        result = oracle('add', 32, 32, accumulators)
                    values[i+1] = result
                expected.extend(values)
                if native:
                    lines += [f'resident_need(cuda_upload(v_output, output as *u8, {capacity*8}));',
                              f'resident_bind(kernel, args, "weights", v_w{turn});',
                              f'resident_bind(kernel, args, "bias", cuda_arg_u64({bias}));',
                              f'let slice{turn}: CudaArgument = resident_typed_view(b_output, 8, {lanes*8}, 0 - 64);',
                              f'resident_bind(kernel, args, "output", slice{turn});',
                              f'let geometry{turn}: CudaGeometry = CudaGeometry {{ grid_x: {lanes//block}, grid_y: 1, grid_z: 1, block_x: {block}, block_y: 1, block_z: 1, shared_bytes: 0 }};',
                              f'resident_need(cuda_launch_grid_sync(kernel, geometry{turn}, args, 10));',
                              f'resident_need(cuda_download(output as *u8, v_output, {capacity*8}));']
                else:
                    lines += [f'var i{turn}: u64 = 0; while i{turn} < {lanes} {{',
                              f'var accumulator: u64 = {bias}; var pair: u64 = 0; while pair < 4 {{',
                              f'let w: u64 = gpu_bounded_load_u64(w{turn}, {rows*2}, (i{turn} % {rows}) * 2 + pair / 2);',
                              f'let x: u64 = gpu_bounded_load_u64(input, {tokens*2}, (i{turn} / {rows}) * 2 + pair / 2);',
                              f'accumulator = low_projection_step(pair, {columns}, (w >> ((pair % 2) * 32)) as u32, (x >> ((pair % 2) * 32)) as u32, accumulator, {fmt}); pair = pair + 1; }}',
                              f'output[i{turn}+1] = low_projection_output(i{turn}, {cells}, accumulator); i{turn} = i{turn} + 1; }}']
                lines.append(f'j{turn} = 0; while j{turn} < {capacity} {{ print(output[j{turn}]); j{turn} = j{turn} + 1; }}')
            if native:
                lines.extend(f'resident_need(cuda_buffer_close(b_{name}));' for name in ('w0', 'w1', 'input', 'output'))
                lines.append('free(args);')
            lines.extend(f'free({name});' for name in ('w0', 'w1', 'input', 'output'))
            lines.append('}')
    lines.append('fn main(argc: i32, argv: **u8) -> i32 {')
    for args in [(0, 1, 1, 0, 2, 0), (1, 1, 9, 2, 2, 0), (1, 1, 1, 2, 2, 2),
                 (1, 1, 1, 8, 2, 0), (1 << 60, 1, 1, 1 << 61, 2, 0),
                 (1 << 32, 1 << 32, 1, 1 << 33, 1 << 33, 0)]:
        lines.append(f'if low_projection_valid({", ".join(map(str, args))}) {{ return 52; }}')
    if native:
        lines += ['if argc != 2 { return 53; }', 'let device: u64 = resident_need(cuda_device_open(0));',
                  'print(resident_need(cuda_device_sm(device)));',
                  'let module: u64 = resident_need(cuda_module_load(device, argv[1]));',
                  'let kernel: u64 = resident_need(cuda_kernel_get_owner(module, "low_projection"));']
    lines.extend(f'case{i}(device, kernel);' if native else f'case{i}();' for i in range(count))
    if native:
        lines.append('resident_need(cuda_module_close(module)); resident_need(cuda_device_close(device));')
    lines.append('return 0; }')
    return '\n'.join(lines), expected


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('tvc'); parser.add_argument('llc'); parser.add_argument('link')
    parser.add_argument('--opt'); parser.add_argument('--cuda')
    a = parser.parse_args()
    host, expected = generate(False)
    def check(text):
        actual = [int(v) & ((1 << 64)-1) for v in text.splitlines()]
        assert actual == expected, next(((i, x, y) for i, (x, y) in enumerate(zip(actual, expected)) if x != y), (len(actual), len(expected)))
    with tempfile.TemporaryDirectory(prefix='traveler-low-projection-') as directory:
        d = Path(directory)
        def compile_source(text, name, extra=()):
            (d/f'{name}.tv').write_text(text)
            run(a.tvc, d/f'{name}.tv', '-o', d/f'{name}.ll')
            run(a.llc, '-filetype=obj', d/f'{name}.ll', '-o', d/f'{name}.o')
            run(a.link, '-no-pie', d/f'{name}.o', '-lm', *extra, '-o', d/name)
        compile_source(host, 'host'); check(run(d/'host').stdout)
        if a.opt:
            run(a.opt, '-passes=default<O1>', '-S', d/'host.ll', '-o', d/'optimized.ll')
            run(a.llc, '-filetype=obj', d/'optimized.ll', '-o', d/'optimized.o')
            run(a.link, '-no-pie', d/'optimized.o', '-lm', '-o', d/'optimized')
            check(run(d/'optimized').stdout)
        run(a.tvc, ROOT/'tests/gpu/cuda_low_projection.tv', '--emit-gpu-nvptx', '-o', d/'device.ll')
        entries = cuda_package.descriptors((d/'device.ll').read_text())
        entry = next(k for k in entries if k['owner'] == 'low_projection')
        assert entry['requires'] == ['ieee32-rne-v1', 'bounded-read-u64-v1']
        for sm in (90, 120):
            cuda_package.build(d/'device.ll', d/f'sm{sm}.tvcp', a.llc, sm)
        if a.cuda:
            native, wanted = generate(True)
            assert wanted == expected
            compile_source(native, 'native', (a.cuda, f'-Wl,-rpath,{Path(a.cuda).resolve().parent}'))
            for target in (90, 120):
                device, _, output = run(d/'native', d/f'sm{target}.tvcp').stdout.partition('\n')
                check(output)
                print(f'Low projection native SM{device}, PTX SM{target}: 12 resident launches, {len(expected)} exact output/guard checks PASS')
        print(f'Low projection portable: {len(expected)} exact output/guard checks, packed storage and ordered f32 accumulation PASS')


if __name__ == '__main__':
    main()
