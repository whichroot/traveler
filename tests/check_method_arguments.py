"""Keep receiver and explicit arguments contiguous across nested calls."""
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile

tvc, llc, opt, linker = sys.argv[1:5]
source = '''
struct Acc { total: i32 }
trait Add2 { fn add2(self, a: i32, b: i32) -> i32; }
impl Add2 for Acc {
    fn add2(self, a: i32, b: i32) -> i32 { return a * 100 + b; }
}
fn twice(x: i32) -> i32 { return x * 2; }
var sequence: i32 = 0;
fn next() -> i32 { sequence = sequence + 1; return sequence; }
fn main() {
    var s: Acc = Acc { total: 0 };
    var t: Acc = Acc { total: 1 };
    print(s.add2(1, 2));
    print(s.add2(twice(7), 2));
    print(s.add2(1, twice(7)));
    print(s.add2(twice(7), twice(8)));
    print(s.add2(t.add2(1, twice(2)), t.add2(twice(3), 7)));
    print(s.add2(twice(next()), twice(next())));
    print(sequence);
}
'''
expected = '102\n1402\n114\n1416\n11007\n204\n2\n'


def run(*args):
    return subprocess.run(list(map(str, args)), capture_output=True, text=True, timeout=60)


def ok(*args):
    result = run(*args)
    assert result.returncode == 0, (args, result.returncode, result.stdout, result.stderr)
    return result.stdout


with tempfile.TemporaryDirectory(prefix='method-arguments-') as tmp:
    d = Path(tmp)
    tv, ir = d / 'methods.tv', d / 'methods.ll'
    tv.write_text(source)
    assert ok(tvc, tv, '--eval') == expected
    ok(tvc, tv, '-o', ir)
    flags = shlex.split(os.environ.get('TRAVELER_LINK_FLAGS', '-no-pie' if sys.platform == 'linux' else ''))
    for profile in ('raw', 'O1', 'O3'):
        selected = ir
        if profile != 'raw':
            selected = d / (profile + '.ll')
            ok(opt, '-passes=default<' + profile + '>', '-S', ir, '-o', selected)
        ok(llc, '-filetype=obj', selected, '-o', d / 'methods.o')
        ok(linker, *flags, d / 'methods.o', '-o', d / 'methods')
        assert ok(d / 'methods') == expected, profile
    tv.write_text('struct S { x: i32 }\nfn main() { var s: S; s.f(' + ','.join('1' for _ in range(64)) + '); }')
    refused = run(tvc, tv, '-o', d / 'refused.ll')
    assert refused.returncode != 0 and 'call arguments' in refused.stderr, refused
    print('method arguments PASS: nested calls, evaluation order, eval/raw/O1/O3, capacity refusal')
