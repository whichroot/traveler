"""Reserve enum and variant slots before resolving nested payload instances."""
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile

tvc, llc, opt, linker = sys.argv[1:5]
source = '''
enum Opt<T> { None, Some(T) }
enum Outer { A(Opt<i32>), B(i64), C(Opt<i64>) }
enum Wrap<T> { Pair(Opt<T>, Opt<T>), Value(T) }
enum Link { End, Next(*Link) }
fn read_outer(x: Outer) -> i64 {
    match x {
        Outer::A(v) => { match v { Opt::None => { return 0; } Opt::Some(n) => { return n as i64; } } }
        Outer::B(n) => { return n; }
        Outer::C(v) => { match v { Opt::None => { return 0; } Opt::Some(n) => { return n; } } }
    }
    return 0;
}
fn main() {
    let a: Outer = Outer::A(Opt::Some(7));
    let b: Outer = Outer::B(8000000000);
    let c: Outer = Outer::C(Opt::Some(9000000000));
    print(read_outer(a)); print(read_outer(b)); print(read_outer(c));
    let pair: Wrap<i16> = Wrap::Pair(Opt::Some(11), Opt::Some(12));
    match pair {
        Wrap::Pair(a, b) => {
            match a { Opt::None => { print(0); } Opt::Some(n) => { print(n); } }
            match b { Opt::None => { print(0); } Opt::Some(n) => { print(n); } }
        }
        Wrap::Value(n) => { print(n); }
    }
    let link: Link = Link::Next(null);
    match link { Link::End => { print(0); } Link::Next(p) => { print((p == null) as i32); } }
}
'''
expected = '7\n8000000000\n9000000000\n11\n12\n1\n'


def run(*args, success=True):
    p = subprocess.run(list(map(str, args)), capture_output=True, text=True, timeout=60)
    if success:
        assert p.returncode == 0, (args, p.returncode, p.stdout, p.stderr)
    return p


with tempfile.TemporaryDirectory(prefix='nested-enums-') as tmp:
    d = Path(tmp)
    tv, ir = d / 'enums.tv', d / 'enums.ll'
    tv.write_text(source)
    run(tvc, tv, '-o', ir)
    flags = shlex.split(os.environ.get('TRAVELER_LINK_FLAGS', '-no-pie' if sys.platform == 'linux' else ''))
    for profile in ('raw', 'O1', 'O3'):
        selected = ir
        if profile != 'raw':
            selected = d / 'optimized.ll'
            run(opt, '-passes=default<' + profile + '>', '-S', ir, '-o', selected)
        run(llc, '-filetype=obj', selected, '-o', d / 'job.o')
        run(linker, *flags, d / 'job.o', '-o', d / 'job')
        assert run(d / 'job').stdout == expected, profile
    for bad in ('enum Bad { Again(Bad) }\nfn main() {}',
                'enum Bad<T> { Again(Bad<T>) }\nfn f(x: Bad<i32>) {}\nfn main() {}'):
        tv.write_text(bad)
        ir.write_text('existing artifact\n')
        refused = run(tvc, tv, '-o', ir, success=False)
        assert refused.returncode == 1 and 'recursive by-value enum payload' in refused.stderr, refused
        assert ir.read_text() == 'existing artifact\n'
    print('nested enums PASS: plain/generic outer layouts, distinct inner instances, pointer recursion, value-cycle refusals; raw/O1/O3')
