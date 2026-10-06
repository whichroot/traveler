"""Enum payload reservations must cover native aggregate layouts and padding."""
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile

tvc, llc, opt, linker = sys.argv[1:5]
types = '''
type F = Field<18446744069414584321>;
type E = ExtField<Field<18446744069414584321>,2>;
struct Inner { a: i64, b: i64, c: i64 }
struct Nested { inner: Inner, last: i64 }
struct Arrays { tag: u8, data: [Inner; 3], last: i64 }
struct Pointers { tag: u8, data: [*u8; 3], last: i64 }
struct Wide { tag: u8, data: u256, last: i64 }
struct Extension { tag: u8, data: E, last: i64 }
struct Polynomial { tag: u8, data: Poly<F,3>, last: i64 }
struct Registers { tag: u8, data: Register<F,4>, last: i64 }
enum Small { None, Some(i64) }
struct Enums { tag: u8, data: Small, last: i64 }
enum Box<T> { Value(T, i64) }
enum Plain { Value(Nested, i64) }
enum Bytes { Value([u8; 3], u8) }
enum EmptyBytes { Value([u8; 0], u8) }
enum Callback { Value(fn()->i64) }
fn answer() -> i64 { return 121; }
enum Result<T,E> { Ok(T), Err(E) }
fn wide_result(p: *Wide) -> Result<Wide,i32> { return Result::Ok(p[0]); }
fn wide_last(p: *Wide) -> Result<i64,i32> {
    let v: Wide = wide_result(p)?;
    return Result::Ok(v.last);
}
'''
statements, expected = [], ''
for i, name in enumerate(('Nested', 'Arrays', 'Pointers', 'Wide', 'Extension', 'Polynomial', 'Registers', 'Enums')):
    statements.append(f'''
    let p{i}: *{name} = alloc(2); p{i}[1].last = {41+i};
    let e{i}: Box<{name}> = Box::Value(p{i}[1], {101+i});
    match e{i} {{ Box::Value(v, marker) => {{ print(v.last); print(marker); }} }}
    free(p{i});
''')
    expected += f'{41+i}\n{101+i}\n'
statements.append('''
    let p: *Nested = alloc(1); p[0].last = 71; p[0].inner.c = 13;
    let e: Plain = Plain::Value(p[0], 72);
    match e { Plain::Value(v, n) => { print(v.last); print(v.inner.c); print(n); } }
    free(p);
    var bytes: [u8; 3] = 0; bytes[2] = 31;
    let b: Bytes = Bytes::Value(bytes, 32);
    match b { Bytes::Value(v, n) => { print(v[2] as i32); print(n as i32); } }
    var empty: [u8; 0] = 0;
    let eb: EmptyBytes = EmptyBytes::Value(empty, 33);
    match eb { EmptyBytes::Value(v, n) => { print(n as i32); } }
    let w: *Wide = alloc(1); w[0].last = 91;
    let r: Result<i64,i32> = wide_last(w);
    match r { Result::Ok(v) => { print(v); } Result::Err(e) => { print(e); } }
    free(w);
    let cb: Callback = Callback::Value(&answer);
    match cb { Callback::Value(f) => { print(f()); } }
''')
expected += '71\n13\n72\n31\n32\n33\n91\n121\n'


def run(*args):
    p = subprocess.run(list(map(str, args)), capture_output=True, text=True, timeout=60)
    assert p.returncode == 0, (args, p.returncode, p.stdout, p.stderr)
    return p.stdout


with tempfile.TemporaryDirectory(prefix='enum-aggregate-layout-') as tmp:
    d = Path(tmp)
    tv, ir = d / 'layout.tv', d / 'layout.ll'
    tv.write_text(types + '\nfn main() {\n' + '\n'.join(statements) + '\n}')
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
        version = run(tools / 'llc', '--version')
        triple = next(line.split(':', 1)[1].strip() for line in version.splitlines() if 'Default target:' in line)
        tv.write_text('''
struct Inner { a: i64, b: i64, c: i64 }
struct Outer { inner: Inner, last: i64 }
enum Event { Value(Outer, i64) }
enum Callback { Value(fn()->i64) }
fn answer() -> i64 { return 121; }
fn main() {
    let p: *Outer = alloc(1); p[0].inner.c = 13;
    let e: Event = Event::Value(p[0], 101);
    match e { Event::Value(v, marker) => { print(v.inner.c); print(marker); } }
    let cb: Callback = Callback::Value(&answer);
    match cb { Callback::Value(f) => { print(f()); } }
    free(p);
}
''')
        typed = d / 'typed.ll'
        run(tvc, tv, '-target', 'tpc', '-o', typed)
        for profile in ('raw', 'O3'):
            selected = typed
            if profile == 'O3':
                selected = d / 'typed-optimized.ll'
                run(tools / 'opt', '-mtriple=' + triple, '-O3', '-S', typed, '-o', selected)
            run(tools / 'llc', '-mtriple=' + triple, '-filetype=obj', selected, '-o', d / 'typed.o')
            run(linker, *flags, d / 'typed.o', '-o', d / 'typed')
            assert run(d / 'typed') == '13\n101\n121\n', ('LLVM14', profile)
        print('enum aggregate layout: LLVM14 typed pointers raw/O3 PASS')
    for invalid, diagnostic in (
        ('enum Huge { Value([u8; 2147483647]) } fn main() {}', 'enum payload storage exceeds'),
        ('struct Recursive { next: Recursive } enum Bad { Value(Recursive) } fn main() {}',
         'recursive by-value struct payload'),
    ):
        tv.write_text(invalid)
        ir.write_text('existing artifact\n')
        p = subprocess.run([tvc, str(tv), '-o', str(ir)], capture_output=True, text=True, timeout=60)
        assert p.returncode == 1 and diagnostic in p.stderr, (invalid, p)
        assert ir.read_text() == 'existing artifact\n'
    print('enum aggregate layout PASS: nested/array/pointer/wide/field/poly/register/enum fields, generic/plain payloads, alignment, propagation; raw/O1/O3/redzones')
