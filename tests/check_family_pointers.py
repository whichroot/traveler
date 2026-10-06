"""Mutable pointer aliases must preserve family bounds and scales exactly."""
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile

tvc, llc, opt, linker = sys.argv[1:5]
pairs = [
    ('*Int<1000>', '*Int<5>'), ('*Int<5>', '*Int<1000>'),
    ('*Exp<16>', '*Exp<8>'), ('*Exp<16>', '*Int<5>'),
    ('*Fix<5,4>', '*Fix<10,4>'), ('*Fix<5,4>', '*Fix<5,2>'),
    ('*i64', '*Int<5>'), ('*Int<5>', '*i64'),
    ('**Int<1000>', '**Int<5>'), ('**Int<5>', '**Int<1000>'),
    ('*[Int<5>; 2]', '*[Int<1000>; 2]'),
    ('*Box<Int<5> >', '*Box<Int<1000> >'),
]


def run(*args, success=True):
    p = subprocess.run(list(map(str, args)), capture_output=True, text=True, timeout=60)
    if success:
        assert p.returncode == 0, (args, p.returncode, p.stdout, p.stderr)
    return p


with tempfile.TemporaryDirectory(prefix='family-pointers-') as tmp:
    d = Path(tmp)
    tv, ir = d / 'pointers.tv', d / 'pointers.ll'
    sources = []
    for src, dst in pairs:
        prelude = f'struct Box<T> {{ value: *T }}\nfn take(p: {dst}) {{}}\n'
        for body in (f'take(p);', f'let q: {dst} = p;'):
            sources.append(prelude + f'fn main() {{ let p: {src} = alloc(1); {body} free(p); }}')
    sources.extend([
        'fn take(p: *Int<5>) -> i64 { return 0; }\nfn main() { let p: *Int<1000> = alloc(1); let cb: fn(*Int<5>)->i64 = &take; print(cb(p)); free(p); }',
        'fn main() { let p: *Int<1000> = alloc(1); let cb = |q: *Int<5>| { }; cb(p); free(p); }',
    ])
    for index, source in enumerate(sources):
        tv.write_text(source)
        for mode in (['-o', ir], ['--eval']):
            ir.write_text('existing artifact\n')
            p = run(tvc, tv, *mode, success=False)
            assert p.returncode == 1 and any(s in p.stderr for s in (
                'bound of the value exceeds', 'scale of the value differs',
                'without an explicit `as` cast')), (index, source, mode, p)
            assert ir.read_text() == 'existing artifact\n'
    tv.write_text('''
fn take(p: *Int<5>) -> Int<5> { return p[0]; }
fn main() {
    let p: *Int<5> = alloc(1); p[0] = 5;
    let q: *Int<5> = p;
    print(take(q));
    let value: Int<1000> = q[0]; print(value);
    let raw: *i64 = p as *i64; print(raw[0]);
    free(p);
}
''')
    expected = '5\n5\n5\n'
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
    print('family pointers PASS: 26 alias/call refusals, nested pointers/arrays/wrappers, scalar widening and explicit casts; eval/raw/O1/O3')
