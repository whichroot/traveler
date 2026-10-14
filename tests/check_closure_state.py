"""Lifted closures have their own function identity, defers, and loop context."""
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile

tvc, llc, opt, linker = sys.argv[1:5]
cases = {
    'defer': ('''fn main() {
        defer print(99);
        let f = |x:i32| -> i32 { return x+1; };
        print(f(4));
    }''', '5\n99\n'),
    'last-function': ('''fn main() {
        let f = |x:i32| -> i32 { return x+1; }; print(f(6));
    }
    fn unused() { defer print(88); }''', '7\n'),
    'bare-return': ('''fn main() {
        let f = |x:i32| { print(x); return; }; f(13);
    }''', '13\n'),
    'question-exit': ('''enum Result<T,E> { Ok(T), Err(E) }
    fn maybe(x:i32)->Result<i32,i32> {
        if x==0 { return Result::Err(7); } return Result::Ok(x);
    }
    fn main() {
        defer print(99);
        let f = |x:i32| -> Result<i32,i32> {
            let y:i32=maybe(x)?; print(y); return Result::Ok(y);
        };
        f(0); f(5); print(42);
    }''', '5\n42\n99\n'),
    'local-loops': ('''fn main() {
        defer print(77);
        let f = |x:i32| -> i32 {
            var j:i32=0; var sum:i32=0;
            while j<x { j=j+1; if j==2 { continue; } if j==5 { break; } sum=sum+j; }
            return sum;
        };
        print(f(8));
        let g = |x:i32| -> i32 { return x+10; }; print(g(3));
    }''', '8\n13\n77\n'),
}


def run(*args):
    p = subprocess.run(list(map(str, args)), capture_output=True, text=True, timeout=60)
    assert p.returncode == 0, (args, p.returncode, p.stdout, p.stderr)
    return p.stdout


with tempfile.TemporaryDirectory(prefix='closure-state-') as tmp:
    d = Path(tmp)
    source, ir = d / 'case.tv', d / 'case.ll'
    flags = shlex.split(os.environ.get('TRAVELER_LINK_FLAGS', '-no-pie' if sys.platform == 'linux' else ''))
    for name, (text, expected) in cases.items():
        source.write_text(text)
        run(tvc, source, '-o', ir)
        for profile in ('raw', 'O1', 'O3'):
            selected = ir
            if profile != 'raw':
                selected = d / 'opt.ll'
                run(opt, '-passes=default<' + profile + '>', '-S', ir, '-o', selected)
            run(llc, '-filetype=obj', selected, '-o', d / 'case.o')
            run(linker, *flags, d / 'case.o', '-o', d / 'case')
            actual = run(d / 'case')
            assert actual == expected, (name, profile, actual, expected)
    for keyword in ('break', 'continue'):
        source.write_text('fn main() { for i in 0..1 { let f = |x:i32| { ' + keyword + '; }; f(i); } }')
        p = subprocess.run([tvc, str(source), '-o', str(ir)], capture_output=True, text=True, timeout=60)
        assert p.returncode != 0 and f'{keyword} outside a loop' in p.stderr, (keyword, p.returncode, p.stderr)
print('closure state PASS: foreign defers, bare return, independent local loops, out-of-loop refusals; raw/O1/O3')
