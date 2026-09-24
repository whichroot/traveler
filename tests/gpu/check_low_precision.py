"""Exhaustive low-precision encodings, rounding boundaries, and packed FMA."""
import argparse
from fractions import Fraction
import math
from pathlib import Path
import random
import struct
import tempfile

from check_ieee_bits import ROOT, cuda_package, decode, oracle, run, sources


def half_widen(bits):
    value = struct.unpack('>e', struct.pack('>H', bits))[0]
    return 0x7FC00000 if math.isnan(value) else struct.unpack('>I', struct.pack('>f', value))[0]


def half_narrow(bits):
    value = struct.unpack('>f', struct.pack('>I', bits))[0]
    if math.isnan(value):
        return 0x7E00
    try:
        return struct.unpack('>H', struct.pack('>e', value))[0]
    except OverflowError:
        return 0xFC00 if value < 0 else 0x7C00


def bfloat_widen(bits):
    return 0x7FC00000 if bits & 0x7FFF > 0x7F80 else bits << 16


def bfloat_narrow(bits):
    negative, value = decode(bits, 32)
    if value == 'nan':
        return 0x7FC0
    if value == 'inf':
        return 0xFF80 if negative else 0x7F80
    lower = (bits & 0x7FFFFFFF) >> 16
    lo = decode(lower << 16, 32)[1]
    hi = Fraction(2)**128 if lower == 0x7F7F else decode((lower+1) << 16, 32)[1]
    middle = (lo+hi)/2
    increment = abs(value) > middle or abs(value) == middle and lower & 1
    return (negative << 15) | (lower + int(bool(increment)))


def catalogue():
    rng = random.Random(5035)
    wrappers = [f'import "{ROOT}/src/lib/float/low_precision.tv";']
    groups = []

    def add(expression, inputs, expected):
        k = len(groups)
        args = ', '.join(f'a{i}: u64' for i in range(len(inputs[0])))
        wrappers.append(f'fn low_case{k}({args}) -> u64 {{ return ({expression}) as u64; }}')
        groups.append((f'low_case{k}', 64, 64, inputs, expected))

    encodings = [(i,) for i in range(65536)]
    for name, widen, narrow in [('ieee16', half_widen, half_narrow), ('bfloat16', bfloat_widen, bfloat_narrow)]:
        wide = [widen(i) for i in range(65536)]
        add(f'{name}_to_ieee32(a0 as u16)', encodings, wide)
        add(f'{name}_from_ieee32({name}_to_ieee32(a0 as u16))', encodings, [narrow(v) for v in wide])
    for name, widen, narrow, infinity in [('ieee16', half_widen, half_narrow, 0x7C00), ('bfloat16', bfloat_widen, bfloat_narrow, 0x7F80)]:
        values = {0, 1, 0x7F800000, 0xFF800000, 0x7F800001, 0xFF800001, 0x80000000,
                  0x477FEFFF, 0x477FF000, 0x477FF001, 0x7F7F7FFF, 0x7F7F8000, 0x7F7F8001}
        values.update(rng.getrandbits(32) for _ in range(1000))
        boundaries = list(range(32))+[rng.randrange(infinity-1) for _ in range(200)]
        for low in boundaries:
            lo = decode(widen(low), 32)[1]
            hi = decode(widen(low+1), 32)[1]
            midpoint = struct.unpack('>I', struct.pack('>f', float((lo+hi)/2)))[0]
            for delta in (-1, 0, 1):
                if 0 <= midpoint+delta < 0x7F800000:
                    values.add(midpoint+delta); values.add((midpoint+delta) | 0x80000000)
        values = sorted(values)
        add(f'{name}_from_ieee32(a0 as u32)', [(v,) for v in values], [narrow(v) for v in values])
    for name, widen in [('ieee16', half_widen), ('bfloat16', bfloat_widen)]:
        inputs = [(rng.getrandbits(32), rng.getrandbits(32), rng.getrandbits(64)) for _ in range(1000)]
        expected = []
        for a, b, acc in inputs:
            low = oracle('fma', 32, 32, (widen(a & 65535), widen(b & 65535), acc & 0xFFFFFFFF))
            high = oracle('fma', 32, 32, (widen(a >> 16), widen(b >> 16), acc >> 32))
            expected.append(low | (high << 32))
        add(f'{name}x2_fma32(a0 as u32, a1 as u32, a2)', inputs, expected)
    return groups, '\n'.join(wrappers)+'\n'


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('tvc'); parser.add_argument('llc'); parser.add_argument('link')
    parser.add_argument('--opt'); parser.add_argument('--cuda')
    a = parser.parse_args()
    groups, prefix = catalogue()
    expected = [v for group in groups for v in group[4]]
    def check(text):
        actual = [int(v) & ((1 << 64)-1) for v in text.splitlines()]
        assert actual == expected, next(((i, x, y) for i, (x, y) in enumerate(zip(actual, expected)) if x != y), (len(actual), len(expected)))
    with tempfile.TemporaryDirectory(prefix='traveler-low-precision-') as directory:
        d = Path(directory)
        kernels, host = sources(groups, sequence_inputs=range(4))
        (d/'kernels.tv').write_text(prefix+kernels)
        (d/'host.tv').write_text(prefix+host)
        run(a.tvc, d/'host.tv', '-o', d/'host.ll')
        run(a.llc, '-filetype=obj', d/'host.ll', '-o', d/'host.o')
        run(a.link, '-no-pie', d/'host.o', '-lm', '-o', d/'host')
        check(run(d/'host').stdout)
        if a.opt:
            run(a.opt, '-passes=default<O1>', '-S', d/'host.ll', '-o', d/'optimized.ll')
            run(a.llc, '-filetype=obj', d/'optimized.ll', '-o', d/'optimized.o')
            run(a.link, '-no-pie', d/'optimized.o', '-lm', '-o', d/'optimized')
            check(run(d/'optimized').stdout)
        run(a.tvc, d/'kernels.tv', '--emit-gpu-nvptx', '-o', d/'device.ll')
        entries = cuda_package.descriptors((d/'device.ll').read_text())
        assert len(entries) == len(groups)
        assert all('requires' not in k for k in entries[:6])
        assert all(k['requires'] == ['ieee32-rne-v1'] for k in entries[6:])
        for sm in (90, 120):
            cuda_package.build(d/'device.ll', d/f'sm{sm}.tvcp', a.llc, sm)
        if a.cuda:
            _, native = sources(groups, native=True, sequence_inputs=range(4))
            (d/'native.tv').write_text(prefix+native)
            run(a.tvc, d/'native.tv', '-o', d/'native.ll')
            run(a.llc, '-filetype=obj', d/'native.ll', '-o', d/'native.o')
            run(a.link, '-no-pie', d/'native.o', '-lm', a.cuda, f'-Wl,-rpath,{Path(a.cuda).resolve().parent}', '-o', d/'native')
            for target in (90, 120):
                device, _, output = run(d/'native', d/f'sm{target}.tvcp').stdout.partition('\n')
                check(output)
                print(f'Low precision native SM{device}, PTX SM{target}: {len(expected)} exact results PASS')
        print(f'Low precision portable: {len(expected)} exact results, exhaustive encodings, rounding boundaries, packed FMA PASS')


if __name__ == '__main__':
    main()
