"""Check composable kernel requirements and closed package admission."""
import copy
import hashlib
import json
from pathlib import Path
import struct
import sys
import tempfile

from check_ieee_bits import ROOT, cuda_package, run


def main():
    tvc, llc, link = sys.argv[1:4]
    with tempfile.TemporaryDirectory(prefix='traveler-capabilities-') as directory:
        d = Path(directory)
        (d/'source.tv').write_text('''
#[kernel]
fn narrow(i: i32, input: *u32, output: *u32) { output[i] = ieee32_add(input[i], 0); }
#[kernel]
fn widen(i: i32, input: *u32, output: *u64) { output[i] = ieee32_to_ieee64(input[i]); }
''')
        run(tvc, d/'source.tv', '--emit-gpu-nvptx', '-o', d/'source.ll')
        entries = cuda_package.descriptors((d/'source.ll').read_text())
        assert entries[0]['requires'] == ['ieee32-rne-v1']
        assert entries[1]['requires'] == ['ieee32-rne-v1', 'ieee64-rne-v1']
        run(tvc, ROOT/'tests/gpu/cuda_numeric_projection.tv', '--emit-gpu-nvptx', '-o', d/'projection.ll')
        projection = cuda_package.descriptors((d/'projection.ll').read_text())[0]
        assert projection['requires'] == ['ieee64-rne-v1', 'bounded-read-u64-v1']
        assert cuda_package.kernel_requirements(projection) == (70, (6, 0))
        cuda_package.build(d/'source.ll', d/'source.tvcp', llc, 90)
        run(tvc, ROOT/'tests/gpu/cuda_package_gate.tv', '-o', d/'gate.ll')
        run(llc, '-filetype=obj', d/'gate.ll', '-o', d/'gate.o')
        run(link, '-no-pie', d/'gate.o', '-o', d/'gate')
        run(d/'gate', d/'source.tvcp')
        blob = (d/'source.tvcp').read_bytes()
        length = struct.unpack('<I', blob[8:12])[0]
        original = json.loads(blob[12:12+length]); ptx = blob[12+length:]

        def publish(header, text=ptx):
            encoded = json.dumps(header, separators=(',', ':')).encode()
            (d/'mutated.tvcp').write_bytes(blob[:8]+struct.pack('<I', len(encoded))+encoded+text)

        for capabilities in (None, {}, [], ['unknown-v1'], ['ieee32-rne-v1']*2,
                             ['ieee64-rne-v1', 'ieee32-rne-v1'], [7], ['warp-full32-v1']):
            header = copy.deepcopy(original)
            header['kernels'][0]['requires'] = capabilities
            publish(header)
            assert run(d/'gate', d/'mutated.tvcp', ok=False).returncode != 0
            try:
                cuda_package.validate_kernel(header['kernels'][0])
            except ValueError:
                pass
            else:
                raise AssertionError(capabilities)
        header = copy.deepcopy(original)
        for entry in header['kernels']:
            del entry['requires']
            cuda_package.validate_kernel(entry)
        publish(header)
        run(d/'gate', d/'mutated.tvcp')
        header = copy.deepcopy(original)
        header['kernels'][0].pop('numerical')
        publish(header)
        assert run(d/'gate', d/'mutated.tvcp', ok=False).returncode != 0
        header = copy.deepcopy(original)
        old = f'.version {header["ptx_major"]}.{header["ptx_minor"]}'.encode()
        downgraded = ptx.replace(old, b'.version 3.0')
        assert downgraded != ptx
        header.update(ptx_major=3, ptx_minor=0, ptx_bytes=len(downgraded), ptx_sha256=hashlib.sha256(downgraded).hexdigest())
        publish(header, downgraded)
        assert run(d/'gate', d/'mutated.tvcp', ok=False).returncode != 0
        print('CUDA capabilities PASS: precision composition, bounded-read requirements, closed schemas, legacy descriptors, PTX floor')


if __name__ == '__main__':
    main()
