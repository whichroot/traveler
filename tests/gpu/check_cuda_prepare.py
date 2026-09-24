"""Exercise snapshot identity, relocation, cache repair, and atomic failures."""
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import shutil
import sys
import tempfile

from check_ieee_bits import ROOT, run
sys.path.insert(0, str(ROOT/'tools'))
import cuda_prepare as prepare


def main():
    tvc, llc, link = sys.argv[1:4]
    with tempfile.TemporaryDirectory(prefix='traveler-prepare-check-') as temporary:
        d = Path(temporary); root = d/'tree'; root.mkdir(); (root/'lib').mkdir()
        (root/'lib/value.tv').write_text('fn value(x: u32) -> u32 { return ieee32_add(x, 0); }')
        (root/'main.tv').write_text('import "lib/value.tv";\n#[kernel] fn map(i: i32, a: *u32, b: *u32) { b[i] = value(a[i]); }')
        cache = d/'cache'
        def build(tree=root, name='result', sm=90, tag='test-closure-v1'):
            return prepare.prepare(tree, 'main.tv', d/(name+'.tvcp'), cache, tvc, llc, sm, tag)
        cold = build(); original = (d/'result.tvcp').read_bytes()
        warm = build()
        assert cold['cache'] == 'miss' and warm['cache'] == 'hit' and cold['key'] == warm['key']
        assert (d/'result.tvcp').read_bytes() == original and cold['dependencies'] == 2
        relocated = d/'relocated'; shutil.copytree(root, relocated)
        assert build(relocated)['cache'] == 'hit'
        (cache/(cold['key']+'.json')).unlink()
        assert build(relocated)['cache'] == 'miss' and (d/'result.tvcp').read_bytes() == original
        (root/'unused.tv').write_text('fn unused() -> i32 { return 99; }')
        assert build()['cache'] == 'hit'
        (root/'lib/value.tv').write_text('fn value(x: u32) -> u32 { return ieee32_mul(x, 1065353216); }')
        changed = build(); assert changed['cache'] == 'miss' and changed['key'] != cold['key']
        (root/'lib/value.tv').write_text('fn value(x: u32) -> u32 { return x; }')
        integer = build(); assert integer['key'] != changed['key']
        assert 'numerical' not in prepare.validate_blob((d/'result.tvcp').read_bytes(), 90)['kernels'][0]
        assert build(sm=80)['key'] != integer['key']
        assert build(tag='test-closure-v2')['key'] != integer['key']
        compiler_copy = d/'compiler-copy'
        compiler_copy.write_bytes(prepare.executable(tvc).read_bytes()+b'\0')
        compiler_copy.chmod(0o755)
        compiler_change = prepare.prepare(root, 'main.tv', d/'compiler-change.tvcp', cache, compiler_copy, llc, 90, 'test-closure-v1')
        assert compiler_change['key'] != integer['key']
        build()
        entry = cache/(integer['key']+'.json')
        for mutation in ('truncated', 'digest', 'request', 'request-boolean', 'artifact', 'wrong-target'):
            record = json.loads(entry.read_bytes())
            if mutation == 'truncated': entry.write_bytes(b'{')
            else:
                if mutation == 'digest': record['sha256'] = '0'*64
                if mutation == 'request': record['request']['abi'] = 99
                if mutation == 'request-boolean': record['request']['abi'] = True
                if mutation == 'artifact': record['artifact'] = '0'*64
                if mutation == 'wrong-target':
                    other = json.loads((cache/(build(sm=80, name='other')['key']+'.json')).read_bytes())
                    for field in ('package', 'sha256', 'artifact'): record[field] = other[field]
                entry.write_bytes(prepare.encoded(record))
            assert build()['cache'] == 'rebuild-corrupt', mutation
        entry.unlink()
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda n: build(name=n), ('parallel0', 'parallel1')))
        assert results[0]['key'] == results[1]['key']
        assert (d/'parallel0.tvcp').read_bytes() == (d/'parallel1.tvcp').read_bytes()
        saved = (d/'result.tvcp').read_bytes()
        (root/'main.tv').write_text('import "missing.tv";')
        try: build()
        except Exception: pass
        else: raise AssertionError('missing import accepted')
        assert (d/'result.tvcp').read_bytes() == saved
        (root/'main.tv').write_text(f'import "{relocated}/main.tv";')
        try: build()
        except ValueError: pass
        else: raise AssertionError('absolute dependency accepted')
        run(tvc, ROOT/'tests/gpu/cuda_package_gate.tv', '-o', d/'gate.ll')
        run(llc, '-filetype=obj', d/'gate.ll', '-o', d/'gate.o')
        run(link, '-no-pie', d/'gate.o', '-o', d/'gate')
        run(d/'gate', d/'result.tvcp')
        print('CUDA prepare PASS: cold/warm, transitive source and policy identity, relocation, target/toolchain invalidation, corrupt repair, concurrent publication, failed-build preservation')


if __name__ == '__main__': main()
