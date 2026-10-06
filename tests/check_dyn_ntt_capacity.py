"""Validate every runtime NTT root slot, including valuations above 32."""
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile

tvc, llc, opt, linker = sys.argv[1:5]
source = '''
fn check<F: Field>(stages: i32) -> i32 {
    var bad: i32 = 0;
    var s: i32 = 0;
    while s < stages {
        let root: F = __ntt_root(s);
        let inverse: F = __ntt_inv_root(s);
        var scale: F = __ntt_n_inv(s);
        if root * inverse != 1 { bad = bad + 1; }
        var power: F = root;
        var j: i32 = 0;
        while j < s { power = power * power; j = j + 1; }
        if power == 1 { bad = bad + 1; }
        if power * power != 1 { bad = bad + 1; }
        j = 0;
        while j <= s { scale = scale + scale; j = j + 1; }
        if scale != 1 { bad = bad + 1; }
        s = s + 1;
    }
    return bad;
}
instantiate check<dyn>;
fn main() {
    print(check_dyn(field(65537), 16));
    print(check_dyn(field(18446744069414584321), 32));
    print(check_dyn(field(77309411329), 33));
    print(check_dyn(field(4179340454199820289), 57));
}
'''


def run(*args):
    p = subprocess.run(list(map(str, args)), capture_output=True, text=True, timeout=90)
    assert p.returncode == 0, (args, p.returncode, p.stdout, p.stderr)
    return p.stdout


with tempfile.TemporaryDirectory(prefix='dyn-ntt-capacity-') as tmp:
    d = Path(tmp)
    tv, ir = d / 'roots.tv', d / 'roots.ll'
    tv.write_text(source)
    flags = shlex.split(os.environ.get('TRAVELER_LINK_FLAGS', '-no-pie' if sys.platform == 'linux' else ''))
    roundtrip = Path(__file__).resolve().parents[1] / 'examples/dyn_ntt_test.tv'
    for fixture, expected in ((tv, '0\n' * 4), (roundtrip, '1\n' * 5)):
        assert run(tvc, fixture, '--eval') == expected
        run(tvc, fixture, '-o', ir)
        for profile in ('raw', 'O1', 'O3'):
            selected = ir
            if profile != 'raw':
                selected = d / 'optimized.ll'
                run(opt, '-passes=default<' + profile + '>', '-S', ir, '-o', selected)
            run(llc, '-filetype=obj', selected, '-o', d / 'job.o')
            run(linker, *flags, d / 'job.o', '-o', d / 'job')
            assert run(d / 'job') == expected, (fixture, profile)
    print('dyn NTT capacity PASS: all slots at k=16/32/33/57, round trips and polynomial products; eval/raw/O1/O3')
