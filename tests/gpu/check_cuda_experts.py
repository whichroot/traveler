"""Compare exact expert traces across placement, prefetch, batching, and fusion."""
import argparse
import hashlib
import json
from pathlib import Path
import statistics
import subprocess
import sys
import tempfile

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
NAMES = ('stream_serial', 'stream_n1', 'cache_demand', 'cache_n1', 'cache_n3_q4',
         'cache_n1_q4', 'batch8_n1', 'cache_n1_fused', 'resident_split', 'resident_fused')
PATTERNS = ('hot', 'uniform', 'thrash5')


def run(*args):
    p = subprocess.run([str(a) for a in args], capture_output=True, text=True, timeout=300)
    assert p.returncode == 0, (args, p.returncode, p.stdout, p.stderr)
    return p.stdout


def trace(pattern):
    if pattern == 0:
        return [i % 2 if i % 8 < 6 else 2 + (i // 8 * 2 + i % 2) % 6 for i in range(32)]
    return [i % (8 if pattern == 1 else 5) for i in range(32)]


def model(pattern, mode):
    capacity = 1 if mode == 0 else 2 if mode == 1 else 8 if mode >= 8 else 4
    ahead = 0 if mode in (0, 2, 8, 9) else 3 if mode == 4 else 1
    experts = trace(pattern)
    order = list(range(32))
    if mode == 6:
        order = [i for start in range(0, 32, 8) for i in sorted(range(start, start+8), key=lambda i: experts[i])]
    slots = [dict(expert=i if mode >= 8 else -1, reserved=0, stamp=-1, used=False) for i in range(capacity)]
    assigned = {}; prepared = 0
    hits = misses = evictions = prefetched = used = 0
    for position, request in enumerate(order):
        while prepared < min(32, position+ahead+1):
            upcoming = order[prepared]
            selected = next((i for i, s in enumerate(slots) if mode > 1 and s['expert'] == experts[upcoming]), None)
            if selected is not None:
                hits += 1
            else:
                selected = min((i for i, s in enumerate(slots) if not s['reserved']), key=lambda i: slots[i]['stamp'])
                s = slots[selected]
                evictions += s['expert'] >= 0
                s.update(expert=experts[upcoming], used=False)
                misses += 1; prefetched += prepared > position
            slots[selected]['reserved'] += 1; slots[selected]['stamp'] = prepared
            assigned[upcoming] = selected; prepared += 1
        s = slots[assigned[request]]
        assert s['expert'] == experts[request] and s['reserved'] > 0
        s['reserved'] -= 1
        if not s['used']:
            used += 1; s['used'] = True
    return [hits, misses, evictions, prefetched, used]


def parse(output, trials):
    lines = output.splitlines(); records = []
    for i, line in enumerate(lines):
        if line != 'sample':
            continue
        values = [int(x) for x in lines[i+1:i+49]]
        assert len(values) == 48
        pattern, mode, trial, size, ns = values[:5]
        assert values[5:10] == model(pattern, mode), (pattern, mode, values[5:10], model(pattern, mode))
        assert 0 < ns and all(0 < t <= ns for t in values[16:])
        records.append(dict(pattern=PATTERNS[pattern], mode=NAMES[mode], trial=trial, bytes=size,
                            elapsed_ns=ns, hits=values[5], misses=values[6], evictions=values[7],
                            prefetched=values[8], used_loads=values[9], staging_ns=values[10],
                            pin_wait_ns=values[11], pin_pending=values[12], queue_wait_ns=values[13],
                            queue_pending=values[14], preload_ns=values[15], completed_ns=values[16:]))
    assert len(records) == 30*trials and 'expert placement PASS' in lines
    assert len({(r['pattern'], r['mode'], r['trial']) for r in records}) == len(records)
    return records


def summarize(records):
    rows = []
    for size in sorted({r['bytes'] for r in records}):
        for pattern in PATTERNS:
            for mode in NAMES:
                group = [r for r in records if (r['bytes'], r['pattern'], r['mode']) == (size, pattern, mode)]
                samples = [r['elapsed_ns']/1e6 for r in group]
                median = lambda key: statistics.median(r[key] for r in group)/1e6
                rows.append(dict(bytes=size, pattern=pattern, mode=mode, median_ms=statistics.median(samples),
                                 min_ms=min(samples), max_ms=max(samples), hits=group[0]['hits'],
                                 upload_bytes=group[0]['misses']*size, prefetched_bytes=group[0]['prefetched']*size,
                                 unused_prefetch_bytes=0, preload_bytes=8*size if mode.startswith('resident') else 0,
                                 unused_preload_bytes=(8-group[0]['used_loads'])*size if mode.startswith('resident') else 0,
                                 staging_ms=median('staging_ns'), pin_wait_ms=median('pin_wait_ns'),
                                 queue_wait_ms=median('queue_wait_ns'), preload_ms=median('preload_ns'),
                                 observed_p95_ms=statistics.median(sorted(r['completed_ns'])[30] for r in group)/1e6,
                                 cold_total_ms=statistics.median(r['elapsed_ns']+r['preload_ns'] for r in group)/1e6))
    return rows


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('tvc'); parser.add_argument('llc'); parser.add_argument('link')
    parser.add_argument('--cuda'); parser.add_argument('--sm', type=int, default=120)
    parser.add_argument('--sizes', type=int, nargs='+', default=[65536, 1048576, 8388608, 33554432])
    parser.add_argument('--trials', type=int, default=3)
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    with tempfile.TemporaryDirectory() as directory:
        temp = Path(directory); ir = temp / 'expert.ll'; package = temp / 'expert.tvcp'
        run(args.tvc, '--emit-gpu-nvptx', HERE / 'cuda_expert_kernels.tv', '-o', ir)
        run(sys.executable, ROOT / 'tools/cuda_package.py', ir, '--llc', args.llc, '--sm', args.sm if args.cuda else 90, '-o', package)
        run(args.tvc, HERE / 'cuda_expert_gate.tv', '--emit', 'obj', '-llc', args.llc, '-o', temp / 'gate.o')
        expected_bytes = 4096*sum(model(p, m)[1]+(8 if m >= 8 else 0) for p in range(3) for m in range(10))
        run(args.link, '-no-pie', '-Wall', '-Wextra', '-Werror', '-DMOCK_ASYNC_CLEAN', f'-DMOCK_EXPERT_UPLOAD_BYTES={expected_bytes}', temp / 'gate.o', HERE / 'cuda_expert_mock.c', '-o', temp / 'mock')
        parse(run(temp / 'mock', package, 4096, 1, 0), 1)
        print('expert placement mock PASS: 30 cases, full output oracles, cache model, no leaks or context-wide waits')
        if args.cuda:
            run(args.link, '-no-pie', temp / 'gate.o', args.cuda, f'-Wl,-rpath,{Path(args.cuda).parent}', '-o', temp / 'native')
            records = []; module_ns = {}
            for size in args.sizes:
                output = run(temp / 'native', package, size, args.trials, args.sm)
                module_ns[str(size)] = int(output.splitlines()[1])
                result = parse(output, args.trials)
                records.extend(result)
                for r in summarize(result):
                    print(f'{size} {r["pattern"]:8} {r["mode"]:16} ms={r["median_ms"]:.3f} [{r["min_ms"]:.3f},{r["max_ms"]:.3f}] upload_MiB={r["upload_bytes"]/1048576:g} hits={r["hits"]} preload_ms={r["preload_ms"]:.3f} stage_ms={r["staging_ms"]:.3f} queue_wait_ms={r["queue_wait_ms"]:.3f} p95_ms={r["observed_p95_ms"]:.3f}')
            if args.output:
                sources = [HERE / 'cuda_expert_gate.tv', HERE / 'cuda_expert_kernels.tv',
                           ROOT / 'src/lib/gpu/cuda_async.tv', ROOT / 'src/lib/gpu/cuda_resident.tv']
                metadata = dict(benchmark='expert-placement-v1', sm=args.sm, requests=32, experts=8, warmup_ms=250,
                                module_load_ns=module_ns, source_sha256={p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in sources})
                args.output.write_text('\n'.join(json.dumps(r, separators=(',', ':')) for r in [metadata, *records])+'\n')
            print(f'expert placement native SM{args.sm} PASS')


if __name__ == '__main__':
    main()
