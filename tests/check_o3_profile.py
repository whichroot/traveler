"""Check explicit-CPU O3 lowering, exact results, and closed driver options."""
import argparse
import json
import os
from pathlib import Path
import re
import resource
import shutil
import subprocess
import sys
import tempfile

from check_arithmetic_contract import value_cases


SOURCE = """
fn dot(codes: *u8, lut: *i32, x: *i16, n: i64) -> i64 {
    var sum: i64 = 0;
    for i in 0..n { sum = sum + (lut[codes[i] as i32] as i64) * (x[i] as i64); }
    return sum;
}
fn main() {
    let n: i64 = 4099;
    let codes: *u8 = alloc(n);
    let lut: *i32 = alloc(256);
    let x: *i16 = alloc(n);
    for i in 0..256 { lut[i] = i * 1793 - 229376; }
    for i in 0..n {
        codes[i] = ((i * 37 + 11) % 256) as u8;
        x[i] = ((i * 97 + 51) % 65536 - 32768) as i16;
    }
    print(dot(codes, lut, x, 0));
    print(dot(codes, lut, x, 1));
    print(dot(codes, lut, x, 15));
    print(dot(codes, lut, x, n));
    free(codes); free(lut); free(x);
}
"""


def run(*args, ok=True, threads=1):
    p = subprocess.run(list(map(str, args)), capture_output=True, text=True,
                       env={**os.environ, 'TRAVELER_THREADS': str(threads)})
    if ok and p.returncode:
        raise AssertionError((args, p.returncode, p.stdout, p.stderr))
    return p


def main():
    parser = argparse.ArgumentParser()
    for name in ('tvc', 'llc', 'opt', 'link'):
        parser.add_argument(name)
    a = parser.parse_args()
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    with tempfile.TemporaryDirectory(prefix='traveler-o3-') as directory:
        d = Path(directory)
        spies = []
        for name, tool in [('opt', a.opt), ('llc', a.llc)]:
            resolved = shutil.which(tool)
            assert resolved, f'{tool} unavailable'
            spy = d/f'{name} spy'
            spy.write_text(f'#!{sys.executable}\nimport json, os, sys\n'
                           f'with open({str(d/(name+".jsonl"))!r}, "a") as f:\n'
                           '    f.write(json.dumps(sys.argv[1:])+"\\n")\n'
                           f'os.execv({str(Path(resolved).resolve())!r}, [{str(tool)!r}]+sys.argv[1:])\n')
            spy.chmod(0o755)
            spies.append(spy)

        def compile_source(source, output, cpu='x86-64', mode='ir'):
            return run(a.tvc, source, '-o', output, '--emit', mode,
                       '--opt-level', 'o3', '-mcpu', cpu, '-opt', spies[0],
                       '-llc', spies[1], '-cc', a.link, '-target', 'x86_64-linux-gnu')

        source = d/'dot.tv'
        source.write_text(SOURCE)
        for cpu in ('x86-64', 'sapphirerapids'):
            ir = d/f'{cpu}.ll'
            compile_source(source, ir, cpu)
            repeated = d/f'{cpu}-repeat.ll'
            compile_source(source, repeated, cpu)
            assert ir.read_bytes() == repeated.read_bytes(), cpu
            assert f'"target-cpu"="{cpu}"' in ir.read_text(), 'CPU requirement missing from IR'
            run(a.opt, '-passes=verify', '-disable-output', ir)
            asm = d/f'{cpu}.s'
            run(a.llc, f'-mcpu={cpu}', ir, '-o', asm)
            if cpu == 'sapphirerapids':
                assert 'vpmuldq' in asm.read_text(), 'signed widening vector multiply missing'
                assert re.search(r'v[pi]?gather', asm.read_text()), 'LUT vector gather missing'
            compile_source(source, d/f'{cpu}.o', cpu, 'obj')
        compile_source(source, d/'dot', mode='exe')
        mask = (1 << 64)-1
        expected = [sum((((i*37+11) % 256)*1793-229376)*((i*97+51) % 65536-32768)
                        for i in range(n)) & mask for n in (0, 1, 15, 4099)]
        for threads in (1, 4):
            assert [int(v) & mask for v in run(d/'dot', threads=threads).stdout.split()] == expected
        values, expected = value_cases()
        source.write_text(values)
        compile_source(source, d/'values', mode='exe')
        actual = [int(v) if int(v) >= 0 else int(v)+(1 << 64)
                  for v in run(d/'values').stdout.splitlines()]
        assert actual == expected, 'arithmetic oracle mismatch under O3'
        for name, expr in [('zero', '(12 as i64) / 0'),
                           ('overflow', '(-9223372036854775808 as i64) / -1')]:
            source.write_text('fn main() { let discarded: i64 = '+expr+'; print(42); }')
            compile_source(source, d/name, mode='exe')
            p = run(d/name, ok=False)
            assert p.returncode == -6 and '42' not in p.stdout, (name, p)
        for name in ('opt', 'llc'):
            calls = [json.loads(line) for line in (d/(name+'.jsonl')).read_text().splitlines()]
            assert calls and all(any(arg.startswith('-mcpu=') for arg in call) for call in calls), calls
            if name == 'opt':
                assert all('-passes=default<O3>' in call and '-verify-each' in call for call in calls)
        source.write_text('fn main() { print(42); }')
        bad_flags = [
            ['--opt-level', 'o3'],
            ['--opt-level', 'o3', '-mcpu', 'native'],
            ['--opt-level', 'o3', '-mcpu', 'sapphirerapids', '-target', 'aarch64-linux-gnu'],
            ['--opt-level', 'o1', '-mcpu', 'x86-64'],
            ['-mcpu', 'x86-64'], ['-mcpu'],
            ['--opt-level', 'o3', '-mcpu', 'x86-64', '--eval'],
            ['--opt-level', 'o3', '-mcpu', 'x86-64', '--emit-gpu-nvptx'],
        ]
        for flags in bad_flags:
            output = d/'protected.ll'
            output.write_text('preserve')
            p = run(a.tvc, source, '-o', output, *flags, ok=False)
            assert p.returncode != 0 and 'error:' in p.stderr, (flags, p)
            assert output.read_text() == 'preserve', flags
    print('O3 PASS: explicit CPU forwarding, vector gather/widening multiply, 978 exact values, failures, and refusals')


if __name__ == '__main__':
    main()
