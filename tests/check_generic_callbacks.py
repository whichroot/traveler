"""Generic callback signatures retain parameter and return type structure."""
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile

tvc, llc, opt, linker = sys.argv[1:5]


def run(*args):
    p = subprocess.run(list(map(str, args)), capture_output=True, text=True, timeout=60)
    assert p.returncode == 0, (args, p.returncode, p.stdout, p.stderr)
    return p.stdout


SOURCE = '''
struct Box<T> { val: T }
fn unbox(b: Box<i32>) -> i32 { return b.val; }
fn apply<T>(cb: fn(Box<T>)->i32, v: Box<T>) -> i32 { return cb(v); }
fn inc(v: i64) -> i64 { return v + 1; }
fn plain<T>(cb: fn(T)->T, v: T) -> T { return cb(v); }
fn invoke(cb: fn(i64)->i64, v: i64) -> i64 { return cb(v); }
fn higher<T>(cb: fn(fn(T)->T,T)->T, f: fn(T)->T, v: T) -> T { return cb(f,v); }
fn make(v: i64) -> *Box<i64> { let p: *Box<i64> = alloc(1); p[0] = Box { val: v }; return p; }
fn produce<T>(cb: fn(T)->*Box<T>, v: T) -> T { let b: *Box<T> = cb(v); let result: T = b[0].val; free(b); return result; }
fn get() -> i64 { return 1099511627781; }
fn zero<T>(cb: fn()->T, seed: T) -> T { return cb(); }
fn from_ptr(p: *i64) -> i64 { return p[0]; }
fn pointer<T>(cb: fn(*T)->T, p: *T) -> T { return cb(p); }
fn main() {
    let b: Box<i32> = Box { val: 9 }; print(apply(&unbox,b));
    var v: i64 = 1099511627776;
    print(plain(&inc,v)); print(higher(&invoke,&inc,v));
    print(produce(&make,v)); print(zero(&get,v)); print(pointer(&from_ptr,&v));
}
'''
EXPECTED = '9\n1099511627777\n1099511627777\n1099511627776\n1099511627781\n1099511627776\n'

with tempfile.TemporaryDirectory(prefix='generic-callbacks-') as tmp:
    d = Path(tmp)
    source, ir = d / 'case.tv', d / 'case.ll'
    source.write_text(SOURCE)
    run(tvc, source, '-o', ir)
    flags = shlex.split(os.environ.get('TRAVELER_LINK_FLAGS', '-no-pie' if sys.platform == 'linux' else ''))
    for profile in ('raw', 'O1', 'O3'):
        selected = ir
        if profile != 'raw':
            selected = d / 'opt.ll'
            run(opt, '-passes=default<' + profile + '>', '-S', ir, '-o', selected)
        run(llc, '-filetype=obj', selected, '-o', d / 'case.o')
        run(linker, *flags, d / 'case.o', '-o', d / 'case')
        actual = run(d / 'case')
        assert actual == EXPECTED, (profile, actual)
print('generic callbacks PASS: generic aggregate inputs/pointer returns, scalar, nested callback, zero-argument and pointer signatures; raw/O1/O3')
