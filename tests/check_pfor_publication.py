"""Exercise complete and torn pool snapshots with deterministic scheduling hooks."""
from pathlib import Path
import re
import os
import shlex
import subprocess
import sys
import tempfile

tvc, llc, opt, link = sys.argv[1:5]
here = Path(__file__).resolve().parent
link_flags = shlex.split(os.environ.get('TRAVELER_LINK_FLAGS', '-no-pie' if sys.platform == 'linux' else ''))


def run(*args, success=True):
    result = subprocess.run([str(a) for a in args], capture_output=True, text=True, timeout=90)
    if success:
        assert result.returncode == 0, (args, result.returncode, result.stdout, result.stderr)
    return result


def insert(text, anchor, extra):
    assert text.count(anchor) == 1, anchor
    return text.replace(anchor, anchor + extra)


with tempfile.TemporaryDirectory(prefix='traveler-publication-') as directory:
    d = Path(directory)
    raw = d / 'fixture.ll'
    run(tvc, here / 'pfor/pfor_i64_bounds.tv', '-o', raw)
    text = raw.read_text()
    declarations = '\n'.join(re.findall(r'^declare .*$', text, re.M))
    globals_ = '\n'.join(re.findall(r'^@(?:__pfor_\w+|\.__tv_threads) = .*$', text, re.M))
    globals_ = globals_.replace(' = internal global', ' = global')
    functions = []
    for name in ('__get_num_cores', '__pfor_pool_init_once', '__pfor_worker', '__pfor_dispatch',
                 '__parallel_for', '__parallel_for_i64'):
        match = re.search(r'^define internal [^\n]*@' + name + r'\([^\n]*\) \{\n.*?^\}', text, re.M | re.S)
        assert match, name
        functions.append(match.group().replace('define internal ', 'define ', 1))
    ir = declarations + '\n' + globals_ + '\n' + '\n\n'.join(functions) + '\n'
    assert len(re.findall(r'load atomic .*@__pfor_job_', ir)) == 6
    assert len(re.findall(r'store atomic .*@__pfor_job_', ir)) == 6
    assert 'fence release\n' in ir and 'fence acquire\n' in ir
    worker, dispatcher = functions[2:4]
    reader_order = ['%g = load atomic i64, ptr @__pfor_gen acquire',
                    '%fn = load atomic', '%ctx = load atomic', '%lo = load atomic',
                    '%hi = load atomic', '%nthr = load atomic', '%wide = load atomic',
                    'fence acquire', '%gv = load atomic i64, ptr @__pfor_gen monotonic']
    writer_order = ['store atomic i64 %writing, ptr @__pfor_gen monotonic', 'fence release',
                    'store atomic ptr %fn', 'store atomic ptr %ctx', 'store atomic i64 %lo',
                    'store atomic i64 %hi', 'store atomic i32 %nthr', 'store atomic i32 %wide',
                    'store atomic i32 0, ptr @__pfor_ack',
                    'store atomic i64 %g1, ptr @__pfor_gen release']
    for body, order in ((worker, reader_order), (dispatcher, writer_order)):
        positions = [body.index(instruction) for instruction in order]
        assert positions == sorted(positions), order
    uninstrumented = d / 'uninstrumented.ll'
    uninstrumented.write_text(ir)
    if 'aarch64' in run(llc, '--version').stdout:
        assembly = d / 'aarch64.s'
        run(llc, '-mtriple=aarch64-linux-gnu', uninstrumented, '-o', assembly)
        asm = assembly.read_text()
        assert re.search(r'\bdmb\s+ish\s*$', asm, re.M)
        assert re.search(r'\bdmb\s+ishld\s*$', asm, re.M)
        print('AArch64 publication fences PASS: release and acquire barriers emitted')
    ir = insert(ir, '  %idx64p = sext i32 %idx to i64\n', '  call void @publication_entry(i32 %idx)\n')
    ir = insert(ir, '  %fn = load atomic ptr, ptr @__pfor_job_fn monotonic, align 8\n',
                '  call void @publication_after_fn(i32 %idx)\n')
    ir = insert(ir, '  %wide = load atomic i32, ptr @__pfor_job_wide monotonic, align 4\n',
                '  call void @publication_snapshot(i32 %idx, ptr %fn, ptr %ctx, i64 %lo, i64 %hi, i32 %nthr, i32 %wide)\n')
    # Dispatch has a spin_more block too; only instrument the worker's copy.
    start = ir.index('define ptr @__pfor_worker(')
    end = ir.index('\n}', start) + 2
    worker = ir[start:end]
    for label, call in [('spin_more', '@publication_spin_more(i32 %idx, i64 %g)'),
                        ('park_wait', '@publication_park(i32 %idx)'),
                        ('retry', '@publication_retry(i32 %idx)'),
                        ('steady', '@publication_steady(i32 %idx)')]:
        worker = insert(worker, label + ':\n', '  call void ' + call + '\n')
    ir = ir[:start] + worker + ir[end:]
    for anchor, phase in [('  store atomic ptr %ctx, ptr @__pfor_job_ctx monotonic, align 8\n', 1),
                          ('  store atomic i32 %nthr, ptr @__pfor_job_nthr monotonic, align 4\n', 2),
                          ('  store atomic i32 0, ptr @__pfor_ack monotonic, align 4\n', 0)]:
        ir = insert(ir, anchor, f'  call void @publication_writer(i32 %nthr, i32 {phase})\n')
    ir = insert(ir, '  call void @publication_writer(i32 %nthr, i32 0)\n',
                '  call void @publication_writer(i32 %nthr, i32 3)\n')
    ir += '''
declare void @publication_entry(i32)
declare void @publication_after_fn(i32)
declare void @publication_snapshot(i32, ptr, ptr, i64, i64, i32, i32)
declare void @publication_spin_more(i32, i64)
declare void @publication_park(i32)
declare void @publication_retry(i32)
declare void @publication_steady(i32)
declare void @publication_writer(i32, i32)
'''
    runtime = d / 'runtime.ll'
    runtime.write_text(ir)
    for profile in ('raw', 'O1', 'O3'):
        selected = runtime
        if profile != 'raw':
            selected = d / f'{profile}.ll'
            run(opt, f'-passes=default<{profile}>', '-verify-each', '-S', runtime, '-o', selected)
        obj = d / 'runtime.o'
        run(llc, '-filetype=obj', selected, '-o', obj)
        exe = d / 'gate'
        run(link, '-std=c11', '-O2', '-pthread', *link_flags, '-Wall', '-Wextra', '-Werror',
            obj, here / 'pfor/publication_harness.c', '-o', exe)
        for mode in range(4):
            print(profile, run(exe, mode).stdout.strip())
    mutant = d / 'unmarked.ll'
    mutant.write_text(ir.replace('  store atomic i64 %writing, ptr @__pfor_gen monotonic, align 8\n', ''))
    for profile in ('raw', 'O3'):
        selected = mutant
        if profile == 'O3':
            selected = d / 'unmarked-O3.ll'
            run(opt, '-passes=default<O3>', '-verify-each', '-S', mutant, '-o', selected)
        run(llc, '-filetype=obj', selected, '-o', d / 'mutant.o')
        run(link, '-std=c11', '-O2', '-pthread', *link_flags, d / 'mutant.o',
            here / 'pfor/publication_harness.c', '-o', d / 'mutant')
        for mode in range(3):
            failed = run(d / 'mutant', mode, success=False)
            assert failed.returncode == 1 and 'snapshot accepted during publication' in failed.stderr, failed
    print('publication negative controls PASS: missing odd mark admits complete and torn records')
