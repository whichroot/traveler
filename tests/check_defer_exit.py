"""Reject exits from deferred bodies without recursive compiler emission."""
from pathlib import Path
import subprocess
import sys
import tempfile

tvc = sys.argv[1]
root = Path(__file__).resolve().parents[1]
cases = [
    'fn f(b: i32) -> i32 { defer { if b == 1 { return 0; } } return 5; }',
    'fn f(b: i32) -> i32 { defer { while b == 1 { return 0; } } return 5; }',
    'fn f(b: i32) -> i32 { defer { for i in 0..2 { if i == 1 { return 0; } } } return 5; }',
    f'import "{root}/src/lib/core/result.tv";\n'
    'fn g() -> Result<i32,i32> { return Result::Err(3); }\n'
    'fn f(b: i32) -> Result<i32,i32> { defer g()?; return Result::Ok(b); }',
]
with tempfile.TemporaryDirectory(prefix='defer-exit-') as tmp:
    d = Path(tmp)
    for index, source in enumerate(cases):
        tv, artifact = d / 'bad.tv', d / 'output.ll'
        tv.write_text(source + '\nfn main() { f(0); }\n')
        artifact.write_text('existing artifact\n')
        p = subprocess.run([tvc, str(tv), '-o', str(artifact)],
                           capture_output=True, text=True, timeout=15)
        assert p.returncode == 1 and 'a defer body cannot exit its function' in p.stderr, (index, p)
        assert artifact.read_text() == 'existing artifact\n', index
    print('defer exit PASS: nested if/while/for and ? refuse without recursion; artifacts preserved')
