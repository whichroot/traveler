"""Function-pointer aggregate results use the same storage convention as direct calls."""
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile

tvc, llc, opt, linker = sys.argv[1:5]
SOURCE = '''
struct Box<T> { val: T }
struct Wide { lo: i64, high: u128, end: i64 }
enum Event { Empty, Value(Wide) }
enum Option<T> { None, Some(T) }
fn make_box(v:i64)->Box<i64> { return Box { val:v }; }
fn produce<T>(cb:fn(T)->Box<T>,v:T)->Box<T> { return cb(v); }
fn make_wide(v:i64)->Wide { return Wide { lo:v, high:(1 as u128)<<100, end:v+7 }; }
fn make_event(v:i64)->Event { return Event::Value(make_wide(v)); }
fn make_empty(v:i64)->Event { return Event::Empty; }
fn make_option(v:i64)->Option<i64> { return Option::Some(v); }
fn inspect(w:Wide)->i64 {
    if w.high != (1 as u128)<<100 { return 0-100; }
    return w.lo+w.end;
}
fn inspect_event(e:Event)->i64 {
    match e { Event::Empty => { return 0; } Event::Value(w) => { return inspect(w); } }
    return 0-1;
}
fn main() {
    let bc:fn(i64)->Box<i64> = &make_box;
    let b:Box<i64> = produce(bc,1099511627776); print(b.val);
    let wc:fn(i64)->Wide = &make_wide;
    let saved:Wide = wc(10);
    var i:i64=0;
    while i<3 { let w:Wide=wc(i); print(inspect(w)); i=i+1; }
    print(inspect(saved)); print(inspect(wc(20)));
    var ec:fn(i64)->Event = &make_event;
    let e:Event=ec(30); ec=&make_empty; print(inspect_event(ec(0))); print(inspect_event(e));
    let oc:fn(i64)->Option<i64> = &make_option;
    let o:Option<i64> = oc(41);
    match o { Option::None => { print(0); } Option::Some(v) => { print(v); } }
}
'''
EXPECTED = '1099511627776\n7\n9\n11\n27\n47\n0\n67\n41\n'


def run(*args):
    p = subprocess.run(list(map(str, args)), capture_output=True, text=True, timeout=60)
    assert p.returncode == 0, (args, p.returncode, p.stdout, p.stderr)
    return p.stdout


with tempfile.TemporaryDirectory(prefix='indirect-aggregate-') as tmp:
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
    if os.environ.get('LLVM14'):
        tools = Path(os.environ['LLVM14']) / 'bin'
        triple = next(line.split(':', 1)[1].strip() for line in run(tools / 'llc', '--version').splitlines()
                      if 'Default target:' in line)
        run(tvc, source, '-target', 'tpc', '-o', ir)
        for profile in ('raw', 'O3'):
            selected = ir
            if profile != 'raw':
                selected = d / 'typed-opt.ll'
                run(tools / 'opt', '-mtriple=' + triple, '-O3', '-S', ir, '-o', selected)
            run(tools / 'llc', '-mtriple=' + triple, '-filetype=obj', selected, '-o', d / 'case.o')
            run(linker, *flags, d / 'case.o', '-o', d / 'case')
            assert run(d / 'case') == EXPECTED, ('LLVM14', profile)
        print('indirect aggregate returns: LLVM14 typed pointers raw/O3 PASS')
print('indirect aggregate returns PASS: generic structs/enums, wide payloads, forwarded returns, repeated calls and snapshots; raw/O1/O3')
