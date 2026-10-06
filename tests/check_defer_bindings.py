"""Bind deferred names at declaration while reading live values at exit."""
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile

tvc, llc, opt, linker = sys.argv[1:5]
source = '''
fn mark(p: *i32) { p[0] = p[0] + 1; }
fn branch(a: *i32, b: *i32, which: i32) -> i32 {
    let p: *i32 = a;
    defer { let temp: i32 = 3; mark(p); }
    if which == 1 { let p: *i32 = b; return 7; }
    if which == 2 { let p: *i32 = b; return 8; }
    return 9;
}
fn live(a: *i32, b: *i32) {
    var p: *i32 = a;
    defer mark(p);
    p = b;
    if 1 == 1 { let p: *i32 = a; return; }
}
fn late() {
    let p: i32 = 3;
    defer print(p);
    let p: i32 = 4;
    defer print(p);
}
fn locals() {
    let p: i32 = 5;
    defer print(p);
    defer let p: i32 = 99;
    defer { let p: i32 = 6; print(p); }
}
fn main() {
    let a: *i32 = alloc(1);
    let b: *i32 = alloc(1);
    print(branch(a, b, 1));
    print(branch(a, b, 2));
    print(branch(a, b, 0));
    live(a, b);
    print(a[0]); print(b[0]);
    late(); locals();
    free(a); free(b);
}
'''
expected = '7\n8\n9\n3\n1\n4\n3\n6\n5\n'


def run(*args):
    p = subprocess.run(list(map(str, args)), capture_output=True, text=True, timeout=60)
    assert p.returncode == 0, (args, p.returncode, p.stdout, p.stderr)
    return p.stdout


with tempfile.TemporaryDirectory(prefix='defer-bindings-') as tmp:
    d = Path(tmp)
    tv, ir = d / 'bindings.tv', d / 'bindings.ll'
    tv.write_text(source)
    assert run(tvc, tv, '--eval') == expected
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
            assert run(d / 'job') == expected, (debug, profile)
    print('defer bindings PASS: exit shadows, live mutation, later declarations, cleanup locals; eval/raw/O1/O3 and redzones')
