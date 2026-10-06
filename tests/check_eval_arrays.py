"""Allocate evaluator local arrays with the same stride used for access."""
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile

tvc, llc, opt, linker = sys.argv[1:5]
source = '''
struct Pair { x: i64, y: i64 }
struct Outer { tag: u8, x: i64, y: i64, last: i64 }
enum Event { Empty, Value(i64) }
fn main() {
    var pairs: [Pair; 8] = 0;
    var outer: [Outer; 8] = 0;
    var events: [Event; 8] = 0;
    let p: *Pair = &pairs[7];
    let q: *Outer = &outer[7];
    print(p.x);
    print(q.last);
    var i: i32 = 0;
    while i < 8 {
        pairs[i] = Pair { x: i as i64, y: 2 };
        outer[i] = Outer { tag: 1, x: i as i64, y: 3, last: 99 };
        i = i + 1;
    }
    print(p.x); print(p.y);
    print(q.x); print(q.y); print(q.last);
    let event: *u8 = (&events[7]) as *u8;
    print(event[0] as i32);
}
'''
expected = '0\n0\n7\n2\n7\n3\n99\n0\n'


def run(*args):
    p = subprocess.run(list(map(str, args)), capture_output=True, text=True, timeout=60)
    assert p.returncode == 0, (args, p.returncode, p.stdout, p.stderr)
    return p.stdout


with tempfile.TemporaryDirectory(prefix='eval-arrays-') as tmp:
    d = Path(tmp)
    tv, ir = d / 'arrays.tv', d / 'arrays.ll'
    tv.write_text(source)
    assert run(tvc, tv, '--eval') == expected
    run(tvc, tv, '-o', ir)
    flags = shlex.split(os.environ.get('TRAVELER_LINK_FLAGS', '-no-pie' if sys.platform == 'linux' else ''))
    for profile in ('raw', 'O1', 'O3'):
        selected = ir
        if profile != 'raw':
            selected = d / 'optimized.ll'
            run(opt, '-passes=default<' + profile + '>', '-S', ir, '-o', selected)
        run(llc, '-filetype=obj', selected, '-o', d / 'job.o')
        run(linker, *flags, d / 'job.o', '-o', d / 'job')
        assert run(d / 'job') == expected, profile
    print('eval aggregate arrays PASS: zero state, last-element writes, padded layout, enums; eval/raw/O1/O3')
