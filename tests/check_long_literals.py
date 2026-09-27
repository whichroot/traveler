"""Check long literal bytes across imports, arena growth, evaluation, and LLVM."""
from pathlib import Path
import platform
import subprocess
import sys
import tempfile

tvc, llc, opt, link = sys.argv[1:5]


def run(*args, code=0):
    p = subprocess.run(list(map(str, args)), capture_output=True, timeout=120)
    assert p.returncode == code, (args, p.stdout, p.stderr)
    return p


with tempfile.TemporaryDirectory(prefix='traveler-long-literals-') as directory:
    d = Path(directory)
    cases = [('x' * n, b'x' * n) for n in (255, 256, 4095, 4096, 65537)]
    cases += [(r'a\0\n\t\"\\z' * 10000, b'a\0\n\t"\\z' * 10000)]
    expected = b''
    calls = []
    for i, (text, value) in enumerate(cases):
        imported = f'import "part{i+1}.tv";\n' if i + 1 < len(cases) else ''
        (d / f'part{i}.tv').write_text(imported + f'fn table{i}()->*u8 {{return "{text}";}}\n')
        calls.append(f'write(1,table{i}(),{len(value)+1});')
        expected += value + b'\0'
    source = d / 'main.tv'
    source.write_text('import "part0.tv";\nextern "C" fn write(fd:i32,p:*u8,n:usize)->i64;\n'
                      'fn main()->i32 {\n' + '\n'.join(calls) + '\nreturn 0;}\n')
    assert run(tvc, source, '--eval').stdout == expected
    profiles = ('none', 'o1', 'o3') if platform.machine() == 'x86_64' and platform.system() == 'Linux' else ('none', 'o1')
    for profile in profiles:
        exe = d / profile
        cpu = ('-mcpu', 'x86-64') if profile == 'o3' else ()
        run(tvc, source, '--emit', 'exe', '--opt-level', profile,
            *cpu, '-llc', llc, '-opt', opt, '-cc', link, '-o', exe)
        assert run(exe).stdout == expected, profile
    source.write_text('import "' + 'x' * 4096 + '"; fn main(){}')
    assert b'path buffer capacity' in run(tvc, source, '-o', d / 'bad.ll', code=1).stderr
    source.write_text('fn main(){let s="' + 'x' * 65537)
    assert b'unterminated string literal' in run(tvc, source, '-o', d / 'bad.ll', code=1).stderr
print('Long literals PASS: boundaries, binary bytes, recursive imports, arena growth, eval/raw/O1/O3')
