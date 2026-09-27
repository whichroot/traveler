"""Verify merged extern declarations and checked signature conflicts."""
from pathlib import Path
import platform
import re
import subprocess
import sys
import tempfile

tvc, llc, opt, link = sys.argv[1:5]


def run(*args, ok=True):
    p = subprocess.run(list(map(str, args)), capture_output=True, timeout=120)
    if ok:
        assert p.returncode == 0, (args, p.stdout, p.stderr)
    return p


with tempfile.TemporaryDirectory(prefix='traveler-externs-') as directory:
    d = Path(directory)
    (d / 'a.tv').write_text('extern "C" fn w18_foreign(x:i64,p:*u8)->i64;\n'
                           'extern "C" fn w18_touch();\n'
                           'fn first()->i64{return w18_foreign(7,"A");}\n')
    (d / 'b.tv').write_text('extern "C" fn w18_foreign(value:i64,data:*u8)->i64;\n'
                           'extern "C" fn w18_touch();\n'
                           'fn second()->i64{return w18_foreign(9,"B");}\n')
    source = d / 'main.tv'
    source.write_text('import "a.tv"; import "b.tv";\n'
                      'extern "C" fn w18_foreign(x:i64,p:*u8)->i64;\n'
                      'extern "C" fn forward(x:i32)->i32;\n'
                      'fn forward(value:i32)->i32{return value+1;}\n'
                      'fn backward(value:i32)->i32{return value+2;}\n'
                      'extern "C" fn backward(x:i32)->i32;\n'
                      'fn main()->i32{w18_touch();print(first()+second());'
                      'print(forward(3)+backward(4));return 0;}\n')
    (d / 'foreign.c').write_text('#include <stdint.h>\n'
                               'int64_t w18_foreign(int64_t x,const uint8_t *p){return x+p[0];}\n'
                               'void w18_touch(void){}\n')
    raw = d / 'raw.ll'
    run(tvc, source, '-o', raw)
    ir = raw.read_text()
    for symbol in ('w18_foreign', 'w18_touch'):
        assert len(re.findall(r'^declare .*@' + symbol + r'\(', ir, re.M)) == 1
    for symbol in ('forward', 'backward'):
        assert not re.search(r'^declare .*@' + symbol + r'\(', ir, re.M)
        assert len(re.findall(r'^define .*@' + symbol + r'\(', ir, re.M)) == 1
    for profile in ('raw', 'O1', 'O3'):
        selected = raw
        if profile != 'raw':
            selected = d / f'{profile}.ll'
            run(opt, f'-passes=default<{profile}>', '-verify-each', '-S', raw, '-o', selected)
        run(llc, '-filetype=obj', selected, '-o', d / 'main.o')
        flags = () if platform.system() == 'Darwin' else ('-no-pie',)
        run(link, *flags, d / 'main.o', d / 'foreign.c', '-o', d / 'main')
        assert run(d / 'main').stdout == b'147\n10\n'
    conflicts = [
        'extern "C" fn clash(x:i32)->i64;',
        'extern "C" fn clash(x:i64)->i32;',
        'extern "C" fn clash(x:i32,y:i32)->i32;',
        'extern "C" fn clash(x:u32)->i32;',
        'extern "other" fn clash(x:i32)->i32;',
        'fn clash(x:i64)->i32{return 0;}',
    ]
    for declaration in conflicts:
        (d / 'a.tv').write_text('extern "C" fn clash(x:i32)->i32;\n')
        (d / 'b.tv').write_text(declaration + '\n')
        source.write_text('import "a.tv"; import "b.tv"; fn main(){}\n')
        for mode in (('-o', d / 'bad.ll'), ('--eval',)):
            p = run(tvc, source, *mode, ok=False)
            assert p.returncode != 0 and b"conflicting function declaration for 'clash'" in p.stderr, p
    source.write_text('extern "C" fn ptr(p:*u8); extern "C" fn ptr(p:*u64); fn main(){}')
    assert b'conflicting function declaration' in run(tvc, source, '-o', d / 'bad.ll', ok=False).stderr
    source.write_text('extern "C" fn f()->i32; fn f()->i32{return 1;} '
                      'fn f()->i32{return 2;} fn main(){}')
    assert b'duplicate function definition' in run(tvc, source, '-o', d / 'bad.ll', ok=False).stderr
    source.write_text('extern "C" fn f(x:i32)->i32; fn f(x:i32)->i32{return x+1;} '
                      'extern "C" fn f(value:i32)->i32; fn main(){print(f(3));}')
    assert run(tvc, source, '--eval').stdout == b'4\n'
print('Extern declarations PASS: imported duplicates, native forwards, raw/O1/O3, eval, signature conflicts')
