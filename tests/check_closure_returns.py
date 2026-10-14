"""Expression-bodied closures obey declared return conversions."""
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile

tvc, llc, opt, linker = sys.argv[1:5]
SOURCE = '''
fn main() {
    let sign_extend = |x:i8| -> i64 x;
    let zero_extend = |x:u8| -> u64 x;
    let narrow = |x:i64| -> i8 x;
    let compare = |x:i32| -> bool x<5;
    let widen_value = |x:i32| -> i128 x;
    let block = |x:i8| -> i64 { return x; };
    let discard = |x:i32| print(x);
    print(sign_extend(0-5)); print(zero_extend(255)); print(narrow(257));
    print(compare(2)); print(compare(5)); print(widen_value(0-7));
    print(block(0-5)); discard(42);
}
'''
EXPECTED = '-5\n255\n1\n1\n0\n-7\n-5\n42\n'


def run(*args):
    p = subprocess.run(list(map(str, args)), capture_output=True, text=True, timeout=60)
    assert p.returncode == 0, (args, p.returncode, p.stdout, p.stderr)
    return p.stdout


with tempfile.TemporaryDirectory(prefix='closure-returns-') as tmp:
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
    source.write_text('fn main() { let f = |x:i64| -> Int<5> x; print(f(10)); }')
    p = subprocess.run([tvc, str(source), '-o', str(ir)], capture_output=True, text=True, timeout=60)
    assert p.returncode != 0 and 'without an explicit `as` cast' in p.stderr, p.stderr
print('closure returns PASS: sign/zero extension, truncation, bool, wide, block/void controls and family refusal; raw/O1/O3')
