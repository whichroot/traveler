"""Run each cleanup on Result propagation while preserving the captured result."""
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile

tvc, llc, opt, linker = sys.argv[1:5]
root = Path(__file__).resolve().parents[1]
source = f'import "{root}/src/lib/core/result.tv";\n' + '''
fn cleanup(tag: i32) { print(tag); }
fn leaf(ok: i32) -> Result<i32,i32> {
    if ok == 0 { return Result::Err(7); }
    return Result::Ok(11);
}
fn middle(ok: i32) -> Result<i32,i32> {
    let p: *i32 = alloc(1);
    p[0] = 12;
    defer { print(p[0]); free(p); }
    defer print(10);
    defer cleanup(11);
    let value: i32 = leaf(ok)?;
    return Result::Ok(value + 1);
}
fn outer(ok: i32) -> Result<i32,i32> {
    defer cleanup(20);
    let value: i32 = middle(ok)?;
    return Result::Ok(value + 1);
}
fn main() {
    let bad: Result<i32,i32> = outer(0);
    match bad { Result::Ok(v) => { print(v); } Result::Err(e) => { print(e); } }
    let good: Result<i32,i32> = outer(1);
    match good { Result::Ok(v) => { print(v); } Result::Err(e) => { print(e); } }
}
'''
expected = '11\n10\n12\n20\n7\n11\n10\n12\n20\n13\n'


def run(*args):
    p = subprocess.run(list(map(str, args)), capture_output=True, text=True, timeout=60)
    assert p.returncode == 0, (args, p.returncode, p.stdout, p.stderr)
    return p.stdout


with tempfile.TemporaryDirectory(prefix='eval-defer-unwind-') as tmp:
    d = Path(tmp)
    tv, ir = d / 'unwind.tv', d / 'unwind.ll'
    tv.write_text(source)
    assert run(tvc, tv, '--eval') == expected
    flags = shlex.split(os.environ.get('TRAVELER_LINK_FLAGS', '-no-pie' if sys.platform == 'linux' else ''))
    run(tvc, tv, '-o', ir, '--alloc-debug')
    for profile in ('raw', 'O1', 'O3'):
        selected = ir
        if profile != 'raw':
            selected = d / 'optimized.ll'
            run(opt, '-passes=default<' + profile + '>', '-S', ir, '-o', selected)
        run(llc, '-filetype=obj', selected, '-o', d / 'job.o')
        run(linker, *flags, d / 'job.o', '-o', d / 'job')
        assert run(d / 'job') == expected, profile
    print('eval defer unwind PASS: nested propagation, LIFO calls/free, Ok/Err payloads; eval/raw/O1/O3')
