"""Check struct snapshots against value semantics, including explicit aliases."""
from pathlib import Path
import platform
import subprocess
import sys
import tempfile

tvc, llc, opt, link = sys.argv[1:5]
header = '''
struct Sample { value: u64, pointer: *u64 }
struct Outer { sample: Sample, other: u64 }
struct Box<T> { value: T }
fn read_once(source: *Sample, count: *u64) -> *Sample {
    count[0] = count[0]+1; return source;
}
fn identity(value: Sample) -> Sample { return value; }
'''
cases = {}
for binding in ('let', 'var'):
    for name, expression in (('index', 'source[0]'), ('member', 'outer.sample'), ('local', 'local')):
        for typed in (False, True):
            annotation = ': Sample' if typed else ''
            cases[f'{binding}_{name}_{typed}'] = f'''
let payload: *u64 = alloc(1); payload[0] = 10;
let source: *Sample = alloc(1); source[0] = Sample {{ value: 7, pointer: payload }};
var local: Sample = Sample {{ value: 7, pointer: payload }};
var outer: Outer = Outer {{ sample: local, other: 0 }};
{binding} snapshot{annotation} = {expression};
source[0].value = 99; local.value = 99; outer.sample.value = 99;
print(snapshot.value);
snapshot.pointer[0] = 11; print(payload[0]);
free(source); free(payload);
''', '7\n11\n'
cases['mutable_writeback'] = '''
let source: *Sample = alloc(1); source[0] = Sample { value: 7, pointer: null };
var snapshot: Sample = source[0]; snapshot.value = 42;
print(source[0].value); print(snapshot.value);
let alias: *Sample = source; alias[0].value = 19; print(source[0].value);
snapshot = Sample { value: 23, pointer: null }; print(source[0].value);
free(source);
''', '7\n42\n19\n19\n'
cases['eval_snapshot'] = '''
var source: Sample = Sample { value: 7, pointer: null };
let snapshot = source; source.value = 42; print(snapshot.value);
''', '7\n'
cases['nested'] = '''
var outer: Outer = Outer { sample: Sample { value: 7, pointer: null }, other: 9 };
let snapshot: Outer = outer; let inner = outer.sample;
outer.sample.value = 42; outer.other = 13;
print(snapshot.sample.value); print(snapshot.other); print(inner.value);
''', '7\n9\n7\n'
cases['generic'] = '''
var source: Box<u64> = Box { value: 7 };
let snapshot = source; source.value = 42; print(snapshot.value);
''', '7\n'
cases['once_return_loop'] = '''
let source: *Sample = alloc(1); let count: *u64 = alloc(1);
source[0] = Sample { value: 7, pointer: null }; count[0] = 0;
let snapshot = read_once(source, count)[0];
let returned = identity(source[0]); source[0].value = 99;
print(count[0]); print(snapshot.value); print(returned.value);
for i in 0..3 {
    source[0].value = i as u64;
    let item = source[0]; source[0].value = 50; print(item.value);
}
free(source); free(count);
''', '1\n7\n7\n0\n1\n2\n'


def run(*args):
    p = subprocess.run([str(a) for a in args], capture_output=True, text=True, timeout=90)
    assert p.returncode == 0, (args, p.stdout, p.stderr)
    return p.stdout


with tempfile.TemporaryDirectory(prefix='traveler-struct-copy-') as directory:
    d = Path(directory)
    for name, (body, expected) in cases.items():
        source = d / f'{name}.tv'
        source.write_text(header + '\nfn main() -> i32 {\n' + body + '\nreturn 0;\n}\n')
        run(tvc, source, '--opt-level', 'none', '-o', d / 'raw.ll')
        if name == 'eval_snapshot':
            assert run(tvc, '--eval', source) == expected
        for profile in ('raw', 'O1', 'O3'):
            ir = d / f'{profile}.ll'
            if profile != 'raw':
                run(opt, f'-passes=default<{profile}>', '-verify-each', '-S', d / 'raw.ll', '-o', ir)
            run(llc, '-filetype=obj', ir, '-o', d / 'case.o')
            flags = ['-no-pie'] if platform.system() == 'Linux' else []
            run(link, *flags, d / 'case.o', '-o', d / 'case')
            actual = run(d / 'case')
            assert actual == expected, (name, profile, expected, actual)
    print(f'Struct copies PASS: {len(cases)} cases, raw/O1/O3, snapshots, mutable copies, pointer aliases, nested/generic values')
