"""Validate fixed-order resident projections, policy changes, and padded tails."""
import argparse
from fractions import Fraction
from pathlib import Path
import random
import tempfile

from check_ieee_bits import ROOT, cuda_package, oracle, rounded, run

SENTINEL = 0xD15EA5E0D15EA5E0


def fixtures():
    rng = random.Random(5034)
    cases = []
    one = 0x3FF0000000000000
    edge = [0, 1 << 63, 1, 0x0010000000000000, one, one+1, one-2,
            0xBFF0000000000000, 0x7FEFFFFFFFFFFFFF, 0x7FF0000000000000,
            0xFFF0000000000000, 0x7FF0000000000001]
    for rows, tokens, columns in [(1, 1, 1), (3, 2, 3), (17, 3, 7), (33, 2, 8)]:
        for exceptional in (False, True):
            def sample():
                return rng.choice(edge) if exceptional else rounded(Fraction(rng.randint(-100, 100), 16), 64)
            weights = [[sample() for _ in range(rows*8)] for _ in range(2)]
            inputs = [sample() for _ in range(tokens*8)]
            # Inactive columns contain poison values and must not contribute.
            for row in range(rows):
                for k in range(columns, 8):
                    weights[0][row*8+k] = weights[1][row*8+k] = 0x7FF0000000000001
            for token in range(tokens):
                for k in range(columns, 8):
                    inputs[token*8+k] = 0x7FF0000000000000
            weights[0][0] = one+1; inputs[0] = one-2
            cases.append((rows, tokens, columns, weights, inputs))
    return cases


def source(cases, native):
    lines = [f'import "{ROOT}/tests/gpu/cuda_numeric_projection.tv";',
             f'import "{ROOT}/src/lib/float/numeric.tv";']
    if native:
        lines.append(f'import "{ROOT}/tests/gpu/cuda_resident_checks.tv";')
    expected = []
    for case, (rows, tokens, columns, weights, inputs) in enumerate(cases):
        cells = rows*tokens
        capacity = ((cells+63)//64)*64+2
        lines.append(f'fn case{case}(device: u64, kernel: u64) {{' if native else f'fn case{case}() {{')
        for name, values in [('w0', weights[0]), ('w1', weights[1]), ('input', inputs)]:
            lines.append(f'let {name}: *u64 = alloc({len(values)});')
            lines.extend(f'{name}[{i}] = {v};' for i, v in enumerate(values))
        lines.append(f'let output: *u64 = alloc({capacity});')
        if native:
            for name, count in [('w0', rows*8), ('w1', rows*8), ('input', tokens*8), ('output', capacity)]:
                lines += [f'let b_{name}: u64 = resident_need(cuda_buffer_alloc(device, {count*8}));',
                          f'let v_{name}: CudaArgument = resident_typed_view(b_{name}, 0, {count*8}, 0 - 64);']
                if name != 'output':
                    lines.append(f'resident_need(cuda_upload(v_{name}, {name} as *u8, {count*8}));')
            lines.append('let args: *CudaArgument = alloc(10);')
            for name, expr in [('weights_count', str(rows*8)), ('input_count', str(tokens*8)),
                               ('rows', str(rows)), ('tokens', str(tokens)), ('columns', str(columns))]:
                lines.append(f'resident_bind(kernel, args, "{name}", cuda_arg_u64({expr}));')
            lines.append('resident_bind(kernel, args, "input", v_input);')
        for turn, block in enumerate((1, 7, 32, 64)):
            expert, fused = turn % 2, turn // 2
            lanes = ((cells+block-1)//block)*block
            bias_integer = -1 if turn % 2 == 0 else 1
            bias = rounded(Fraction(bias_integer), 64)
            lines += [f'if numeric_projection_valid({rows}, {tokens}, {columns}, {rows*8}, {tokens*8}, {fused}) {{ }} else {{ exit(41); }}',
                      f'var j{turn}: i32 = 0; while j{turn} < {capacity} {{ output[j{turn}] = {SENTINEL}; j{turn} = j{turn} + 1; }}',
                      f'let bias{turn}: u64 = ieee64_from_i32({bias_integer});']
            values = [SENTINEL]*capacity
            for i in range(lanes):
                value = 0
                if i < cells:
                    token, row = divmod(i, rows)
                    value = bias
                    for k in range(columns):
                        value = oracle('fma' if fused else 'split', 64, 64,
                                       (weights[expert][row*8+k], inputs[token*8+k], value))
                values[i+1] = value
            expected.extend(values)
            if native:
                lines += [f'resident_need(cuda_upload(v_output, output as *u8, {capacity*8}));',
                          f'resident_bind(kernel, args, "weights", v_w{expert});',
                          f'resident_bind(kernel, args, "bias", cuda_arg_u64(bias{turn}));',
                          f'resident_bind(kernel, args, "fused", cuda_arg_u64({fused}));',
                          f'let slice{turn}: CudaArgument = resident_typed_view(b_output, 8, {lanes*8}, 0 - 64);',
                          f'resident_bind(kernel, args, "output", slice{turn});',
                          f'let geometry{turn}: CudaGeometry = CudaGeometry {{ grid_x: {lanes//block}, grid_y: 1, grid_z: 1, block_x: {block}, block_y: 1, block_z: 1, shared_bytes: 0 }};',
                          f'resident_need(cuda_launch_grid_sync(kernel, geometry{turn}, args, 10));',
                          f'resident_need(cuda_download(output as *u8, v_output, {capacity*8}));']
            else:
                lines += [f'var i{turn}: u64 = 0; while i{turn} < {lanes} {{',
                          f'var sum{turn}: u64 = bias{turn};',
                          f'var k{turn}: u64 = 0; while k{turn} < 8 {{',
                          f'let w: u64 = gpu_bounded_load_u64(w{expert}, {rows*8}, (i{turn} % {rows}) * 8 + k{turn});',
                          f'let x: u64 = gpu_bounded_load_u64(input, {tokens*8}, (i{turn} / {rows}) * 8 + k{turn});',
                          f'sum{turn} = numeric_projection_step(k{turn}, {columns}, w, x, sum{turn}, {fused}); k{turn} = k{turn} + 1; }}',
                          f'output[i{turn}+1] = numeric_projection_output(i{turn}, {cells}, sum{turn}); i{turn} = i{turn} + 1; }}']
            lines.append(f'j{turn} = 0; while j{turn} < {capacity} {{ print(output[j{turn}]); j{turn} = j{turn} + 1; }}')
        if native:
            lines += [f'resident_need(cuda_buffer_close(b_{name}));' for name in ('w0', 'w1', 'input', 'output')]
            lines.append('free(args);')
        lines.extend(f'free({name});' for name in ('w0', 'w1', 'input', 'output'))
        lines.append('}')
    lines += ['fn main(argc: i32, argv: **u8) -> i32 {']
    for args in [(0, 1, 1, 0, 8, 0), (1, 0, 1, 8, 0, 0), (1, 1, 0, 8, 8, 0),
                 (1, 1, 9, 8, 8, 0), (1, 1, 1, 7, 8, 0), (1, 1, 1, 8, 7, 0),
                 (1, 1, 1, 8, 8, 2), ((1 << 64)-1, 1, 1, 8, 8, 0),
                 (1 << 58, 1, 1, 1 << 61, 8, 0), (1, 1 << 58, 1, 8, 1 << 61, 0),
                 (1 << 32, 1 << 32, 1, 1 << 35, 1 << 35, 0)]:
        lines.append(f'if numeric_projection_valid({", ".join(map(str, args))}) {{ return 42; }}')
    if native:
        lines += ['if argc != 2 { return 43; }', 'let device: u64 = resident_need(cuda_device_open(0));',
                  'print(resident_need(cuda_device_sm(device)));',
                  'let module: u64 = resident_need(cuda_module_load(device, argv[1]));',
                  'let kernel: u64 = resident_need(cuda_kernel_get_owner(module, "numeric_projection"));']
    lines += [f'case{i}(device, kernel);' if native else f'case{i}();' for i in range(len(cases))]
    if native:
        lines += ['resident_need(cuda_module_close(module)); resident_need(cuda_device_close(device));']
    lines += ['return 0; }']
    return '\n'.join(lines), expected


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('tvc'); parser.add_argument('llc'); parser.add_argument('link')
    parser.add_argument('--opt'); parser.add_argument('--cuda')
    a = parser.parse_args()
    cases = fixtures()
    host, expected = source(cases, False)
    def check(text):
        values = [int(v) & ((1 << 64)-1) for v in text.splitlines()]
        assert values == expected, next(((i, x, y) for i, (x, y) in enumerate(zip(values, expected)) if x != y), (len(values), len(expected)))
    with tempfile.TemporaryDirectory(prefix='traveler-projection-') as directory:
        d = Path(directory)
        def compile_source(text, name, extra=()):
            (d/f'{name}.tv').write_text(text)
            run(a.tvc, d/f'{name}.tv', '-o', d/f'{name}.ll')
            run(a.llc, '-filetype=obj', d/f'{name}.ll', '-o', d/f'{name}.o')
            run(a.link, '-no-pie', d/f'{name}.o', '-lm', *extra, '-o', d/name)
        compile_source(host, 'host')
        check(run(d/'host').stdout)
        if a.opt:
            run(a.opt, '-passes=default<O1>', '-S', d/'host.ll', '-o', d/'optimized.ll')
            run(a.llc, '-filetype=obj', d/'optimized.ll', '-o', d/'optimized.o')
            run(a.link, '-no-pie', d/'optimized.o', '-lm', '-o', d/'optimized')
            check(run(d/'optimized').stdout)
        run(a.tvc, ROOT/'tests/gpu/cuda_numeric_projection.tv', '--emit-gpu-nvptx', '-o', d/'device.ll')
        entries = cuda_package.descriptors((d/'device.ll').read_text())
        assert len(entries) == 1 and entries[0]['numerical'] == 'ieee-bits-rne-v1'
        assert entries[0]['profile'] == 'cooperative-grid-v1'
        bounded = [p for p in entries[0]['parameters'] if 'count_parameter' in p.get('footprint', {})]
        assert len(bounded) == 2 and all(p['access'] == 'read' for p in bounded)
        for sm in (90, 120):
            cuda_package.build(d/'device.ll', d/f'sm{sm}.tvcp', a.llc, sm)
        if a.cuda:
            native, native_expected = source(cases, True)
            assert native_expected == expected
            compile_source(native, 'native', (a.cuda, f'-Wl,-rpath,{Path(a.cuda).resolve().parent}'))
            for target in (90, 120):
                device, _, output = run(d/'native', d/f'sm{target}.tvcp').stdout.partition('\n')
                check(output)
                print(f'Projection native SM{device}, PTX SM{target}: 32 resident launches, {len(expected)} exact output/guard checks PASS')
        print(f'Projection portable: {len(expected)} exact output/guard checks, geometry/tail/policy admission PASS')


if __name__ == '__main__':
    main()
