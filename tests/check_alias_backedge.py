"""Loop-carried pointer rebinding must not elide runtime alias guards."""
import os
import json
from pathlib import Path
import re
import shlex
import subprocess
import sys
import tempfile

tvc, llc, opt, linker = sys.argv[1:5]


def source(mode):
    opening = 'for turn in 0..3 {' if mode == 'for' else 'var turn: i32 = 0; while turn < 3 {'
    closing = '' if mode == 'for' else 'turn = turn + 1;'
    if mode == 'control':
        opening, closing = 'let turn: i32 = 0;', ''
    if mode == 'continue':
        closing += ' continue;'
    alias = 'let current: *u64 = src;' if mode == 'snapshot' else ''
    read = 'current' if mode == 'snapshot' else 'src'
    return f'''
extern "C" fn dispatches() -> i32;
fn main() {{
    let original: *u64 = alloc(4097);
    let out: *u64 = alloc(4097);
    var src: *u64 = original;
    var j: i32 = 0;
    while j < 4097 {{ original[j] = (j + 11) as u64; out[j] = 7; j = j + 1; }}
    {opening}
        {alias}
        for i in 0..4096 {{ out[i + 1] = {read}[i]; }}
        print(dispatches());
        var bad: i32 = 0;
        j = 0;
        while j < 4096 {{
            var expected: u64 = (j + 11) as u64;
            if turn == 1 {{ expected = 7; }}
            if out[j + 1] != expected {{ bad = bad + 1; }}
            j = j + 1;
        }}
        print(bad);
        if turn == 0 {{ src = out; }} else {{ src = original; }}
        {closing}
    {'' if mode == 'control' else '}'}
    free(out); free(original);
}}
'''


def run(*args, threads='1'):
    p = subprocess.run(list(map(str, args)), capture_output=True, text=True, timeout=60,
                       env=dict(os.environ, TRAVELER_THREADS=threads))
    assert p.returncode == 0, (args, p.returncode, p.stdout, p.stderr)
    return p.stdout


with tempfile.TemporaryDirectory(prefix='alias-backedge-') as tmp:
    d = Path(tmp)
    stub = d / 'counter.c'
    stub.write_text('static int count; void note_dispatch(void) { ++count; } int dispatches(void) { return count; }\n')
    flags = shlex.split(os.environ.get('TRAVELER_LINK_FLAGS', '-no-pie' if sys.platform == 'linux' else ''))
    for mode in ('while', 'for', 'continue', 'snapshot', 'control'):
        tv, ir = d / 'backedge.tv', d / 'backedge.ll'
        tv.write_text(source(mode))
        report = [json.loads(line) for line in run(tvc, tv, '--pfor-alias-report').splitlines()]
        admitted = [row for row in report if row['admitted']]
        assert len(admitted) == 1, (mode, report)
        assert admitted[0]['alias'] == 'checked', (mode, admitted)
        run(tvc, tv, '-o', ir)
        text, count = re.subn(r'  call void @__parallel_for\(ptr @__pfor_worker_\d+,',
                             lambda m: '  call void @note_dispatch()\n' + m[0], ir.read_text())
        assert count == 1, (mode, count)
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
                expected = '1\n0\n' if mode == 'control' else '1\n0\n1\n0\n2\n0\n'
                assert output == expected, (mode, profile, threads, output)
print('alias backedge PASS: while/for/continue/snapshot, disjoint dispatch and overlapping serial fallback; raw/O1/O3 T=1/8')
