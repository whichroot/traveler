"""Aliased capture writes must fall back to live source bindings, not snapshots."""
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys
import tempfile

tvc, llc, opt, linker = sys.argv[1:5]


def source(mode, ty):
    global_decl = f'var scalar: {ty} = 0;' if mode == 'global' else ''
    setup = f'scalar = 0;' if mode == 'global' else f'var scalar: {ty} = 0;'
    pointee = ty
    address = '&scalar'
    written, read, expected = '7', 'scalar', '0'
    cleanup = ''
    if mode == 'pointer':
        setup = '''let original: *i64 = alloc(1); original[0] = 11;
        let replacement: *i64 = alloc(1); replacement[0] = 7;
        var scalar: *i64 = original;'''
        pointee, address = 'u64', '(&scalar) as *u64'
        written, read, expected = 'replacement as u64', 'scalar[0]', '11'
        cleanup = 'free(replacement); free(original);'
    assignment = f'out[i] = {read};'
    if mode == 'closure':
        assignment = 'let get = |ignored: i64| -> i64 scalar; out[i] = get(0);'
    return f'''
extern "C" fn dispatches() -> i32;
{global_decl}
fn job(alias: i32) {{
    {setup}
    let scratch: *{pointee} = alloc(4096);
    let out: *{ty} = alloc(4096);
    var p: *{pointee} = scratch;
    if alias != 0 {{ p = {address}; }}
    for i in 0..4096 {{ if i == 0 {{ p[i] = {written}; }} {assignment} }}
    print(dispatches());
    var want: {ty} = {expected};
    if alias != 0 {{ want = 7; }}
    var bad: i32 = 0;
    var j: i32 = 0;
    while j < 4096 {{ if out[j] != want {{ bad = bad + 1; }} j = j + 1; }}
    print(bad);
    free(out); free(scratch); {cleanup}
}}
fn main() {{ job(1); job(0); }}
'''


def run(*args, threads='1'):
    p = subprocess.run(list(map(str, args)), capture_output=True, text=True, timeout=60,
                       env=dict(os.environ, TRAVELER_THREADS=threads))
    assert p.returncode == 0, (args, p.returncode, p.stdout, p.stderr)
    return p.stdout


with tempfile.TemporaryDirectory(prefix='scalar-capture-alias-') as tmp:
    d = Path(tmp)
    stub = d / 'counter.c'
    stub.write_text('static int count; void note_dispatch(void) { ++count; } int dispatches(void) { return count; }\n')
    flags = shlex.split(os.environ.get('TRAVELER_LINK_FLAGS', '-no-pie' if sys.platform == 'linux' else ''))
    for mode, ty in (('global', 'i32'), ('global', 'i64'), ('local', 'i32'), ('local', 'i64'), ('pointer', 'i64'), ('closure', 'i64')):
        tv, ir = d / 'capture.tv', d / 'capture.ll'
        tv.write_text(source(mode, ty))
        run(tvc, tv, '-o', ir)
        text, count = re.subn(r'  call void @__parallel_for\(ptr @__pfor_worker_\d+,',
                             lambda m: '  call void @note_dispatch()\n' + m[0], ir.read_text())
        assert count == 1, (mode, ty, count)
        ir.write_text(text + '\ndeclare void @note_dispatch()\n')
        for profile in ('raw', 'O1', 'O3'):
            selected = ir
            if profile != 'raw':
                selected = d / 'optimized.ll'
                run(opt, '-passes=default<' + profile + '>', '-S', ir, '-o', selected)
            run(llc, '-filetype=obj', selected, '-o', d / 'job.o')
            run(linker, *flags, d / 'job.o', stub, '-o', d / 'job')
            for threads in ('1', '8'):
                output = run(d / 'job', threads=threads)
                expected = '0\n0\n0\n0\n' if mode == 'pointer' else '0\n0\n1\n0\n'
                assert output == expected, (mode, ty, profile, threads, output)
print('scalar capture alias PASS: global/local i32/i64 and pointer slots; live serial fallback, disjoint dispatch; raw/O1/O3 T=1/8')
