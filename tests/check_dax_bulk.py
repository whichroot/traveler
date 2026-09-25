"""Check non-atomic DAX copies against byte oracles without a DAX device."""
import argparse
import ctypes as C
import json
import os
from pathlib import Path
import platform
import re
import resource
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]


def run(*args, ok=True):
    p = subprocess.run(list(map(str, args)), capture_output=True, text=True)
    if ok and p.returncode:
        raise AssertionError((args, p.returncode, p.stdout, p.stderr))
    return p


def main():
    parser = argparse.ArgumentParser()
    for name in ('tvc', 'llc', 'opt', 'link'):
        parser.add_argument(name)
    a = parser.parse_args()
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    os.environ['TRAVELER_THREADS'] = '4'
    with tempfile.TemporaryDirectory(prefix='traveler-dax-bulk-') as directory:
        d = Path(directory)
        source = d/'copy.tv'
        source.write_text(f'import "{ROOT}/src/lib/mem/dax.tv";\n'+'''
#[export]
fn probe(src: *u8, length: i64, rel: i64, dst: *u8, n: i64) {
    var view: DaxView = DaxView { ptr: src, offset: 0, length: length };
    dax_copy_bulk(&view, rel, dst, n);
}
fn main(argc: i32, argv: **u8) {
    let data: *u8 = alloc(64);
    var view: DaxView = DaxView { ptr: data, offset: 0, length: 64 };
    if argc < 2 { return; }
    let which: u8 = argv[1][0];
    if which == 97 { dax_copy_bulk(&view, 0, data, -1); }
    if which == 98 { dax_copy_bulk(&view, -1, data, 1); }
    if which == 99 { dax_copy_bulk(&view, 63, data, 2); }
    if which == 100 { dax_copy_bulk(&view, 0, null, 1); }
    if which == 101 { view.ptr = null; dax_copy_bulk(&view, 0, data, 1); }
    if which == 102 { dax_copy_bulk(null, 0, data, 0); }
    if which == 103 { dax_copy_bulk(&view, 0, data, 1); }
    if which == 104 { dax_copy_bulk(&view, 0, &data[1], 8); }
    if which == 105 { dax_copy_bulk(&view, 1, data, 8); }
    if which == 106 { view.length = -1; dax_copy_bulk(&view, 0, data, 0); }
    if which == 107 { dax_copy_bulk(&view, 65, data, 0); }
    if which == 108 { view.ptr = (18446744073709551612 as u64) as *u8; dax_copy_bulk(&view, 8, data, 1); }
    if which == 109 { view.ptr = (18446744073709551612 as u64) as *u8; dax_copy_bulk(&view, 0, data, 8); }
    if which == 110 { dax_copy_bulk(&view, 0, (18446744073709551612 as u64) as *u8, 8); }
    exit(99);
}
''')
        raw = d/'raw.ll'
        run(a.tvc, source, '-o', raw)
        text = raw.read_text()
        worker = re.findall(r'define internal void @__pfor_worker_\d+\([^\n]+\) \{(.*?)\n}', text, re.S)
        assert any('load i64, ptr' in body and 'store i64' in body and 'load atomic' not in body for body in worker)
        assert 'load atomic i8' in text, 'legacy per-byte atomic copy disappeared'
        report = [json.loads(row) for row in run(a.tvc, source, '--pfor-alias-report').stdout.splitlines()]
        assert any(row['fn'] == 'dax_copy_bulk_words' and row['alias'] == 'checked' for row in report), report
        modes = ['none', 'o1']
        if platform.system() == 'Linux' and platform.machine() == 'x86_64':
            modes.append('o3')
        count = 0
        for mode in modes:
            ir = raw
            if mode != 'none':
                ir = d/f'{mode}.ll'
                flags = ['-mcpu', 'x86-64'] if mode == 'o3' else []
                run(a.tvc, source, '-o', ir, '--opt-level', mode, '-opt', a.opt, *flags)
            run(a.opt, '-passes=verify', '-disable-output', ir)
            obj = d/f'{mode}.o'
            run(a.llc, '-relocation-model=pic', '-filetype=obj', ir, '-o', obj)
            shared = d/f'{mode}.so'
            run(a.link, '-dynamiclib' if platform.system() == 'Darwin' else '-shared', obj, '-o', shared)
            lib = C.CDLL(str(shared))
            lib.probe.argtypes = [C.c_void_p, C.c_int64, C.c_int64, C.c_void_p, C.c_int64]
            lib.probe.restype = None
            values = bytes((i*37+(i >> 8)*53+11) & 255 for i in range(65580))
            src = C.create_string_buffer(values)
            dst = C.create_string_buffer(len(values))
            for off in range(8):
                for target in range(8):
                    for n in [*range(32), 63, 64, 65, 255, 256, 257, 4099, 65539]:
                        C.memset(dst, 205, len(values))
                        lib.probe(C.addressof(src), off+n, off, C.addressof(dst)+target, n)
                        expected = bytes([205])*target + values[off:off+n] + bytes([205])*(len(values)-target-n)
                        assert dst.raw == expected, (mode, off, target, n)
                        count += 1
            assert src.raw[:-1] == values, mode
            lib.probe(None, 0, 0, None, 0)
            lib.probe(C.addressof(src), len(values), len(values), None, 0)
            exe = d/f'{mode}-negative'
            flags = [] if platform.system() == 'Darwin' else ['-no-pie']
            run(a.link, *flags, obj, '-o', exe)
            for case in 'abcdefghijklmn':
                p = run(exe, case, ok=False)
                assert p.returncode == 96 and 'dax: bulk copy' in p.stderr, (mode, case, p)
        print(f'DAX bulk copy PASS: {count} byte-oracle cases, wide non-atomic loop, guards, and 14 refusals/profile')


if __name__ == '__main__':
    main()
