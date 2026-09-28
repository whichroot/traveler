"""Compare CPU expert operations with independent scalar arithmetic."""
from pathlib import Path
import subprocess
import sys
import tempfile

tvc,llc,opt,link = sys.argv[1:5]
here = Path(__file__).resolve().parent
root = here.parent


def run(*args,ok=True):
    p = subprocess.run(list(map(str,args)),capture_output=True,timeout=180)
    if ok:
        assert p.returncode == 0,(args,p.stdout,p.stderr)
    return p


with tempfile.TemporaryDirectory(prefix='traveler-cpu-expert-') as directory:
    d = Path(directory)
    src = d/'expert.tv'
    src.write_text(f'import "{root}/src/lib/nn/cpu_expert.tv";\n'+'''
#[export] fn expert_full(rec:*u8,z:*i64,y:*i64,m:i64,d:i64,f:i64)->i32 {
    match cpu_expert_scratch(m,d,f) {
        Result::Ok(sc) => {
            var scratch = sc;
            cpu_expert_run(rec,z,y,&scratch,17179869184,107374182400,null);
            cpu_expert_scratch_free(&scratch); return 0;
        }
        Result::Err(err) => { return 1; }
    }
    return 2;
}
#[export] fn expert_situ(g:*i64,u:*i64,y:*i64,n:i64,beta:i64,lb:i64){cpu_situ(g,u,y,n,beta,lb);}
#[export] fn expert_dot(x:*i32,e:*i64,p:*u8,s:*u8,y:*i64,m:i64,n:i64,k:i64) {
    let planes:*u8=alloc(m*3*k); let scratch:*i64=alloc(k/32); let weights:*i8=alloc(k);
    cpu_mxfp4_planes(x,planes,m,k);
    cpu_mxfp4_rows(planes,e,p,s,y,m,n,k,1,n-1,scratch,weights);
    free(planes); free(scratch); free(weights);
}
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
        run(llc,'-filetype=obj',ir,'-o',d/'expert.o')
        run(link,'-O2','-no-pie',d/'expert.o',here/'cpu_expert_harness.c','-o',d/'expert')
        p = run(d/'expert',*(['spr'] if mode == 'spr' else []),ok=False)
        assert p.returncode == 0 or (mode == 'spr' and p.returncode == 77),(mode,p.stdout,p.stderr)
        if mode == 'spr':
            run(llc,ir,'-o',d/'expert.s')
            asm = (d/'expert.s').read_text()
            assert 'vpdpbusd' in asm and 'vpdpbusds' not in asm
            assert 'vpmadd52luq' in asm and 'vpmadd52huq' in asm
        if p.returncode == 77:
            print('Native SPR expert execution SKIP: host lacks required features')
print('CPU expert PASS: portable raw/O1/O3 byte parity and SPR instruction selection')
