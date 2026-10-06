"""Resolve fall-through defers while function locals and parameters are live."""
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile

tvc, llc, opt, linker = sys.argv[1:5]
source = '''
var p: i32 = 900;
fn local() {
    let p: *i32 = alloc(1);
    p[0] = 7;
    defer { print(p[0]); free(p); }
    defer print(8);
}
fn parameter(p: i32) { defer print(p); }
fn explicit() {
    let p: i32 = 11;
    defer print(p);
    return;
}
fn updated() {
    var p: i32 = 1;
    defer print(p);
    p = 12;
}
fn main() {
    local();
    parameter(9);
    explicit();
    updated();
    print(p);
    let p: i32 = 13;
    defer print(p);
}
'''
expected = '8\n7\n9\n11\n12\n900\n13\n'


def run(*args):
    p = subprocess.run(list(map(str, args)), capture_output=True, text=True, timeout=60)
    assert p.returncode == 0, (args, p.returncode, p.stdout, p.stderr)
    return p.stdout


with tempfile.TemporaryDirectory(prefix='defer-fallthrough-') as tmp:
    d = Path(tmp)
    tv, ir = d / 'defer.tv', d / 'defer.ll'
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
    print('fall-through defer PASS: local/parameter scope, LIFO, mutation, explicit return, main; eval/raw/O1/O3 and redzones')
