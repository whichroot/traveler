"""Compare aggregate byte offsets and strides in the evaluator and native code."""
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile

tvc, llc, opt, linker = sys.argv[1:5]
types = '''
struct Wide128 { tag: u8, value: u128, last: i64 }
struct Wide256 { tag: u8, value: u256, last: i64 }
struct Wide512 { tag: u8, value: u512, last: i64 }
struct Callback { tag: u8, value: fn()->i64, last: i64 }
struct Nested { tag: u8, value: [Wide256; 3], last: i64 }
struct Callbacks { tag: u8, value: [fn()->i64; 3], last: i64 }
'''
body = []
for i, name in enumerate(('Wide128', 'Wide256', 'Wide512', 'Callback', 'Nested', 'Callbacks')):
    body.append(f'''
    let bytes{i}: *u8 = alloc(1024);
    for j in 0..1024 {{ bytes{i}[j] = 0; }}
    let p{i}: *{name} = bytes{i} as *{name};
    p{i}[0].last = 91; p{i}[1].tag = 72;
    for j in 0..1024 {{ print(bytes{i}[j] as i32); }}
    free(bytes{i});
''')


def run(*args):
    p = subprocess.run(list(map(str, args)), capture_output=True, text=True, timeout=60)
    assert p.returncode == 0, (args, p.returncode, p.stderr)
    return p.stdout


with tempfile.TemporaryDirectory(prefix='eval-native-layout-') as tmp:
    d = Path(tmp)
    source, ir = d / 'layout.tv', d / 'layout.ll'
    source.write_text(types + '\nfn main() {\n' + '\n'.join(body) + '\n}')
    evaluated = run(tvc, '--eval', source)
    assert len(evaluated.splitlines()) == 6144
    assert evaluated.splitlines().count('91') == 6
    assert evaluated.splitlines().count('72') == 6
    flags = shlex.split(os.environ.get('TRAVELER_LINK_FLAGS', '-no-pie' if sys.platform == 'linux' else ''))
    for debug in (False, True):
        run(tvc, source, '-o', ir, *(['--alloc-debug'] if debug else []))
        for profile in ('raw', 'O1', 'O3'):
            selected = ir
            if profile != 'raw':
                selected = d / 'optimized.ll'
                run(opt, '-passes=default<' + profile + '>', '-S', ir, '-o', selected)
            run(llc, '-filetype=obj', selected, '-o', d / 'layout.o')
            run(linker, *flags, d / 'layout.o', '-o', d / 'layout')
            native = run(d / 'layout')
            assert native == evaluated, (debug, profile, 'native/evaluator byte layout differs')
print('eval native layout PASS: u128/u256/u512, function pointers, nested arrays; raw/O1/O3/redzones')
