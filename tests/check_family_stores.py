"""All aggregate stores must enforce bound and scale coercions."""
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile

tvc, llc, opt, linker = sys.argv[1:5]
sites = {
    'struct': 'let s: Box = Box { value: x };',
    'member': 'var s: Box = Box { value: 0 }; s.value = x;',
    'array': 'var a: [DEST; 1] = 0; a[0] = x;',
    'pointer': 'let p: *DEST = alloc(1); p[0] = x; free(p);',
    'enum': 'let e: Event = Event::Value(x);',
}


def run(*args, success=True):
    p = subprocess.run(list(map(str, args)), capture_output=True, text=True, timeout=60)
    if success:
        assert p.returncode == 0, (args, p.returncode, p.stdout, p.stderr)
    return p


with tempfile.TemporaryDirectory(prefix='family-stores-') as tmp:
    d = Path(tmp)
    tv, ir = d / 'stores.tv', d / 'stores.ll'
    for src, dst, message in (
        ('Int<1000>', 'Int<5>', 'Int bound of the value exceeds'),
        ('Exp<16>', 'Exp<8>', 'Exp scale of the value differs'),
        ('Fix<1000,4>', 'Fix<5,4>', 'Fix bound of the value exceeds'),
        ('Fix<5,4>', 'Fix<5,2>', 'Fix scale of the value differs'),
    ):
        for site, statement in sites.items():
            tv.write_text(f'struct Box {{ value: {dst} }}\nenum Event {{ Value({dst}) }}\n'
                          f'fn main() {{ let x: {src} = 1; {statement.replace("DEST", dst)} }}')
            for mode in (['-o', ir], ['--eval']):
                ir.write_text('existing artifact\n')
                p = run(tvc, tv, *mode, success=False)
                assert p.returncode == 1 and message in p.stderr, (src, dst, site, mode, p)
                assert ir.read_text() == 'existing artifact\n'
    tv.write_text('''
struct Box { value: Int<10> }
enum Event { Value(Int<10>) }
fn main() {
    let x: Int<5> = 5;
    var s: Box = Box { value: x };
    print(s.value); s.value = x;
    var a: [Int<10>; 1] = 0; a[0] = x; print(a[0]);
    let p: *Int<10> = alloc(1); p[0] = x; print(p[0]); free(p);
    let e: Event = Event::Value(x);
    match e { Event::Value(v) => { print(v); } }
}
''')
    expected = '5\n5\n5\n5\n'
    assert run(tvc, tv, '--eval').stdout == expected
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
    print('family stores PASS: 20 invalid bound/scale stores, native/eval refusals, valid widening eval/raw/O1/O3')
