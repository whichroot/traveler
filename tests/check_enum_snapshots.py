"""Enum bindings must copy values instead of sharing initializer storage."""
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile

tvc, llc, opt, linker = sys.argv[1:5]
source = '''
enum Event { Empty, Value(i64) }
enum Option<T> { None, Some(T) }
fn inspect(e: Event) -> i64 {
    match e { Event::Empty => { return 0; } Event::Value(n) => { return n; } }
    return 0;
}
fn main() {
    var a: Event = Event::Value(11);
    let b: Event = a;
    var c = b;
    let ap: *u8 = (&a) as *u8;
    let cp: *u8 = (&c) as *u8;
    ap[0] = 0; cp[0] = 0;
    print(inspect(b)); print(inspect(a)); print(inspect(c));
    var g: Option<i64> = Option::Some(29);
    let saved = g;
    let gp: *u8 = (&g) as *u8;
    gp[0] = 0;
    match saved { Option::None => { print(0); } Option::Some(n) => { print(n); } }
    REASSIGN
}
'''


def run(*args):
    p = subprocess.run(list(map(str, args)), capture_output=True, text=True, timeout=60)
    assert p.returncode == 0, (args, p.returncode, p.stdout, p.stderr)
    return p.stdout


with tempfile.TemporaryDirectory(prefix='enum-snapshots-') as tmp:
    d = Path(tmp)
    tv, ir = d / 'snapshots.tv', d / 'snapshots.ll'
    tv.write_text(source.replace('REASSIGN', ''))
    output = run(tvc, tv, '--eval')
    assert output == '11\n0\n0\n29\n', output
    tv.write_text(source.replace('REASSIGN', '''
    var original: Event = Event::Value(51);
    let snapshot = original;
    original = Event::Empty;
    print(inspect(snapshot));
'''))
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
            assert output == '11\n0\n0\n29\n51\n', (debug, profile, output)
    print('enum snapshots PASS: plain/generic bindings, inferred types, independent mutation, native reassignment; eval/raw/O1/O3/redzones')
