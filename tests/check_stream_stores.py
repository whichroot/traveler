"""Check streaming-store values, parallel admission, fences, and target refusals."""
import argparse
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile


SOURCE = """
fn copy(dst: *u64, src: *u64, n: i64) {
    for i in 0..n { stream_store_u64(dst, i, src[i]); }
    stream_store_fence();
}
fn copy32(dst: *u64, src: *u64, n: i32) {
    for i in 0..n { stream_store_u64(dst, i, src[i]); }
    stream_store_fence();
}
fn generic_copy<T>(dst: *T, src: *T, n: i32) {
    for i in 0..n { stream_store_u64(dst, i, src[i]); }
    stream_store_fence();
}
fn fixed(dst: *u64, src: *u64, n: i32) {
    for i in 0..n { stream_store_u64(dst, 0, src[i]); }
    stream_store_fence();
}
fn fenced(dst: *u64, src: *u64, n: i32) {
    for i in 0..n {
        stream_store_u64(dst, i, src[i]);
        stream_store_fence();
    }
}
fn store_helper(dst: *u64, index: i32, value: u64) {
    stream_store_u64(dst, index, value);
}
fn helper_loop(dst: *u64, src: *u64, n: i32) {
    for i in 0..n { store_helper(dst, 0, src[i]); }
    stream_store_fence();
}
fn main() {
    let n: i64 = 32779;
    let src: *u64 = alloc(n + 2);
    let dst: *u64 = alloc(n + 2);
    for i in 0..n + 2 {
        src[i] = (i as u64) * 11400714819323198485;
        dst[i] = 777;
    }
    copy(&dst[1], &src[1], n);
    var bad: i64 = 0;
    var i: i64 = 1;
    while i <= n {
        if dst[i] != src[i] { bad = bad + 1; }
        i = i + 1;
    }
    if dst[0] != 777 || dst[n + 1] != 777 { bad = bad + 1; }
    copy32(dst, src, 0);
    if dst[0] != 777 { bad = bad + 1; }
    copy32(dst, src, 1);
    if dst[0] != src[0] { bad = bad + 1; }
    copy32(dst, src, 32779);
    generic_copy(dst, src, 32779);
    copy(dst, dst, n);
    i = 0;
    while i < n {
        if dst[i] != src[i] { bad = bad + 1; }
        i = i + 1;
    }
    src[0] = 91;
    copy(&src[1], src, n);
    i = 0;
    while i <= n {
        if src[i] != 91 { bad = bad + 1; }
        i = i + 1;
    }
    stream_store_u64(dst, 0, 18446744073709551615);
    stream_store_fence();
    if dst[0] != 18446744073709551615 { bad = bad + 1; }
    print(bad);
    free(src);
    free(dst);
}
"""


def run(*args, ok=True, threads=1):
    p = subprocess.run(list(map(str, args)), capture_output=True, text=True,
                       env={**os.environ, 'TRAVELER_THREADS': str(threads)})
    if ok and p.returncode:
        raise AssertionError((args, p.returncode, p.stdout, p.stderr))
    return p


def main():
    parser = argparse.ArgumentParser()
    for name in ('tvc', 'llc', 'opt', 'link'):
        parser.add_argument(name)
    a = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix='traveler-stream-') as directory:
        d = Path(directory)
        source = d/'copy.tv'
        source.write_text(SOURCE)
        ir = d/'copy.ll'
        run(a.tvc, source, '-target', 'x86_64-linux-gnu', '-o', ir)
        raw = ir.read_text()
        workers = re.findall(r'define internal void @__pfor_worker_\d+\([^\n]*\).*?\n}', raw, re.S)
        streaming = [worker for worker in workers if '!nontemporal' in worker]
        assert len(streaming) == 3, len(streaming)
        for worker in streaming:
            assert worker.count('"mfence"') == 1, worker
            assert re.search(r'call void asm sideeffect "mfence", "~\{memory\}"\(\)\n  ret void', worker), worker
        report = run(a.tvc, source, '--pfor-report').stdout.splitlines()
        copies = [json.loads(row) for row in report if row.startswith('{')]
        for name in ('copy', 'copy32'):
            assert any(row['fn'] == name and row['dispatched'] == 1 for row in copies), copies
        for name in ('fixed', 'fenced', 'helper_loop'):
            rows = [row for row in copies if row['fn'] == name]
            assert rows and all(row['dispatched'] == 0 for row in rows), copies
        for mode in ('raw', 'o1', 'o3'):
            selected = ir
            if mode == 'o1':
                selected = d/'o1.ll'
                run(a.opt, '-passes=default<O1>', '-S', ir, '-o', selected)
            if mode == 'o3':
                selected = d/'o3.ll'
                run(a.tvc, source, '-o', selected, '--opt-level', 'o3',
                    '-mcpu', 'x86-64', '-opt', a.opt, '-target', 'x86_64-linux-gnu')
            run(a.opt, '-passes=verify', '-disable-output', selected)
            asm = d/f'{mode}.s'
            run(a.llc, '-mtriple=x86_64-linux-gnu', selected, '-o', asm)
            instructions = asm.read_text()
            assert re.search(r'\bmovnti[qwl]?\b', instructions), instructions
            assert re.search(r'\bmfence\b', instructions), instructions
            obj = d/f'{mode}.o'
            exe = d/mode
            run(a.llc, '-mtriple=x86_64-linux-gnu', '-filetype=obj', selected, '-o', obj)
            run(a.link, '-no-pie', obj, '-o', exe)
            for threads in (1, 4):
                assert run(exe, threads=threads).stdout.strip() == '0', (mode, threads)
        for flags in (['--eval'], ['-target', 'aarch64-linux-gnu'],
                      ['--emit-gpu-nvptx'], ['--emit-gpu']):
            output = d/'refused.ll'
            output.write_text('preserve')
            p = run(a.tvc, source, *flags, '-o', output, ok=False)
            assert p.returncode != 0 and 'native x86_64 CPU codegen' in p.stderr, p.stderr
            assert output.read_text() == 'preserve', flags
        invalid = {
            'arity': 'stream_store_u64(p, 0);',
            'pointer': 'stream_store_u64(p as *u32, 0, 1 as u64);',
            'value': 'stream_store_u64(p, 0, 1 as i32);',
            'index': 'stream_store_u64(p, true, 1 as u64);',
            'expression': 'let x: u64 = stream_store_u64(p, 0, 1 as u64);',
            'fence': 'stream_store_fence(1);',
            'fence_value': 'let x: u64 = stream_store_fence();',
            'fence_inferred': 'let x = stream_store_fence();',
        }
        for name, statement in invalid.items():
            source.write_text('fn main() { let p: *u64 = alloc(1); '+statement+' }')
            p = run(a.tvc, source, '-o', d/f'{name}.ll', ok=False)
            assert p.returncode != 0 and 'stream_store_' in p.stderr, (name, p.stderr)
        source.write_text('fn stream_store_u64(x: i32) { print(x); }\n'
                          'fn main() { stream_store_u64(7); stream_store_fence(); }')
        run(a.tvc, source, '-o', d/'shadow.ll')
        assert '!nontemporal' not in (d/'shadow.ll').read_text()
        assert '"mfence"' in (d/'shadow.ll').read_text()
    print('PASS: streaming stores, worker fences, raw/O1/O3 values, alias fallback, and refusals')


if __name__ == '__main__':
    main()
