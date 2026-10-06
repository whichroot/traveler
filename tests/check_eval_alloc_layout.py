"""Check evaluator heap extents against aggregate access and realloc strides."""
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile

tvc, llc, opt, linker = sys.argv[1:5]
source = '''
struct Inner { a: i64, b: i64, c: i64 }
struct Outer { inner: Inner, last: i64 }
enum Event { Empty, Value(i64) }
fn main() {
    var p: *Outer = alloc(3);
    p[2].last = 71;
    print(p[2].last);
    p = realloc(p, 6);
    print(p[2].last);
    p[5].last = 93;
    print(p[5].last);
    free(p);
    var events: *Event = alloc(4);
    let tag: *u8 = (&events[3]) as *u8;
    tag[0] = 1;
    events = realloc(events, 8);
    let saved: *u8 = (&events[3]) as *u8;
    let last: *u8 = (&events[7]) as *u8;
    print(saved[0] as i32);
    last[15] = 99;
    print(last[15] as i32);
    free(events);
}
'''
expected = '71\n71\n93\n1\n99\n'


def run(*args):
    p = subprocess.run(list(map(str, args)), capture_output=True, text=True, timeout=60)
    assert p.returncode == 0, (args, p.returncode, p.stdout, p.stderr)
    return p.stdout


with tempfile.TemporaryDirectory(prefix='eval-alloc-layout-') as tmp:
    d = Path(tmp)
    tv, ir = d / 'layout.tv', d / 'layout.ll'
    tv.write_text(source)
    evaluated = run(tvc, tv, '--eval')
    assert evaluated == expected, evaluated
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
    print('eval heap layout PASS: nested structs, enum extents, realloc preservation; eval/raw/O1/O3/redzones')
