"""Check arithmetic domains against integer oracles and stable failure outcomes."""
import argparse
import os
from pathlib import Path
import resource
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT/'tools'))
import cuda_package


def run(*args, ok=True, threads=1):
    p = subprocess.run(list(map(str, args)), capture_output=True, text=True,
                       env={**os.environ, 'TRAVELER_THREADS': str(threads)})
    if ok and p.returncode: raise AssertionError((args, p.returncode, p.stdout, p.stderr))
    return p


def quotient(a, b):
    return (abs(a)//abs(b)) * (-1 if (a < 0) != (b < 0) else 1)


def value_cases():
    functions, calls, expected = [], [], []
    for width in (8, 16, 32, 64, 128, 256):
        for signed in (False, True):
            t = ('i' if signed else 'u')+str(width); mask = (1 << width)-1
            for op, symbol in [('left', '<<'), ('right', '>>')]:
                functions.append(f'fn {op}_{t}(x: {t}, n: {t}) -> {t} {{ return x {symbol} n; }}')
                for bits in (0, 1, (1 << (width-1))-1, 1 << (width-1), mask):
                    x = bits-(1 << width) if signed and bits >> (width-1) else bits
                    for count in (0, 1, width-1, width, width+1, mask):
                        n = count-(1 << width) if signed and count >> (width-1) else count
                        if op == 'left': answer = (bits << count) & mask if count < width else 0
                        else: answer = (x >> min(count, width-1)) & mask if signed else (bits >> count if count < width else 0)
                        calls.append(f'print(({op}_{t}({x}, {n}) as u{width}) as u{max(64, width)});')
                        expected.append(answer)
            if width <= 64:
                for op, symbol in [('div', '/'), ('rem', '%')]:
                    functions.append(f'fn {op}_{t}(x: {t}, n: {t}) -> {t} {{ return x {symbol} n; }}')
                    values = (0, 1, -1, -(1 << (width-1)), (1 << (width-1))-1) if signed else (0, 1, mask, mask//2)
                    divisors = (1, -1, 3, -(1 << (width-1))) if signed else (1, 3, mask)
                    for x in values:
                        for n in divisors:
                            if op == 'div' and signed and x == -(1 << (width-1)) and n == -1: continue
                            q = quotient(x, n)
                            answer = q if op == 'div' else x-q*n
                            calls.append(f'print(({op}_{t}({x}, {n}) as u{width}) as u64);')
                            expected.append(answer & mask)
    calls += ['let a: A = 250; let b: B = (a as u64) as B; print(b); print(b + 1);',
              'let f: F = a; print(f);', 'print(255 as u8); print(65535 as u16); print(-1 as i8);']
    expected += [5, 6, 250, 255, 65535, (1 << 64)-1]
    return 'type A = Field<251>; type F = Field<251>; type B = Field<7>;\n'+'\n'.join(functions)+'\nfn main() {\n'+'\n'.join(calls)+'\n}', expected


def main():
    p = argparse.ArgumentParser()
    for name in ('tvc', 'llc', 'opt', 'link'): p.add_argument(name)
    p.add_argument('--cuda'); a = p.parse_args()
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    with tempfile.TemporaryDirectory(prefix='traveler-arithmetic-') as directory:
        d = Path(directory)
        def compile_case(name, text):
            source = d/f'{name}.tv'; source.write_text(text)
            run(a.tvc, source, '-o', d/f'{name}.ll')
            run(a.opt, '-passes=verify', '-disable-output', d/f'{name}.ll')
            run(a.opt, '-passes=default<O1>', '-S', d/f'{name}.ll', '-o', d/f'{name}-o1.ll')
            binaries = []
            for mode in ('', '-o1'):
                stem = name+mode
                run(a.llc, '-filetype=obj', d/f'{stem}.ll', '-o', d/f'{stem}.o')
                flags = ['-no-pie'] if sys.platform.startswith('linux') else []
                run(a.link, *flags, d/f'{stem}.o', '-o', d/stem)
                binaries.append(d/stem)
            return source, binaries
        text, expected = value_cases()
        source, binaries = compile_case('values', text)
        for command in [(a.tvc, source, '--eval'), *((b,) for b in binaries)]:
            actual = [int(v) if int(v) >= 0 else int(v)+(1 << 64) for v in run(*command).stdout.splitlines()]
            assert actual == expected, next(((i, x, y) for i, (x, y) in enumerate(zip(actual, expected)) if x != y), (len(actual), len(expected)))
        failures = {}
        for t in ('i8', 'u8', 'i16', 'u16', 'i32', 'u32', 'i64', 'u64'):
            for op, symbol in [('div', '/'), ('rem', '%')]:
                failures[f'{op}_{t}_zero'] = f'fn main() {{ let z: {t} = 0; let discarded: {t} = (12 as {t}) {symbol} z; print(42); }}'
            if t.startswith('i'):
                failures[f'{t}_overflow'] = f'fn main() {{ let x: {t} = -{1 << (int(t[1:])-1)}; let n: {t} = -1; let discarded: {t} = x / n; print(42); }}'
        for name, declaration in [('prime2', 'type F = Field<2>;'), ('prime251', 'type F = Field<251>;'),
                                  ('binary', 'type F = BinField<8, 283>;'), ('binary4', 'type F = BinField<4, 19>;'),
                                  ('extension', 'type B = Field<251>; type F = ExtField<Field<251>, 2>;')]:
            failures[name] = declaration+' fn main() { let x: F = 1; let z: F = 0; let discarded: F = x / z; print(42); }'
        failures['dynamic'] = '''fn inverse<F: Field>(x: F) -> F { return 1 / x; }
instantiate inverse<dyn>;
fn main() { let f: Field = field(251); let discarded: i64 = inverse_dyn(f, 0 as i64) as i64; print(42); }'''
        failures['parallel'] = '''fn main() {
let a: *i64 = alloc(4096); let b: *i64 = alloc(4096); let out: *i64 = alloc(4096);
var j: i32 = 0; while j < 4096 { a[j] = 12; b[j] = 1; j = j + 1; } b[2077] = 0;
for i in 0..4096 { let discarded: i64 = a[i] / b[i]; out[i] = i as i64; }
print(42); }'''
        for name, text in failures.items():
            source, binaries = compile_case(name, text)
            if name == 'parallel': assert 'define internal void @__pfor_worker_' in (d/f'{name}.ll').read_text()
            for command in [(a.tvc, source, '--eval'), *((b,) for b in binaries)]:
                for threads in ((1, 4) if name == 'parallel' else (1,)):
                    result = run(*command, ok=False, threads=threads)
                    if name == 'extension' and '--eval' in command:
                        assert result.returncode == 97 and 'eval-refused: extfield' in result.stderr
                        continue
                    status = 128-result.returncode if result.returncode < 0 else result.returncode
                    assert status == 134 and not result.stdout, (name, command, threads, result)
        for name, text in {
            'carrier': 'fn main() { let x = 0 as Field; }',
            'cross': 'type A = Field<251>; type B = Field<7>; fn main() { let a: A = 250; let b: B = a as B; print(b); }',
            'implicit': 'type A = Field<251>; type B = Field<7>; fn main() { let a: A = 250; let b: B = a; print(b); }',
            'wide': 'type F = Field<251>; fn main() { let a: u128 = 18446744073709551616; let b: F = a as F; print(b); }',
            'wide_dynamic': 'fn bad<F: Field>(a: u128) -> F { return a as F; } instantiate bad<dyn>; fn main() {}',
        }.items():
            source = d/f'{name}.tv'; source.write_text(text); output = d/f'{name}.ll'; output.write_text('preserved')
            result = run(a.tvc, source, '-o', output, ok=False)
            assert result.returncode != 0 and 'unsupported' in result.stderr and output.read_text() == 'preserved', (name, result)
        source = d/'device.tv'
        source.write_text('#[kernel] fn arithmetic(i: i32, x: *i64, n: *i64, out: *i64) { out[i] = x[i] / n[i] + (x[i] >> n[i]); }')
        targets = run(a.llc, '--version').stdout
        for flag, cpu, target in [('--emit-gpu-nvptx', 'sm_90', 'nvptx64'), ('--emit-gpu', 'gfx1100', 'amdgcn')]:
            if target not in targets: continue
            run(a.tvc, source, flag, '-o', d/f'{cpu}.ll')
            text = (d/f'{cpu}.ll').read_text()
            assert 'call void @llvm.trap()' in text and '@abort' not in text
            run(a.opt, '-passes=default<O1>', '-S', d/f'{cpu}.ll', '-o', d/f'{cpu}-o1.ll')
            run(a.llc, f'-mcpu={cpu}', d/f'{cpu}-o1.ll', '-o', d/f'{cpu}.s')
        if a.cuda:
            cuda_package.build(d/'sm_90.ll', d/'arithmetic.tvcp', a.llc, 90)
            lines = [f'import "{ROOT}/tests/gpu/cuda_resident_checks.tv";',
                     'fn main(argc: i32, argv: **u8) -> i32 {',
                     'let device: u64 = resident_need(cuda_device_open(0));',
                     'let module: u64 = resident_need(cuda_module_load(device, argv[1]));',
                     'let kernel: u64 = resident_need(cuda_kernel_get_owner(module, "arithmetic"));',
                     'let x: *i64 = alloc(32); let n: *i64 = alloc(32); let out: *i64 = alloc(32);']
            wanted = []
            for i in range(32):
                x = -37 + 3*i; n = (1, 2, 63, 64, 65, -1)[i % 6]
                wanted.append(quotient(x, n) + (x >> (n if 0 <= n < 64 else 63)))
                lines.append(f'x[{i}] = {x}; n[{i}] = {n};')
            lines += ['if argc > 2 { n[0] = 0; }',
                      'if argc > 3 { x[0] = -9223372036854775808; n[0] = -1; }',
                      'let args: *CudaArgument = alloc(3);']
            for name in ('x', 'n', 'out'):
                lines += [f'let b_{name}: u64 = resident_need(cuda_buffer_alloc(device, 256));',
                          f'let v_{name}: CudaArgument = resident_typed_view(b_{name}, 0, 256, 64);',
                          f'resident_bind(kernel, args, "{name}", v_{name});']
                if name != 'out': lines.append(f'resident_need(cuda_upload(v_{name}, {name} as *u8, 256));')
            lines += ['match cuda_launch_sync(kernel, 0, 32, args, 3) {',
                      'Result::Ok(value) => { if argc > 2 { return 81; } },',
                      'Result::Err(error) => { if argc <= 2 || error.boundary != 8 || error.status <= 0 { return 82; } print(error.status); return 0; },',
                      '}', 'resident_need(cuda_download(out as *u8, v_out, 256));',
                      'var i: i32 = 0; while i < 32 { print(out[i]); i = i + 1; }']
            for name in ('x', 'n', 'out'): lines.append(f'resident_need(cuda_buffer_close(b_{name})); free({name});')
            lines += ['free(args); resident_need(cuda_module_close(module)); resident_need(cuda_device_close(device)); return 0; }']
            (d/'native.tv').write_text('\n'.join(lines))
            run(a.tvc, d/'native.tv', '-o', d/'native.ll')
            run(a.llc, '-filetype=obj', d/'native.ll', '-o', d/'native.o')
            run(a.link, '-no-pie', d/'native.o', a.cuda, f'-Wl,-rpath,{Path(a.cuda).resolve().parent}', '-o', d/'native')
            assert list(map(int, run(d/'native', d/'arithmetic.tvcp').stdout.splitlines())) == wanted
            for args in [('zero',), ('overflow', 'signed')]:
                assert int(run(d/'native', d/'arithmetic.tvcp', *args).stdout.strip()) > 0
            print('Native arithmetic PASS: 32 exact results; division-by-zero and signed-overflow kernel failures in separate processes')
        print(f'Arithmetic contract PASS: {len(expected)} exact values; {len(failures)} failure cases across supported eval/raw/O1; parallel failures; refused casts; available GPU lowering')


if __name__ == '__main__': main()
