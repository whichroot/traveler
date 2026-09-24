"""Check IEEE carriers against exact rational rounding and native CUDA execution."""
import argparse
from fractions import Fraction
import itertools
import json
from pathlib import Path
import random
import struct
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'tools'))
import cuda_package


def run(*args, ok=True):
    p = subprocess.run([str(a) for a in args], capture_output=True, text=True, timeout=300)
    if ok:
        assert p.returncode == 0, (args, p.returncode, p.stdout, p.stderr)
    return p


def layout(width):
    fraction = 23 if width == 32 else 52
    exponent = width - fraction - 1
    infinity = ((1 << exponent) - 1) << fraction
    return fraction, (1 << (exponent - 1)) - 1, infinity


def decode(bits, width):
    fraction, bias, infinity = layout(width)
    sign = bits >> (width - 1)
    magnitude = bits & ((1 << (width - 1)) - 1)
    if magnitude >= infinity:
        return sign, 'inf' if magnitude == infinity else 'nan'
    exponent, mantissa = divmod(magnitude, 1 << fraction)
    if exponent:
        mantissa += 1 << fraction
    value = Fraction(mantissa) * Fraction(2) ** (max(exponent, 1) - bias - fraction)
    return sign, -value if sign else value


def rounded(value, width, negative_zero=False, square=False):
    fraction, bias, infinity = layout(width)
    sign = value < 0 or value == 0 and negative_zero
    value = abs(value)
    # Adjacent positive encodings are monotone, including subnormal boundaries.
    def magnitude(bits):
        return decode(bits, width)[1]
    limit = magnitude(infinity - 1) + Fraction(2) ** (bias - fraction - 1)
    if value >= (limit * limit if square else limit):
        return (int(sign) << (width - 1)) | infinity
    low, high = 0, infinity - 1
    while low < high:
        mid = (low + high + 1) // 2
        v = magnitude(mid)
        if (v*v if square else v) <= value:
            low = mid
        else:
            high = mid - 1
    if low < infinity - 1:
        midpoint = (magnitude(low) + magnitude(low + 1)) / 2
        boundary = midpoint * midpoint if square else midpoint
        if value > boundary or value == boundary and low & 1:
            low += 1
    return low | (int(sign) << (width - 1))


def oracle(name, width, output_width, args):
    if name in ('split', 'ordered_sum'):
        first = oracle('mul' if name == 'split' else 'add', width, width, args[:2])
        return oracle('add', width, width, (first, args[2]))
    decoded = [decode(a, width) for a in args]
    signs, values = zip(*decoded)
    infinity = layout(output_width)[2]
    nan = infinity | (1 << (layout(output_width)[0] - 1))
    inf = lambda sign: infinity | (int(sign) << (output_width - 1))
    if 'nan' in values:
        return nan
    if name == 'convert':
        return inf(signs[0]) if values[0] == 'inf' else rounded(values[0], output_width, signs[0])
    if name == 'sqrt':
        if signs[0] and values[0] != 0:
            return nan
        return infinity if values[0] == 'inf' else rounded(values[0], width, signs[0], square=True)
    a, b = values[:2]
    sa, sb = signs[:2]
    if name in ('add', 'sub'):
        if name == 'sub':
            sb ^= 1
            if b != 'inf':
                b = -b
        if a == b == 'inf':
            return nan if sa != sb else inf(sa)
        if a == 'inf' or b == 'inf':
            return inf(sa if a == 'inf' else sb)
        return rounded(a+b, width, a == b == 0 and sa and sb)
    if name in ('mul', 'fma'):
        if a == 'inf' and b == 0 or b == 'inf' and a == 0:
            return nan
        ps = sa ^ sb
        product = 'inf' if 'inf' in (a, b) else a*b
        if name == 'mul':
            return inf(ps) if product == 'inf' else rounded(product, width, ps)
        c = values[2]
        if product == c == 'inf' and ps != signs[2]:
            return nan
        if product == 'inf' or c == 'inf':
            return inf(ps if product == 'inf' else signs[2])
        return rounded(product+c, width, product == c == 0 and ps and signs[2])
    assert name == 'div'
    if a == b == 'inf' or a == b == 0:
        return nan
    if a == 'inf' or b == 0:
        return inf(sa ^ sb)
    if b == 'inf':
        return (sa ^ sb) << (width - 1)
    return rounded(a/b, width, sa ^ sb)


def cases():
    rng = random.Random(5032)
    groups = []
    for width in (32, 64):
        fraction, bias, infinity = layout(width)
        sign = 1 << (width - 1)
        one = bias << fraction
        edges = [0, sign, 1, sign | 1, (1 << fraction)-1, 1 << fraction,
                 one, one+1, one-1, one+(1 << fraction), one-(1 << fraction), sign | one, infinity-1, infinity,
                 sign | infinity, infinity+1, infinity | (1 << (fraction-1))]
        for name, arity in [('add', 2), ('sub', 2), ('mul', 2), ('div', 2), ('sqrt', 1), ('fma', 3), ('convert', 1), ('split', 3), ('ordered_sum', 3)]:
            pool = edges if arity < 3 else [0, sign, one, sign | one, infinity, sign | infinity, infinity+1]
            inputs = list(itertools.product(pool, repeat=arity))
            inputs += [tuple(rng.getrandbits(width) for _ in range(arity)) for _ in range(64)]
            if name in ('fma', 'split'):
                inputs.append((one+1, one-2, sign | one))
            if name == 'ordered_sum':
                inputs.append((infinity-1, sign | (infinity-1), one))
            if name in ('add', 'sub'):
                half_ulp = (bias-fraction-1) << fraction
                inputs += [(one, half_ulp), (one+1, half_ulp)]
            output = 96-width if name == 'convert' else width
            builtin = f'ieee{width}_to_ieee{output}' if name == 'convert' else f'ieee{width}_{name}'
            groups.append((builtin, width, output, inputs,
                           [oracle(name, width, output, row) for row in inputs]))
    return groups


def sources(groups, native=False, sequence_inputs=()):
    kernels, functions, calls = [], [], []
    if native:
        functions.append(f'import "{ROOT}/tests/gpu/cuda_resident_checks.tv";')
    for k, (builtin, width, output, inputs, expected) in enumerate(groups):
        arity, n = len(inputs[0]), len(inputs)
        params = ', '.join(f'a{j}: u{width}' for j in range(arity))
        args = ', '.join(f'a{j}' for j in range(arity))
        expression = f'{builtin}({args})'
        if builtin.endswith(('_split', '_ordered_sum')):
            inner = 'mul' if builtin.endswith('_split') else 'add'
            expression = f'ieee{width}_add(ieee{width}_{inner}(a0, a1), a2)'
        kernels.append(f'fn helper{k}({params}) -> u{output} {{ return {expression}; }}')
        pointers = ', '.join(f'a{j}: *u{width}' for j in range(arity))
        indexed = ', '.join(f'a{j}[i]' for j in range(arity))
        kernels.append(f'#[kernel]\nfn numeric{k}(i: i32, {pointers}, out: *u{output}) {{ out[i] = helper{k}({indexed}); }}')
        functions.append(f'fn check{k}(device: u64, module: u64) {{' if native else f'fn check{k}() {{')
        for j in range(arity):
            functions.append(f'let a{j}: *u{width} = alloc({n});')
            if k in sequence_inputs:
                functions.append(f'for init{j} in 0..{n} {{ a{j}[init{j}] = init{j} as u{width}; }}')
            else:
                functions.extend(f'a{j}[{i}] = {row[j]};' for i, row in enumerate(inputs))
        functions.append(f'let out: *u{output} = alloc({n});')
        if native:
            functions += [f'let kernel: u64 = resident_need(cuda_kernel_get_owner(module, "numeric{k}"));',
                          f'let args: *CudaArgument = alloc({arity+1});']
            for j in range(arity+1):
                w = width if j < arity else output
                name = f'a{j}' if j < arity else 'out'
                functions += [f'let b{j}: u64 = resident_need(cuda_buffer_alloc(device, {n*w//8}));',
                              f'let v{j}: CudaArgument = resident_typed_view(b{j}, 0, {n*w//8}, 0 - {w});',
                              f'resident_bind(kernel, args, "{name}", v{j});']
                if j < arity:
                    functions.append(f'resident_need(cuda_upload(v{j}, a{j} as *u8, {n*w//8}));')
            functions += [f'resident_need(cuda_launch_sync(kernel, 0, {n}, args, {arity+1}));',
                          f'resident_need(cuda_download(out as *u8, v{arity}, {n*output//8}));']
            functions.extend(f'resident_need(cuda_buffer_close(b{j}));' for j in range(arity+1))
            functions.append('free(args);')
        else:
            functions.append(f'for i in 0..{n} {{ out[i] = helper{k}({indexed}); }}')
        functions.append(f'var i: i32 = 0; while i < {n} {{ print(out[i]); i = i + 1; }}')
        functions.extend(f'free(a{j});' for j in range(arity))
        functions.append('free(out); }')
        calls.append(f'check{k}(device, module);' if native else f'check{k}();')
    if native:
        functions += ['fn main(argc: i32, argv: **u8) -> i32 { if argc != 2 { return 1; }',
                      'let device: u64 = resident_need(cuda_device_open(0));',
                      'print(resident_need(cuda_device_sm(device)));',
                      'let module: u64 = resident_need(cuda_module_load(device, argv[1]));']
        functions += calls + ['resident_need(cuda_module_close(module)); resident_need(cuda_device_close(device)); return 0; }']
    else:
        functions += ['fn main() {'] + calls + ['}']
    return '\n'.join(kernels), '\n'.join(kernels+functions)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('tvc'); parser.add_argument('llc'); parser.add_argument('link')
    parser.add_argument('--opt')
    parser.add_argument('--cuda')
    parser.add_argument('--sm', type=int, choices=(90, 120), default=120)
    a = parser.parse_args()
    groups = cases()
    expected = [v for group in groups for v in group[4]]
    widths = [group[2] for group in groups for _ in group[4]]
    def results(text):
        values = [int(v) for v in text.splitlines()]
        assert len(values) == len(widths)
        return [v & ((1 << width)-1) for v, width in zip(values, widths)]
    with tempfile.TemporaryDirectory(prefix='traveler-ieee-') as directory:
        d = Path(directory)
        kernels, host = sources(groups)
        (d/'kernels.tv').write_text(kernels)
        (d/'host.tv').write_text(host)

        def compile_host(source, name, extra=()):
            run(a.tvc, source, '-o', d/f'{name}.ll')
            run(a.llc, '-filetype=obj', d/f'{name}.ll', '-o', d/f'{name}.o')
            run(a.link, '-no-pie', d/f'{name}.o', '-lm', *extra, '-o', d/name)
            return d/name

        executable = compile_host(d/'host.tv', 'host')
        actual = results(run(executable).stdout)
        assert actual == expected, next(((i, x, y) for i, (x, y) in enumerate(zip(actual, expected)) if x != y), (len(actual), len(expected)))
        run(a.tvc, d/'host.tv', '--emit', 'exe', '-llc', a.llc, '-cc', a.link, '-o', d/'driver')
        assert results(run(d/'driver').stdout) == expected
        host_ir = (d/'host.ll').read_text()
        assert '@llvm.experimental.constrained.fma.f32' in host_ir
        report = run(a.tvc, d/'host.tv', '--pfor-report')
        assert '__pfor_worker_' in host_ir, report.stdout + report.stderr
        decisions = [json.loads(line) for line in report.stdout.splitlines()]
        assert len(decisions) == len(groups) and all(r['independent'] == 1 for r in decisions)
        if a.opt:
            run(a.opt, '-passes=default<O1>', '-S', d/'host.ll', '-o', d/'optimized.ll')
            run(a.llc, '-filetype=obj', d/'optimized.ll', '-o', d/'optimized.o')
            run(a.link, '-no-pie', d/'optimized.o', '-lm', '-o', d/'optimized')
            assert results(run(d/'optimized').stdout) == expected
        run(a.tvc, d/'kernels.tv', '--emit-gpu-nvptx', '-o', d/'device.ll')
        descriptors = cuda_package.descriptors((d/'device.ll').read_text())
        assert len(descriptors) == len(groups) and all(k['numerical'] == 'ieee-bits-rne-v1' for k in descriptors)
        refused = run(a.tvc, d/'kernels.tv', '--emit-gpu', '-o', d/'amd.ll', ok=False)
        assert refused.returncode != 0 and not (d/'amd.ll').exists()
        for sm in (90, 120):
            cuda_package.build(d/'device.ll', d/f'sm{sm}.tvcp', a.llc, sm)
            blob = (d/f'sm{sm}.tvcp').read_bytes()
            offset = 12 + struct.unpack('<I', blob[8:12])[0]
            ptx = blob[offset:].decode()
            for width in (32, 64):
                for operation in ('add', 'sub', 'mul', 'div', 'sqrt', 'fma'):
                    assert f'{operation}.rn.f{width}' in ptx
            assert '.ftz' not in ptx and '.approx' not in ptx
        package_gate = compile_host(ROOT/'tests/gpu/cuda_package_gate.tv', 'package')
        run(package_gate, d/'sm90.tvcp')
        blob = (d/'sm90.tvcp').read_bytes()
        length = struct.unpack('<I', blob[8:12])[0]
        header = json.loads(blob[12:12+length])
        for bad in ('fast-math-v1', None, 7):
            header['kernels'][0]['numerical'] = bad
            encoded = json.dumps(header, separators=(',', ':')).encode()
            (d/'bad.tvcp').write_bytes(blob[:8]+struct.pack('<I', len(encoded))+encoded+blob[12+length:])
            assert run(package_gate, d/'bad.tvcp', ok=False).returncode != 0
            try:
                cuda_package.validate_kernel(header['kernels'][0])
            except ValueError:
                pass
            else:
                raise AssertionError('unsupported numerical profile admitted')
        for text in ('ieee32_add(1)', 'ieee32_add(1 as u64, 1)', 'ieee64_sqrt(1 as i64)'):
            (d/'refuse.tv').write_text(f'fn main() {{ print({text}); }}')
            assert run(a.tvc, d/'refuse.tv', '-o', d/'refuse.ll', ok=False).returncode != 0
        (d/'shadow.tv').write_text('fn ieee32_add(a: u32, b: u32) -> u32 { return a ^ b; }\nfn main() { print(ieee32_add(1, 3)); }')
        assert run(compile_host(d/'shadow.tv', 'shadow')).stdout.strip() == '2'
        (d/'refuse.tv').write_text('fn main() { print(ieee32_add(1, 1)); }')
        refused = run(a.tvc, d/'refuse.tv', '--eval', ok=False)
        assert 'eval-refused' in refused.stderr
        if a.cuda:
            _, native = sources(groups, native=True)
            (d/'native.tv').write_text(native)
            executable = compile_host(d/'native.tv', 'native', (a.cuda, f'-Wl,-rpath,{Path(a.cuda).resolve().parent}'))
            for target in sorted({90, a.sm}):
                device_sm, _, output = run(executable, d/f'sm{target}.tvcp').stdout.partition('\n')
                assert results(output) == expected
                print(f'IEEE native SM{int(device_sm)}, PTX SM{target}: {len(expected)} exact bit-pattern results PASS')
        print(f'IEEE portable: {len(expected)} exact results, SM90/SM120 PTX, policy/refusal checks PASS')


if __name__ == '__main__':
    main()
