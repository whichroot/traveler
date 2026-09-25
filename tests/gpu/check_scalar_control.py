"""Check scalar device control flow against host execution and exact oracles."""
import ctypes
import json
import os
from pathlib import Path
import random
import re
import signal
import subprocess
import sys
import tempfile

TVC, LLC, OPT, LINK = sys.argv[1:5]
HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
TRIPLE = re.search(r'Default target: (\S+)', subprocess.check_output([LLC, '--version'], text=True))[1]
MASK = (1 << 64) - 1
ONE = 1 << 32


def run(*args, code=0):
    p = subprocess.run(list(map(str, args)), capture_output=True, text=True,
                       env={**os.environ, 'TRAVELER_THREADS': '1'}, timeout=90)
    assert p.returncode == code, (args, p.returncode, p.stdout, p.stderr)
    return p


def signed(x):
    x &= MASK
    return x - (1 << 64) if x >> 63 else x


def rounded(n, d):
    q, r = divmod(n, d)
    return q + int(2*r > d or (2*r == d and q & 1))


def rne(a, bits):
    return signed(0 if bits >= 128 else rounded(a, 1 << bits))


def mul(a, b):
    return rne(a*b, 32)


def ratio(a, b):
    return rounded(a*ONE, b)


def exp_neg(x):
    if x >= 0:
        return ONE
    if x <= -32*ONE:
        return 0
    ln2 = 195103586505167
    k, magnitude = divmod(-x*65536, ln2)
    term = total = 1 << 48
    for j in range(1, 15):
        product = rne(-term*magnitude, 48)
        term = (abs(product)//j) * (-1 if product < 0 else 1)
        total += term
    return rne(total, 16+k)


def sigmoid(x):
    e = exp_neg(-abs(x))
    return ratio(e if x < 0 else ONE, ONE+e)


def tanh(x):
    e = exp_neg(-2*abs(x))
    y = ratio(ONE-e, ONE+e)
    return -y if x < 0 else y


def situ(x, y):
    b = ratio(abs(y), 2*ONE) * (-1 if y < 0 else 1)
    return mul(mul(mul(ONE, tanh(x)), sigmoid(x)), mul(2*ONE, tanh(b)))


def retarget(text):
    text = re.sub(r'target triple = "[^"]+"', f'target triple = "{TRIPLE}"', text)
    text = text.replace('define ptx_kernel', 'define').replace('ptr addrspace(1)', 'ptr')
    text = re.sub(r'addrspacecast ptr (%\w+) to ptr', r'getelementptr i8, ptr \1, i64 0', text)
    for kind in ('tid', 'ctaid', 'ntid', 'nctaid'):
        for axis in 'xyz':
            name = f'llvm.nvvm.read.ptx.sreg.{kind}.{axis}'
            value = 1 if kind in ('ntid', 'nctaid') else 0
            text = text.replace(f'declare i32 @{name}()', f'@test_{kind}_{axis} = global i32 {value}')
            text = text.replace(f'call i32 @{name}()', f'load i32, ptr @test_{kind}_{axis}')
    return text


def library(text, path, optimized):
    path.write_text(text)
    run(OPT, '-passes=default<O1>' if optimized else '-passes=verify', '-verify-each', '-S', path, '-o', path)
    run(LLC, f'-mtriple={TRIPLE}', '-relocation-model=pic', '-filetype=obj', path, '-o', path.with_suffix('.o'))
    run(LINK, '-shared', path.with_suffix('.o'), '-o', path.with_suffix('.so'))
    return ctypes.CDLL(str(path.with_suffix('.so')))


rng = random.Random(3218)
wide = [(x, y) for x in (0, 1, -1, -(1 << 63), (1 << 63)-1, 3, -3)
        for y in (0, 1, 2, 32, 63, 64, 127, 128, 129)]
wide += [(signed(rng.getrandbits(64)), rng.randrange(130)) for _ in range(193)]
qvalues = [(x, y) for x in (-40*ONE, -32*ONE, -16*ONE, -ONE, -1, 0, 1, ONE, 16*ONE)
           for y in (-16*ONE, -1, 0, 1, 16*ONE)]
qvalues += [(rng.randrange(-16*ONE, 16*ONE), rng.randrange(-16*ONE, 16*ONE)) for _ in range(211)]


def loops(x, y):
    acc, other = x, y
    for i in range(y & 7):
        for j in range(3):
            if (i+j) & 1:
                acc = signed(acc+other)
                other = signed(other-1)
            else:
                acc = signed(acc ^ other)
    return signed(acc+other)


CASES = [
    ('rne', '', 'q_rne(x as i128, y)', wide, rne),
    ('mul', '', 'q_mul(x, y)', qvalues + [(signed(rng.getrandbits(64)), signed(rng.getrandbits(64))) for _ in range(256)], mul),
    ('ratio', '', 'q_ratio(x & 1099511627775, 4294967296 + (y & 65535))', wide,
     lambda x, y: ratio(x & ((1 << 40)-1), ONE+(y & 65535))),
    ('exp', '', 'q_exp_neg(x)', qvalues, lambda x, y: exp_neg(x)),
    ('sigmoid', '', 'q_sigmoid(x)', qvalues, lambda x, y: sigmoid(x)),
    ('tanh', '', 'q_tanh(x)', qvalues, lambda x, y: tanh(x)),
    ('situ', 'var aa:i64=x; if aa<0 { aa=0-aa; } var bb:i64=y; if bb<0 { bb=0-bb; } '
     'aa=q_ratio(aa,4294967296); bb=q_ratio(bb,8589934592); '
     'if x<0 { aa=0-aa; } if y<0 { bb=0-bb; }',
     'q_mul(q_mul(q_mul(4294967296,q_tanh(aa)),q_sigmoid(x)),q_mul(8589934592,q_tanh(bb)))', qvalues, situ),
    ('loops', 'var acc:i64=x; var other:i64=y; var i:i64=0; '
     'while i<(y&7) { var j:i64=0; while j<3 { '
     'if ((i+j)&1)!=0 { acc=acc+other; other=other-1; } else { acc=acc^other; } j=j+1; } i=i+1; }',
     'acc+other', wide, loops),
    ('lazy', '', '((x==0 || 100/x==0) && (x!=0 || y>=0)) as i64', wide,
     lambda x, y: int((x == 0 or abs(100)//abs(x) == 0) and (x != 0 or y >= 0))),
    ('early', '', 'early(x,y)', wide,
     lambda x, y: signed((y+7) if x < 0 and y > 0 else (y-3) if x < 0 else x+y)),
    ('continuation', '', 'scoped(x,y)', wide,
     lambda x, y: signed(y+1 if x < 0 and y <= 0 else x+y)),
    ('shadow', 'var z:i64=x; if y>0 { var z:i64=y; z=z+9; } else { z=z+3; } '
     'var j:i64=0; while j<(y&3) { let z:i64=j; j=z+1; }', 'z+j', wide,
     lambda x, y: signed(x+(0 if y > 0 else 3)+(y & 3))),
    ('bits', 'var v:u64=x as u64; var n:i64=0; while v>0 && n<64 { n=n+1; v=v>>1; }',
     'n', wide, lambda x, y: (x & MASK).bit_length()),
    ('narrow', 'var v:i8=x as i8; var flag:bool=x<0; var j:i64=0; '
     'while j<(y&7) { v=v+1; flag=!flag; j=j+1; }', '(v as i64)+(flag as i64)', wide,
     lambda x, y: ((x+(y & 7)+128) & 255)-128+int((x < 0) ^ bool(y & 1))),
    ('zero', 'while false { output[gpu_global_index(t)]=99; }', 'x+y', wide,
     lambda x, y: signed(x+y)),
    ('stores', 'if y>0 { output[gpu_global_index(t)]=x; } else { output[gpu_global_index(t)]=y; } '
     'var j:i64=0; while j<(y&7) { output[gpu_global_index(t)]=output[gpu_global_index(t)]+j; j=j+1; }',
     'output[gpu_global_index(t)]', wide, lambda x, y: signed((x if y > 0 else y)+sum(range(y & 7)))),
    ('bounded', 'var z:u64=0; var j:u64=0; while j<(y as u64 & 7) { '
     'if j%2==0 { z=z+gpu_bounded_load_u64(a,count,gpu_global_index(t)+j); } j=j+1; }',
     'z as i64', [(i, i % 8) for i in range(129)],
     lambda x, y: sum(x+j for j in range(y & 7) if j % 2 == 0 and x+j < 129)),
    ('trap', '', '(x!=0 || 7/y==0) as i64', [(1, 0), (0, 8), (0, -8)], lambda x, y: 1),
]

with tempfile.TemporaryDirectory(prefix='traveler-device-control-') as directory:
    temp = Path(directory)
    count = 0
    for name, setup, expression, values, oracle in CASES:
        print(f'  scalar control: {name}', flush=True)
        source = temp/f'{name}.tv'
        input_type = 'u64' if name == 'bounded' else 'i64'
        load = 'gpu_bounded_load_u64(a,count,gpu_global_index(t))' if name == 'bounded' else 'a[gpu_global_index(t)]'
        source.write_text(f'import "{ROOT}/src/lib/gpu/thread.tv";\nimport "{HERE}/device_q32.tv";\n'
                          'fn gpu_bounded_load_u64(buffer:*u64,count:u64,index:u64)->u64 { '
                          'if index<count { return buffer[index]; } return 0; }\n'
                          'fn early(x:i64,y:i64)->i64 { var z:i64=x; if x<0 { '
                          'let x:i64=y; if x>0 { return x+7; } z=y-3; } else { z=x+y; } return z; }\n'
                          'fn scoped(x:i64,y:i64)->i64 { if x<0 { var x:i64=y; '
                          'if y>0 { } else { x=x+1; return x; } } let z:i64=x+y; return z; }\n'
                          f'#[kernel] fn flow(t:GpuThread,a:*{input_type},b:*i64,output:*i64,count:u64) {{ '
                          f'let x:i64={load} as i64; let y:i64=b[gpu_global_index(t)]; '
                          f'{setup} output[gpu_global_index(t)]={expression}; }}\n'
                          f'#[export] fn cpu(i:u64,a:*{input_type},b:*i64,output:*i64,count:u64) {{ '
                          'let t:GpuThread=GpuThread{thread_x:i,thread_y:0,thread_z:0,block_x:0,block_y:0,block_z:0,'
                          'block_dim_x:count,block_dim_y:1,block_dim_z:1,grid_dim_x:1,grid_dim_y:1,grid_dim_z:1}; '
                          'flow(t,a,b,output,count); }\n')
        device_ir = temp/f'{name}.ll'
        result = run(TVC, '--emit-gpu-nvptx', source, '-o', device_ir)
        text = device_ir.read_text()
        assert 'alloca' not in text
        if name != 'zero':
            assert 'phi i' in text
        if name in ('ratio', 'exp', 'loops', 'bits', 'zero'):
            labels = {m[1]: m.start() for m in re.finditer(r'^(ds\d+):', text, re.M)}
            assert any(labels[m[1]] < m.start() for m in re.finditer(r'br label %(ds\d+)', text)), name
        for sm in (90, 120):
            run(LLC, f'-mcpu=sm_{sm}', device_ir, '-o', temp/f'{name}-{sm}.ptx')
        host_ir = temp/f'{name}-host.ll'
        run(TVC, source, '-o', host_ir)
        a = (ctypes.c_int64*len(values))(*(x for x, _ in values))
        b = (ctypes.c_int64*len(values))(*(y for _, y in values))
        expected = [signed(oracle(x, y)) for x, y in values]
        for optimized in (False, True):
            device = library(retarget(text), temp/f'{name}-device-{optimized}.ll', optimized)
            host = library(host_ir.read_text(), temp/f'{name}-host-{optimized}.ll', optimized)
            worker = device.__traveler_kernel_0
            worker.argtypes = [ctypes.c_void_p]*3+[ctypes.c_uint64]*3
            host.cpu.argtypes = [ctypes.c_uint64]+[ctypes.c_void_p]*3+[ctypes.c_uint64]
            worker.restype = host.cpu.restype = None
            output = (ctypes.c_int64*len(values))()
            reference = (ctypes.c_int64*len(values))()
            tid = ctypes.c_uint32.in_dll(device, 'test_tid_x')
            ctypes.c_uint32.in_dll(device, 'test_ntid_x').value = len(values)
            signal.alarm(30)
            for i in range(len(values)):
                tid.value = i
                worker(a, b, output, len(values), 0, len(values))
                host.cpu(i, a, b, reference, len(values))
            signal.alarm(0)
            assert list(output) == expected, (name, optimized, list(output), expected)
            assert list(reference) == expected, (name, optimized, 'host mismatch')
            count += len(values)
            if name == 'trap':
                for kind in ('host', 'device'):
                    path = temp/f'{name}-{kind}-{optimized}.so'
                    call = 'f(0,a,a,b,1)' if kind == 'host' else 'f(a,a,b,1,0,1)'
                    symbol = 'cpu' if kind == 'host' else '__traveler_kernel_0'
                    args = '[ctypes.c_uint64]+[ctypes.c_void_p]*3+[ctypes.c_uint64]' if kind == 'host' else '[ctypes.c_void_p]*3+[ctypes.c_uint64]*3'
                    child = subprocess.run([sys.executable, '-c',
                        f'import ctypes,resource; resource.setrlimit(resource.RLIMIT_CORE,(0,0)); '
                        f'lib=ctypes.CDLL({str(path)!r}); f=getattr(lib,{symbol!r}); f.argtypes={args}; '
                        f'a=(ctypes.c_int64*1)(0); b=(ctypes.c_int64*1)(0); {call}'],
                        capture_output=True, text=True, timeout=10)
                    assert child.returncode != 0, (kind, optimized, 'executed division did not fail')

    refusals = [
        ('for-loop', 'for j in 0..4 { x=x+1; }'),
        ('loop-or-return-control', 'while x>0 { if x==2 { break; } x=x-1; }'),
        ('loop-or-return-control', 'while x>0 { x=x-1; continue; }'),
        ('loop-or-return-control', 'while x>0 { return; }'),
        ('aggregate-merge', 'var s:Pair=Pair{value:x}; if x>0 { s.value=1; } x=s.value;'),
        ('aggregate-merge', 'var s:Pair=Pair{value:x}; while x>0 { s.value=x; x=x-1; }'),
        ('non-canonical-store', 'output[gpu_global_index(t)>>5]=x;'),
        ('conditional-effect', 'if x>0 { gpu_atomic_exchange_u64(output,count,0,x,0,1); }'),
        ('conditional-effect', 'while x>0 { gpu_atomic_exchange_u64(output,count,0,x,0,1); x=x-1; }'),
        ('conditional-effect', 'if x>0 { let w:u32=gpu_warp_shuffle_xor_u32(4294967295,x as u32,1); }'),
        ('conditional-effect', 'while x>0 { let w:u32=gpu_warp_shuffle_xor_u32(4294967295,x as u32,1); x=x-1; }'),
        ('conditional-effect', 'if x>0 { gpu_block_barrier(); }'),
    ]
    for index, (reason, body) in enumerate(refusals):
        source = temp/'refused.tv'
        source.write_text(''.join(f'import "{ROOT}/src/lib/gpu/{part}.tv";\n'
                                  for part in ('thread', 'shared', 'warp', 'atomic')) +
                          'struct Pair { value:u64, }\n'
                          '#[kernel] fn bad(t:GpuThread,a:*u64,output:*u64,count:u64) {\n'
                          'var x:u64=a[gpu_global_index(t)];\n' + body + '\n'
                          'output[gpu_global_index(t)]=x; }\n')
        target = temp/'refused.ll'
        target.write_text('prior artifact')
        result = run(TVC, '--emit-gpu-nvptx', source, '-o', target, code=1)
        assert target.read_text() == 'prior artifact'
        decisions = [json.loads(line) for line in result.stderr.splitlines() if line.startswith('{')]
        assert any(d.get('reason') == reason for d in decisions), (index, reason, result.stderr)
        assert f'device-call-refused: {reason} at line 8' in result.stderr, (index, result.stderr)
        assert ':8:' in result.stderr, (index, result.stderr)

    print(f'Scalar control PASS: {count} exact raw/O1 device and host results; Q32 composition; '
          f'SM90/120 lowering; traps and {len(refusals)} control/effect refusals')
