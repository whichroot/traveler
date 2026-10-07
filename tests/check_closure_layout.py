"""Closure environments must hold full value snapshots with native alignment."""
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile

tvc, llc, opt, linker = sys.argv[1:5]
definitions, calls, expected = [], [], ''
for width in (128, 256, 512):
    definitions.append(f'''
fn wide{width}() {{
    let guard_a: i64 = 111;
    var big: u{width} = ((1 as u{width}) << {width - 8}) + (5 as u{width});
    let guard_b: i64 = 222;
    let other: u{width} = 17;
    let small: u8 = 3;
    let f = |x: u{width}| -> u{width} big + x + other + (small as u{width});
    big = 99 as u{width};
    let result: u{width} = f(1 as u{width});
    print(result as u64);
    print((result >> {width - 8}) as u64);
    print(guard_a); print(guard_b);
}}
''')
    calls.append(f'wide{width}();')
    expected += '26\n1\n111\n222\n'
definitions.append('''
type F = Field<65537>;
type Gold = Field<18446744069414584321>;
type Extension = ExtField<Field<18446744069414584321>,2>;
fn extension() {
    var value: Extension = 5;
    let marker: i64 = 9;
    let f = |unused: i64| -> i64 { print(value); return marker; };
    value = 7;
    print(f(0));
}
fn polynomials() {
    let p: Poly<F,3> = poly(1, 2, 3, 4);
    let r: Register<F,3> = register(1, 2, 3, 4);
    let f = |unused: i64| -> i64 (eval(p, 2) as i64) + (eval(r, 2) as i64);
    print(f(0));
}
struct Inner { a: i64, b: i64, c: i64 }
struct Outer { tag: u8, inner: Inner, last: i64 }
enum Event { Value(Outer, i64) }
fn event_value(e: Event) -> i64 {
    var result: i64 = 0;
    match e { Event::Value(v, marker) => { result = v.inner.c + v.last + marker; } }
    return result;
}
fn answer() -> i64 { return 29; }
var global_values: [i64; 3] = 0;
fn aggregates() {
    var record: Outer = Outer { tag: 1, inner: Inner { a: 2, b: 3, c: 13 }, last: 17 };
    var values: [i64; 3] = 0; values[2] = 19;
    global_values[2] = 23;
    let event: Event = Event::Value(record, 31);
    let callback: fn()->i64 = &answer;
    let f = |unused: i64| -> i64 record.inner.c + record.last + values[2]
        + global_values[2] + event_value(event) + callback();
    record.inner.c = 100; record.last = 200;
    values[2] = 300; global_values[2] = 400;
    print(f(0));
}
''')
calls.append('aggregates();')
expected += '162\n'
calls.append('polynomials();')
expected += '57\n'
calls.append('extension();')
expected += '5\n0\n9\n'


def run(*args):
    p = subprocess.run(list(map(str, args)), capture_output=True, text=True, timeout=60)
    assert p.returncode == 0, (args, p.returncode, p.stdout, p.stderr)
    return p.stdout


with tempfile.TemporaryDirectory(prefix='closure-layout-') as tmp:
    d = Path(tmp)
    tv, ir = d / 'capture.tv', d / 'capture.ll'
    tv.write_text('\n'.join(definitions) + '\nfn main() { ' + ' '.join(calls) + ' }')
    flags = shlex.split(os.environ.get('TRAVELER_LINK_FLAGS', '-no-pie' if sys.platform == 'linux' else ''))
    for debug in (False, True):
        run(tvc, tv, '-o', ir, *(['--alloc-debug'] if debug else []))
        for profile in ('raw', 'O1', 'O3'):
            selected = ir
            if profile != 'raw':
                selected = d / 'optimized.ll'
                run(opt, '-passes=default<' + profile + '>', '-S', ir, '-o', selected)
            run(llc, '-filetype=obj', selected, '-o', d / 'job.o')
            run(linker, *flags, d / 'job.o', '-o', d / 'job')
            output = run(d / 'job')
            assert output == expected, (debug, profile, output, expected)
    if os.environ.get('LLVM14'):
        tools = Path(os.environ['LLVM14']) / 'bin'
        triple = next(line.split(':', 1)[1].strip() for line in
                      run(tools / 'llc', '--version').splitlines() if 'Default target:' in line)
        run(tvc, tv, '-target', 'tpc', '-o', ir)
        for profile in ('raw', 'O3'):
            selected = ir
            if profile == 'O3':
                selected = d / 'typed-optimized.ll'
                run(tools / 'opt', '-mtriple=' + triple, '-O3', '-S', ir, '-o', selected)
            run(tools / 'llc', '-mtriple=' + triple, '-filetype=obj', selected, '-o', d / 'job.o')
            run(linker, *flags, d / 'job.o', '-o', d / 'job')
            assert run(d / 'job') == expected, ('LLVM14', profile)
print('closure layout PASS: wide/struct/array/enum/function-pointer snapshots, neighbor guards; raw/O1/O3/redzones')
