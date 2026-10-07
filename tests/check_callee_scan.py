"""Recursive and shared callee graphs must not multiply compiler traversal work."""
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import time

tvc, llc, opt, linker = sys.argv[1:5]


def run(*args, threads='1', timeout=30):
    start = time.monotonic()
    try:
        p = subprocess.run(list(map(str, args)), capture_output=True, text=True, timeout=timeout,
                           env=dict(os.environ, TRAVELER_THREADS=threads))
    except subprocess.TimeoutExpired:
        raise AssertionError(f'{args}: exceeded {timeout}s') from None
    assert p.returncode == 0, (args, p.returncode, p.stdout, p.stderr)
    return p.stdout, time.monotonic() - start


cases = {
    'fib': ('fn fib(n:i32)->i32 { if n<2 {return n;} return fib(n-1)+fib(n-2); }\n'
            'fn f(x:i32)->i32 { return fib(x)+1; }', [1, 2, 2, 3, 4, 6, 9, 14], True),
    'mutual': ('fn a(n:i32)->i32 { if n<2 {return n;} return b(n-1)+b(n-2); }\n'
               'fn b(n:i32)->i32 { return a(n); }\n'
               'fn f(x:i32)->i32 { return a(x); }', [0, 1, 1, 2, 3, 5, 8, 13], True),
    'effect': ('var state:i32=0;\n'
               'fn fib(n:i32)->i32 { if n<2 {return n;} return fib(n-1)+fib(n-2); }\n'
               'fn f(x:i32)->i32 { state=state+1; return fib(x)+state; }', [1, 3, 4, 6, 8, 11, 15, 21], False),
    'control': ('fn f(x:i32)->i32 { return x+1; }', list(range(1, 9)), True),
}
diamond = 'fn d0(x:i32)->i32 { return x; }\n'
for i in range(1, 25):
    diamond += f'fn d{i}(x:i32)->i32 {{ if x==0 {{ return d{i-1}(0); }} return d{i-1}(x-1); }}\n'
cases['diamond'] = (diamond + 'fn f(x:i32)->i32 { return d24(0); }', [0]*8, True)

with tempfile.TemporaryDirectory(prefix='callee-scan-') as tmp:
    d = Path(tmp)
    source, ir = d / 'case.tv', d / 'case.ll'
    flags = shlex.split(os.environ.get('TRAVELER_LINK_FLAGS', '-no-pie' if sys.platform == 'linux' else ''))
    for name, (decls, values, dispatch) in cases.items():
        source.write_text(decls + '''
fn work(out:*i32,n:i32) { for i in 0..n { out[i]=f(i); } }
fn main() { let out:*i32=alloc(8); work(out,8); var j:i32=0;
    while j<8 { print(out[j]); j=j+1; } free(out); }
''')
        _, elapsed = run(tvc, source, '-o', ir, timeout=10)
        assert ('call void @__parallel_for(' in ir.read_text()) == dispatch, name
        expected = ''.join(f'{v}\n' for v in values)
        for profile in ('raw', 'O1', 'O3'):
            selected = ir
            if profile != 'raw':
                selected = d / 'opt.ll'
                run(opt, '-passes=default<' + profile + '>', '-S', ir, '-o', selected)
            run(llc, '-filetype=obj', selected, '-o', d / 'case.o')
            run(linker, *flags, d / 'case.o', '-o', d / 'case')
            for threads in ('1', '8'):
                actual, _ = run(d / 'case', threads=threads)
                assert actual == expected, (name, profile, threads, actual)
        print(f'callee scan {name}: PASS (compile {elapsed:.3f}s; raw/O1/O3 T=1/8)')
