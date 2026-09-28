"""Check portable SIMD semantics and explicit Sapphire Rapids instruction selection."""
from pathlib import Path
import subprocess
import sys
import tempfile

tvc, llc, opt, link = sys.argv[1:5]
here = Path(__file__).resolve().parent


def run(*args, ok=True):
    p = subprocess.run(list(map(str, args)), capture_output=True, timeout=120)
    if ok:
        assert p.returncode == 0, (args, p.stdout, p.stderr)
    return p


with tempfile.TemporaryDirectory(prefix='traveler-cpu-simd-') as directory:
    d = Path(directory)
    src = d/'simd.tv'
    src.write_text('''
#[export] fn dot(d:*i32,a:*i32,b:*u8,c:*i8){cpu_dpbusd_512(d,a,b,c);}
#[export] fn low(d:*u64,a:*u64,b:*u64,c:*u64){cpu_madd52lo_512(d,a,b,c);}
#[export] fn high(d:*u64,a:*u64,b:*u64,c:*u64){cpu_madd52hi_512(d,a,b,c);}
#[export] fn shuffle(d:*u8,a:*u8,b:*u8){cpu_shuffle8_512(d,a,b);}
#[export] fn divide(d:*u64,a:*u64,b:*u64){cpu_div_u53_512(d,a,b);}
#[export] fn ratio(d:*u64,a:*u64,b:*u64){cpu_ratio_q32_512(d,a,b);}
''')
    run(tvc,src,'-o',d/'raw.ll')
    for mode in ('raw','O1','O3','spr'):
        ir = d/'raw.ll'
        if mode == 'spr':
            ir = d/'spr.ll'
            run(tvc,src,'--emit','ir','--opt-level','o3','-mcpu','sapphirerapids','-opt',opt,'-o',ir)
        elif mode != 'raw':
            ir = d/f'{mode}.ll'
            run(opt,f'-passes=default<{mode}>','-verify-each','-S',d/'raw.ll','-o',ir)
        run(opt,'-passes=verify','-disable-output',ir)
        run(llc,'-filetype=asm',ir,'-o',d/'simd.s')
        asm = (d/'simd.s').read_text()
        if mode == 'spr':
            for opcode in ('vpdpbusd','vpmadd52luq','vpmadd52huq','vpshufb'):
                assert opcode in asm, opcode
            assert 'vpdpbusds' not in asm
        else:
            assert 'vpdpbusd' not in asm and 'vpmadd52' not in asm
        run(llc,'-filetype=obj',ir,'-o',d/'simd.o')
        run(link,'-O2','-no-pie',d/'simd.o',here/'cpu_simd_harness.c','-o',d/'simd')
        p = run(d/'simd',*(['spr'] if mode == 'spr' else []),ok=False)
        assert p.returncode == 0 or (mode == 'spr' and p.returncode == 77), (mode,p.stdout,p.stderr)
        if p.returncode == 77:
            print('Native AVX-512/VNNI/IFMA execution SKIP: host lacks required features')
    for body in ('cpu_dpbusd_512(p,p,p,p);', 'cpu_shuffle8_512(p,p);',
                 'let x=cpu_shuffle8_512(p,p,p);'):
        src.write_text('fn main(){let p:*u8=alloc(64);'+body+'}')
        p = run(tvc,src,'-o',d/'bad.ll',ok=False)
        assert p.returncode != 0 and b'CPU SIMD' in p.stderr, p
    src.write_text('fn main(){let p:*u8=alloc(64);cpu_shuffle8_512(p,p,p);}')
    assert run(tvc,src,'--eval',ok=False).returncode != 0
    run(tvc,src,'-target','aarch64-linux-gnu','-o',d/'arm.ll')
    run(llc,'-mtriple=aarch64-linux-gnu','-filetype=obj',d/'arm.ll','-o',d/'arm.o')
    src.write_text('fn cpu_dpbusd_512(x:i32)->i32{return x+1;} fn main(){print(cpu_dpbusd_512(2));}')
    run(tvc,src,'-o',d/'shadow.ll')
    assert '@__traveler_cpu_simd_' not in (d/'shadow.ll').read_text()
print('CPU SIMD PASS: raw/O1/O3 oracles, SPR instructions, aliases, guards, and refusals')
