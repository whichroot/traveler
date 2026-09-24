"""Check pure integer/IEEE conversions, classification, and ordered comparisons."""
import argparse
from fractions import Fraction
import itertools
from pathlib import Path
import random
import tempfile

from check_ieee_bits import ROOT, cuda_package, decode, layout, rounded, run, sources


def catalogue():
    rng = random.Random(5033)
    groups, wrappers = [], [f'import "{ROOT}/src/lib/float/numeric.tv";']
    mask = (1 << 64)-1

    def add(expression, inputs, expected):
        k = len(groups)
        params = ', '.join(f'a{i}: u64' for i in range(len(inputs[0])))
        wrappers.append(f'fn numeric_case{k}({params}) -> u64 {{ return ({expression}) as u64; }}')
        groups.append((f'numeric_case{k}', 64, 64,
                       [tuple(v & mask for v in row) for row in inputs], [int(v) & mask for v in expected]))

    for width in (32, 64):
        fraction, bias, infinity = layout(width)
        sign = 1 << (width-1)
        one = bias << fraction
        edges = [0, sign, 1, sign+1, (1 << fraction)-1, 1 << fraction,
                 one, one-1, one+1, one+(1 << fraction), one-(1 << fraction),
                 sign | one, infinity-1, infinity, sign | infinity,
                 infinity+1, sign | (infinity+1), infinity | (1 << (fraction-1))]
        values = edges + [rng.getrandbits(width) for _ in range(100)]
        for bits in (32, 64):
            for signed in (False, True):
                low, high = (-(1 << (bits-1)), (1 << (bits-1))-1) if signed else (0, (1 << bits)-1)
                ty = f'{"i" if signed else "u"}{bits}'
                integers = [low, high, 0, 1, high-1] + ([-1, low+1] if signed else [])
                for power in range(bits):
                    integers.extend(v for v in ((1 << power)-1, 1 << power, (1 << power)+1) if low <= v <= high)
                integers += [rng.randint(low, high) for _ in range(100)]
                add(f'ieee{width}_from_{ty}(a0 as {ty})', [(v,) for v in integers], [rounded(Fraction(v), width) for v in integers])
                boundary = [rounded(Fraction(v), width) for v in (low, high, low-1, high+1)]
                floats = values + [v+d for v in boundary for d in (-1, 0, 1) if 0 <= v+d < (1 << width)]
                expected = []
                for v in floats:
                    negative, number = decode(v, width)
                    if number == 'nan':
                        expected.append(0)
                    elif number == 'inf':
                        expected.append(low if negative else high)
                    else:
                        expected.append(max(low, min(high, int(number))))
                add(f'ieee{width}_to_{ty}_sat(a0 as u{width})', [(v,) for v in floats], expected)
        pairs = list(itertools.product(edges, repeat=2)) + [(rng.getrandbits(width), rng.getrandbits(width)) for _ in range(100)]
        for operation in ('eq', 'lt', 'le'):
            expected = []
            for a, b in pairs:
                sa, va = decode(a, width); sb, vb = decode(b, width)
                if va == 'nan' or vb == 'nan':
                    expected.append(False)
                else:
                    ka = (0 if sa else 2, 0) if va == 'inf' else (1, va)
                    kb = (0 if sb else 2, 0) if vb == 'inf' else (1, vb)
                    expected.append(ka == kb if operation == 'eq' else ka < kb if operation == 'lt' else ka <= kb)
            add(f'ieee{width}_{operation}(a0 as u{width}, a1 as u{width})', pairs, expected)
        classes = []
        for v in values:
            magnitude = v & (sign-1)
            classes.append(4 if magnitude > infinity else 3 if magnitude == infinity else 0 if magnitude == 0 else 2 if magnitude < (1 << fraction) else 1)
        for name, expected in [('classify', classes), ('is_nan', [c == 4 for c in classes]), ('is_finite', [c < 3 for c in classes])]:
            add(f'ieee{width}_{name}(a0 as u{width})', [(v,) for v in values], expected)
    return groups, '\n'.join(wrappers)+'\n'


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('tvc'); parser.add_argument('llc'); parser.add_argument('link')
    parser.add_argument('--opt'); parser.add_argument('--cuda')
    a = parser.parse_args()
    groups, prefix = catalogue()
    expected = [v for group in groups for v in group[4]]
    def check(text):
        values = [int(v) & ((1 << 64)-1) for v in text.splitlines()]
        assert values == expected, next(((i, x, y) for i, (x, y) in enumerate(zip(values, expected)) if x != y), (len(values), len(expected)))
    with tempfile.TemporaryDirectory(prefix='traveler-numeric-') as directory:
        d = Path(directory)
        kernels, host = sources(groups)
        (d/'kernels.tv').write_text(prefix+kernels)
        (d/'host.tv').write_text(prefix+host)
        run(a.tvc, d/'host.tv', '-o', d/'host.ll')
        run(a.llc, '-filetype=obj', d/'host.ll', '-o', d/'host.o')
        run(a.link, '-no-pie', d/'host.o', '-o', d/'host')
        check(run(d/'host').stdout)
        run(a.tvc, ROOT/'tests/gpu/ieee_predicates.tv', '-o', d/'predicates.ll')
        run(a.llc, '-filetype=obj', d/'predicates.ll', '-o', d/'predicates.o')
        run(a.link, '-no-pie', d/'predicates.o', '-o', d/'predicates')
        assert run(d/'predicates').stdout.strip() == '6'
        if a.opt:
            run(a.opt, '-passes=default<O1>', '-S', d/'host.ll', '-o', d/'optimized.ll')
            run(a.llc, '-filetype=obj', d/'optimized.ll', '-o', d/'optimized.o')
            run(a.link, '-no-pie', d/'optimized.o', '-o', d/'optimized')
            check(run(d/'optimized').stdout)
            run(a.opt, '-passes=default<O1>', '-S', d/'predicates.ll', '-o', d/'predicates-opt.ll')
            run(a.llc, '-filetype=obj', d/'predicates-opt.ll', '-o', d/'predicates-opt.o')
            run(a.link, '-no-pie', d/'predicates-opt.o', '-o', d/'predicates-opt')
            assert run(d/'predicates-opt').stdout.strip() == '6'
        run(a.tvc, d/'kernels.tv', '--emit-gpu-nvptx', '-o', d/'device.ll')
        entries = cuda_package.descriptors((d/'device.ll').read_text())
        assert len(entries) == len(groups)
        for sm in (90, 120):
            cuda_package.build(d/'device.ll', d/f'sm{sm}.tvcp', a.llc, sm)
        if a.cuda:
            _, native = sources(groups, native=True)
            (d/'native.tv').write_text(prefix+native)
            run(a.tvc, d/'native.tv', '-o', d/'native.ll')
            run(a.llc, '-filetype=obj', d/'native.ll', '-o', d/'native.o')
            run(a.link, '-no-pie', d/'native.o', a.cuda, f'-Wl,-rpath,{Path(a.cuda).resolve().parent}', '-o', d/'native')
            for target in (90, 120):
                device, _, output = run(d/'native', d/f'sm{target}.tvcp').stdout.partition('\n')
                check(output)
                print(f'IEEE utilities native SM{device}, PTX SM{target}: {len(expected)} exact results PASS')
        print(f'IEEE utilities portable: {len(expected)} exact results across {len(groups)} kernels PASS')


if __name__ == '__main__':
    main()
