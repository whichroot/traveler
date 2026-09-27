"""Verify dispatched streaming copies, vector lowering, and a portable loop oracle."""
from pathlib import Path
import re
import subprocess
import sys
import tempfile

tvc, llc, opt, link = sys.argv[1:5]
here = Path(__file__).resolve().parent
source = '#[export] fn copy_bytes(dst:*u8,src:*u8,n:u64) { stream_copy_bytes(dst,src,n); }\n'


def run(*args, ok=True):
    p = subprocess.run([str(a) for a in args], capture_output=True, text=True, timeout=120)
    if ok:
        assert p.returncode == 0, (args, p.stdout, p.stderr)
    return p


with tempfile.TemporaryDirectory(prefix='traveler-stream-copy-') as directory:
    d = Path(directory)
    (d / 'copy.tv').write_text(source)
    run(tvc, d / 'copy.tv', '-target', 'x86_64-linux-gnu', '-o', d / 'raw.ll')
    ir = (d / 'raw.ll').read_text()
    assert '230' in ir and '65536' in ir and '402653184' in ir and 'xgetbv' in ir and 'cpuid' in ir
    for profile in ('raw', 'O1', 'O3'):
        selected = d / 'raw.ll'
        if profile != 'raw':
            selected = d / f'{profile}.ll'
            run(opt, f'-passes=default<{profile}>', '-verify-each', '-S', d / 'raw.ll', '-o', selected)
        run(llc, '-filetype=asm', selected, '-o', d / 'copy.s')
        asm = (d / 'copy.s').read_text()
        assert re.search(r'vmovnt\w+\s+%zmm', asm), asm
        assert 'sfence' in asm and 'cpuid' in asm and 'xgetbv' in asm
        run(llc, '-filetype=obj', selected, '-o', d / 'copy.o')
        run(link, '-no-pie', '-O2', '-Wall', '-Wextra', '-Werror', d / 'copy.o', here / 'stream_copy_harness.c', '-o', d / 'copy')
        run(d / 'copy')
    # Execute the same vector loop on baseline x86 even when AVX-512 is unavailable.
    portable = re.sub(r'define internal void @__traveler_stream_copy\([^\n]+\) \{.*?\n\}',
                     'define internal void @__traveler_stream_copy(ptr %dst, ptr %src, i64 %n) {\n'
                     '  call void @__traveler_stream_copy_avx512(ptr %dst, ptr %src, i64 %n)\n  ret void\n}',
                     ir, count=1, flags=re.S)
    portable = portable.replace('"target-features"="+avx512f"', '')
    (d / 'portable.ll').write_text(portable)
    for profile in ('raw', 'O1', 'O3'):
        selected = d / 'portable.ll'
        if profile != 'raw':
            selected = d / f'portable-{profile}.ll'
            run(opt, f'-passes=default<{profile}>', '-verify-each', '-S', d / 'portable.ll', '-o', selected)
        run(llc, '-mcpu=x86-64', '-filetype=obj', selected, '-o', d / 'copy.o')
        run(link, '-no-pie', '-O2', d / 'copy.o', here / 'stream_copy_harness.c', '-o', d / 'copy')
        run(d / 'copy')
    run(tvc, d / 'copy.tv', '-target', 'aarch64-linux-gnu', '-o', d / 'arm.ll')
    arm = (d / 'arm.ll').read_text()
    assert 'avx512' not in arm and 'cpuid' not in arm and 'llvm.memcpy' in arm
    run(llc, '-mtriple=aarch64-linux-gnu', '-filetype=obj', d / 'arm.ll', '-o', d / 'arm.o')
    for body in ('let x = stream_copy_bytes(a,a,1);', 'stream_copy_bytes(a,a);',
                 'stream_copy_bytes(a,a,1 as i32);', 'stream_copy_bytes(a as *u64,a,1);'):
        (d / 'bad.tv').write_text('fn main(){ let a:*u8=alloc(8); ' + body + ' }')
        p = run(tvc, d / 'bad.tv', '-o', d / 'bad.ll', ok=False)
        assert p.returncode != 0 and 'stream_copy_bytes' in p.stderr, p.stderr
    (d / 'eval.tv').write_text('fn main(){ let a:*u8=alloc(8); stream_copy_bytes(a,a,0); }')
    p = run(tvc, '--eval', d / 'eval.tv', ok=False)
    assert p.returncode != 0 and 'native CPU' in p.stderr, p.stderr
    (d / 'shadow.tv').write_text('fn stream_copy_bytes(x:i32)->i32{return x+1;} '
                               'fn main(){print(stream_copy_bytes(2));}')
    run(tvc, d / 'shadow.tv', '-o', d / 'shadow.ll')
    assert '@__traveler_stream_copy' not in (d / 'shadow.ll').read_text()
    print('Streaming copy PASS: raw/O1/O3 dispatch, AVX-512 stores and fence, portable loop oracle, ARM fallback, refusals')
