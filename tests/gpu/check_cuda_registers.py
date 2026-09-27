"""Check source-owned, per-entry register caps through LLVM and CUDA packages."""
import copy
import json
from pathlib import Path
import struct
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[2]
TVC, LLC, OPT, LINK = sys.argv[1:5]
sys.path.insert(0, str(ROOT/'tools'))
import cuda_package as package
import cuda_prepare as prepare


def run(*args, ok=True):
    p = subprocess.run(list(map(str, args)), capture_output=True, text=True, timeout=90)
    assert (p.returncode == 0) == ok, (args, p.returncode, p.stdout, p.stderr)
    return p


def refused(fn):
    try:
        fn()
    except ValueError:
        return
    raise AssertionError('invalid register metadata accepted')


with tempfile.TemporaryDirectory() as directory:
    d = Path(directory)
    src = d/'caps.tv'; ir = d/'caps.ll'; blob = d/'caps.tvcp'
    plain = '#[kernel] fn plain(i:i32,a:*u32,b:*u32) { b[i] = a[i]+1; }\n'
    text = (f'import "{ROOT}/src/lib/gpu/thread.tv";\n'
            + plain + '#[cuda_max_registers(64)]\n' + plain.replace('plain', 'first')
            + '#[kernel]\n#[cuda_max_registers(96)]\n'
            + 'fn second(i:i32,a:*u32,b:*u32) { b[i] = a[i]*2; }\n'
            + '#[kernel]\n#[cuda_max_registers(128)]\n'
            + 'fn cooperative(t:GpuThread,a:*u64,b:*u64) { let i = gpu_global_index(t); b[i] = a[i]+1; }\n')
    src.write_text(text)
    run(TVC, src, '--emit-gpu-nvptx', '-o', ir)
    original = ir.read_text(); entries = package.descriptors(original)
    assert [k.get('max_registers') for k in entries] == [None, 64, 96, 128]
    package.build(ir, blob, LLC, 90)
    header = prepare.validate_blob(blob.read_bytes(), 90)
    assert header['kernels'] == entries
    if OPT:
        run(OPT, '-passes=default<O2>', '-verify-each', '-S', ir, '-o', d/'optimized.ll')
        run(LLC, '-mcpu=sm_90', d/'optimized.ll', '-o', d/'optimized.ptx')
        package.validate_register_limits((d/'optimized.ptx').read_bytes(), entries)
    gate = d/'gate'
    run(TVC, ROOT/'tests/gpu/cuda_package_gate.tv', '--emit', 'exe', '-llc', LLC, '-cc', LINK, '-o', gate)
    assert run(gate, blob).stdout == '90\n4\n'
    for value in (0, -1, 256, True, '64'):
        changed = copy.deepcopy(header); changed['kernels'][1]['max_registers'] = value
        refused(lambda: package.validate_kernel(changed['kernels'][1]))
        size = struct.unpack('<I', blob.read_bytes()[8:12])[0]
        encoded = json.dumps(changed).encode()
        (d/'bad.tvcp').write_bytes(package.MAGIC + struct.pack('<I', len(encoded)) + encoded + blob.read_bytes()[12+size:])
        assert run(gate, d/'bad.tvcp', ok=False).returncode == 2
    for bad in (original.replace('"nvvm.maxnreg"="64"', ''),
                original.replace('"nvvm.maxnreg"="64"', '"nvvm.maxnreg"="63"'),
                original.replace('"nvvm.maxnreg"="64"', '"nvvm.maxnreg"="64" "nvvm.maxnreg"="64"')):
        refused(lambda: package.descriptors(bad))
    size = struct.unpack('<I', blob.read_bytes()[8:12])[0]; ptx = blob.read_bytes()[12+size:]
    for bad in (ptx.replace(b'.maxnreg 64', b''), ptx.replace(b'.maxnreg 64', b'.maxnreg 63'),
                ptx.replace(b'.maxnreg 64', b'.maxnreg 64\n.maxnreg 64')):
        refused(lambda: package.validate_register_limits(bad, entries))
    for attr in ('0', '-1', '256', '18446744073709551617', '64+1', 'cap', '64,32', ''):
        src.write_text(f'#[cuda_max_registers({attr})]\n' + plain)
        assert 'cuda_max_registers' in run(TVC, src, '--emit-gpu-nvptx', '-o', ir, ok=False).stderr
    for bad in ('#[cuda_max_registers(64)]\n' + plain.replace('#[kernel]', ''),
                '#[cuda_max_registers(64)]\n#[cuda_max_registers(64)]\n' + plain,
                '#[cuda_max_registers(64)] extern "C" fn x();',
                '#[cuda_max_registers(64)] struct X { x:i32 }',
                '#[cuda_max_registers(64)]'):
        src.write_text(bad)
        assert 'cuda_max_registers' in run(TVC, src, '--emit-gpu-nvptx', '-o', ir, ok=False).stderr
    src.write_text(text)
    assert 'CUDA device target' in run(TVC, src, '--emit-gpu', '-o', ir, ok=False).stderr
    run(TVC, src, '--emit', 'ir', '-o', d/'host.ll')
    src.write_text('#[cuda_max_registers(64)]\n' + plain)
    cold = prepare.prepare(d, 'caps.tv', blob, d/'cache', TVC, LLC, 90, 'register-test')
    warm = prepare.prepare(d, 'caps.tv', blob, d/'cache', TVC, LLC, 90, 'register-test')
    assert cold['cache'] == 'miss' and warm['cache'] == 'hit'
    src.write_text('#[cuda_max_registers(96)]\n' + plain)
    changed = prepare.prepare(d, 'caps.tv', blob, d/'cache', TVC, LLC, 90, 'register-test')
    assert changed['cache'] == 'miss' and changed['key'] != cold['key']
    print('CUDA register caps PASS: per-entry LLVM/PTX, package/runtime validation, refusals, and cache identity')
