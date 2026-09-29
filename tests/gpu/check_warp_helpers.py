"""Check helper-expanded collectives, participation proofs, and plan budgets."""
import ctypes
import json
import math
from pathlib import Path
import random
import re
import subprocess
import sys
import tempfile

TVC, LLC, OPT, LINK = sys.argv[1:5]
HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
HEADER = f'import "{ROOT}/src/lib/gpu/warp.tv";\nimport "{ROOT}/src/lib/gpu/shared.tv";\n'
SIG = '(t:GpuThread,input:*u64,output:*u64,limit:u64)'
SETUP = 'let index=gpu_global_index(t); let local=gpu_local_index(t); var v=input[index] as u32;'
STEP = '''
v = v + gpu_warp_shuffle_xor_u32(4294967295,v,1);
let votes = gpu_warp_ballot(4294967295,(v & 1)==0);
let any = gpu_warp_any(4294967295,v!=0);
let all = gpu_warp_all(4294967295,v<4294967295);
gpu_warp_sync(4294967295);
let first = gpu_warp_shuffle_u32(4294967295,v,0);
v = (v ^ votes ^ first) + (any as u32) + (all as u32);
'''
HELPERS = '''
fn shuffle<T>(value:T, delta:u32) -> u32 {
    return gpu_warp_shuffle_xor_u32(4294967295,value as u32,delta);
}
fn fence(mask:u32) { gpu_warp_sync(mask); return; }
fn first(mask:u32,value:u32,lane:u32) -> u32 {
    return gpu_warp_shuffle_u32(mask as u64,value,lane as u64);
}
fn step(value:u32) -> u32 {
    var v=value;
''' + STEP.replace('gpu_warp_shuffle_xor_u32(4294967295,v,1)', 'shuffle(v,1)') \
          .replace('gpu_warp_sync(4294967295)', 'fence(4294967295)') \
          .replace('gpu_warp_shuffle_u32(4294967295,v,0)', 'first(4294967295,v,0)') + '''
    return v;
}
fn repeat(value:u32, rounds:u64) -> u32 {
    if rounds==0 { return value; }
    var v=value; var n:u64=0;
    while n<rounds { v=step(v); n=n+1; }
    return v;
}
'''


def run(*args, code=0):
    p = subprocess.run(list(map(str, args)), capture_output=True, text=True, timeout=90)
    assert p.returncode == code, (args, p.returncode, p.stdout, p.stderr)
    return p


with tempfile.TemporaryDirectory() as directory:
    d = Path(directory); src = d/'helpers.tv'; ir = d/'helpers.ll'
    varying = 'if (local & 1)==0 { v=v+1; } else { v=v+2; }'
    src.write_text(HEADER + HELPERS + f'#[kernel] fn helpers{SIG} {{ {SETUP} {varying} '
                   'v=repeat(v,limit); output[index]=v as u64; }\n'
                   + f'#[kernel] fn inline_version{SIG} {{ {SETUP} {varying} var n:u64=0; '
                   + f'while n<limit {{ {STEP} n=n+1; }} output[index]=v as u64; }}')
    run(TVC, '--emit-gpu-nvptx', src, '-o', ir)
    text = ir.read_text()
    entries = [json.loads(line.split(' ', 2)[2]) for line in text.splitlines() if line.startswith('; traveler.kernel.v1 ')]
    assert len(entries) == 2 and all('warp-full32-v1' in k['requires'] for k in entries)
    assert all(k['profile'] == 'cooperative-warp-v1' for k in entries)
    assert 'alloca' not in text and not re.search(r'call .*@(step|repeat|fence)\(', text)
    modes = ['raw', 'o2'] if OPT else ['raw']
    triple = re.search(r'Default target: (\S+)', run(LLC, '--version').stdout)[1]
    for mode in modes:
        selected = ir
        if mode == 'o2':
            selected = d/'optimized.ll'
            run(OPT, '-passes=default<O2>', '-verify-each', '-S', ir, '-o', selected)
        run(LLC, '-mcpu=sm_90', selected, '-o', d/f'{mode}.ptx')
        assert 'shfl.sync.bfly.b32' in (d/f'{mode}.ptx').read_text()
        native = selected.read_text().replace('define ptx_kernel', 'define').replace('ptr addrspace(1)', 'ptr')
        native = re.sub(r'addrspacecast ptr (%\w+) to ptr', r'getelementptr i8, ptr \1, i64 0', native)
        for kind in ('tid', 'ctaid', 'ntid', 'nctaid'):
            for axis in 'xyz':
                native = native.replace(f'llvm.nvvm.read.ptx.sreg.{kind}.{axis}', f'test_{kind}_{axis}')
        for old, new in [('bar.warp.sync', 'warp_sync'), ('shfl.sync.idx.i32', 'shuffle'),
                         ('shfl.sync.bfly.i32', 'shuffle_xor'), ('vote.ballot.sync', 'ballot'),
                         ('vote.any.sync', 'any'), ('vote.all.sync', 'all')]:
            native = native.replace('llvm.nvvm.'+old, 'test_'+new)
        host = d/f'{mode}-host.ll'; host.write_text(native)
        run(LLC, f'-mtriple={triple}', '-relocation-model=pic', '-filetype=obj', host, '-o', d/f'{mode}.o')
        run(LINK, '-shared', '-fPIC', '-pthread', '-Wall', '-Wextra', '-Werror',
            d/f'{mode}.o', HERE/'shared_sim.c', '-o', d/f'{mode}.so')
        lib = ctypes.CDLL(str(d/f'{mode}.so')); simulate = lib.simulate
        simulate.argtypes = [ctypes.c_void_p]*3 + [ctypes.c_uint64, ctypes.c_void_p]
        simulate.restype = None
        rng = random.Random(193)
        for shape in ((1, 1, 1, 32, 1, 1), (1, 1, 1, 8, 4, 2), (2, 1, 1, 64, 1, 1)):
            count = math.prod(shape); values = [rng.getrandbits(32) for _ in range(count)]
            inp = (ctypes.c_uint64*count)(*values)
            for rounds in (0, 1, 3):
                expected = [(v+1+i%2) & 0xffffffff for i, v in enumerate(values)]
                for _ in range(rounds):
                    for start in range(0, count, 32):
                        old = expected[start:start+32]
                        v = [(x+old[i^1]) & 0xffffffff for i, x in enumerate(old)]
                        votes = sum(int(x%2 == 0) << i for i, x in enumerate(v))
                        add = int(any(v)) + int(all(x < 0xffffffff for x in v))
                        expected[start:start+32] = [((x ^ votes ^ v[0])+add) & 0xffffffff for x in v]
                for symbol in ('__traveler_kernel_0', '__traveler_kernel_1'):
                    out = (ctypes.c_uint64*count)()
                    simulate(getattr(lib, symbol), inp, out, rounds, (ctypes.c_uint32*6)(*shape))
                    assert list(out) == expected, (mode, shape, rounds, symbol)

    failures = [
        ('', 'if (local & 1)==0 { v=step(v); }', 'conditional-effect'),
        ('fn bad(v:u32,p:bool)->u32 { if p { return step(v); } return v; }', 'v=bad(v,(local & 1)==0);', 'conditional-effect'),
        ('fn bad(v:u32,p:bool)->u32 { if p { return v; } return step(v); }', 'v=bad(v,(local & 1)==0);', 'conditional-effect'),
        ('fn bad(v:u32,n:u64)->u32 { var i:u64=0; var r=v; while i<n { r=step(r); i=i+1; } return r; }', 'v=bad(v,local);', 'conditional-effect'),
        ('fn bad(v:u32,n:u64)->u32 { var i:u64=0; var r=v; while i<2 { r=step(r); i=i+n+1; } return r; }', 'v=bad(v,local);', 'conditional-effect'),
        ('', 'fence(limit as u32);', 'expression-shape'),
        ('', 'v=first(4294967295,v,limit as u32);', 'expression-shape'),
        ('', 'v=first(7,v,0);', 'expression-shape'),
        ('', 'v=first(4294967295,v,32);', 'expression-shape'),
        ('fn bad() { gpu_block_barrier(); }', 'bad();', 'expression-shape'),
        ('fn bad() { bad(); }', 'bad();', 'callee'),
        ('fn bad(p:bool) { if p { return; } fence(4294967295); }', 'bad((local & 1)==0);', 'loop-or-return-control'),
    ]
    for helpers, body, reason in failures:
        src.write_text(HEADER + HELPERS + helpers + f'\n#[kernel] fn bad_entry{SIG} {{ {SETUP} {body} output[index]=v as u64; }}')
        ir.write_text('previous artifact')
        p = run(TVC, '--emit-gpu-nvptx', src, '-o', ir, code=1)
        assert reason in p.stderr and ir.read_text() == 'previous artifact', p.stderr
        if reason == 'conditional-effect':
            assert 'expanded helper call at line' in p.stderr and 'non-uniform control' in p.stderr, p.stderr

    small_sig = '(t:GpuThread,input:*u64,output:*u64)'
    for n, code in ((252, 0), (253, 1)):
        body = 'let index=gpu_global_index(t);' + ''.join(f'let v{i}:u64={i};' for i in range(n))
        src.write_text(HEADER + f'#[kernel] fn capacity{small_sig} {{ {body} output[index]=input[index]+v0; }}')
        p = run(TVC, '--emit-gpu-nvptx', src, '-o', ir, code=code)
        if code: assert 'live bindings (256)' in p.stderr, p.stderr
    body = 'let index=gpu_global_index(t);' + ''.join(f'var v{i}:u64={i};' for i in range(200))
    for loops, code in ((10, 0), (39, 0), (40, 1)):
        src.write_text(HEADER + f'#[kernel] fn capacity{small_sig} {{ {body} ' + 'while false {}'*loops + 'gpu_warp_sync(4294967295); output[index]=input[index]+v0; }')
        if code: ir.write_text('previous artifact')
        p = run(TVC, '--emit-gpu-nvptx', src, '-o', ir, code=code)
        if code: assert 'device nodes (16384)' in p.stderr and ir.read_text() == 'previous artifact', p.stderr
        else:
            run(OPT, '-passes=verify', '-disable-output', ir)
            run(LLC, '-mcpu=sm_90', ir, '-o', d / 'capacity.ptx')
    for count, code in ((1400, 0), (5500, 1)):
        body = 'let index=gpu_global_index(t); var v:u64=0;' + 'v=v+1;'*count
        src.write_text(HEADER + f'#[kernel] fn capacity{small_sig} {{ {body} output[index]=input[index]+v; }}')
        p = run(TVC, '--emit-gpu-nvptx', src, '-o', ir, code=code)
        if code: assert 'traversal work (16384)' in p.stderr, p.stderr
        else: run(OPT, '-passes=verify', '-disable-output', ir)
    for count, code in ((256, 0), (1023, 0), (1024, 1)):
        body = 'let index=gpu_global_index(t); var v:u64=0;' + 'if true { let a:[u64;16]=0; v=v+a[0]; }\n'*count
        src.write_text(HEADER + f'#[kernel] fn capacity{small_sig} {{ {body} output[index]=input[index]+v; }}')
        p = run(TVC, '--emit-gpu-nvptx', src, '-o', ir, code=code)
        if code: assert 'aggregate components (16384)' in p.stderr, p.stderr
        else: run(OPT, '-passes=verify', '-disable-output', ir)
    print(f'warp helpers PASS: {"/".join(modes)}, six collectives, nested/generic/void helpers, uniformity refusals, and capacity boundaries')
