"""Compare native geometry admission against an exact-integer host oracle."""
import ctypes
import itertools
import math
from pathlib import Path
import random
import subprocess
import sys
import tempfile

TVC, LLC, LINK = sys.argv[1:4]
ROOT = Path(__file__).resolve().parents[2]
U32 = (1 << 32) - 1
I64 = (1 << 63) - 1
U64 = (1 << 64) - 1


def run(*args):
    result = subprocess.run([str(a) for a in args], capture_output=True, text=True, timeout=90)
    assert result.returncode == 0, (args, result.stdout, result.stderr)


def admitted(limits, geometry, threads, shared):
    grid, block = geometry[:3], geometry[3:6]
    return (all(0 < v <= U32 for v in grid + block)
            and geometry[6] <= U32
            and all(a <= b for a, b in zip(grid, limits[4:7]))
            and all(a <= b for a, b in zip(block, limits[1:4]))
            and math.prod(block) <= min(threads, limits[0])
            and geometry[6] + shared <= limits[7]
            and math.prod(grid + block) <= I64)


with tempfile.TemporaryDirectory() as directory:
    temp = Path(directory)
    source = temp / "geometry.tv"
    source.write_text(f'import "{ROOT / "src/lib/gpu/cuda_geometry.tv"}";\n'
                      f'import "{ROOT / "src/lib/gpu/thread.tv"}";\n'
                      '#[export] fn geometry_probe(l:*CudaDeviceLimits,g:*CudaGeometry,t:u64,s:u64)->i32 {\n'
                      ' return cuda_geometry_status(l,g[0],t,s);\n}\n'
                      '#[export] fn thread_probe(t:*GpuThread,out:*u64) {\n'
                      ' out[0]=gpu_global_x(t[0]); out[1]=gpu_global_y(t[0]); out[2]=gpu_global_z(t[0]);\n'
                      ' out[3]=gpu_global_size_x(t[0]); out[4]=gpu_global_size_y(t[0]);\n'
                      ' out[5]=gpu_global_size_z(t[0]); out[6]=gpu_local_index(t[0]);\n'
                      ' out[7]=gpu_block_index(t[0]); out[8]=gpu_global_index(t[0]);\n}\n')
    run(TVC, source, "-o", temp / "geometry.ll")
    run(LLC, "-relocation-model=pic", "-filetype=obj", temp / "geometry.ll", "-o", temp / "geometry.o")
    run(LINK, "-shared", temp / "geometry.o", "-o", temp / "geometry.so")
    library = ctypes.CDLL(str(temp / "geometry.so"))
    probe = library.geometry_probe
    probe.argtypes = [ctypes.POINTER(ctypes.c_uint64), ctypes.POINTER(ctypes.c_uint64),
                     ctypes.c_uint64, ctypes.c_uint64]
    probe.restype = ctypes.c_int32
    limits = [1024, 1024, 1024, 64, 2147483647, 65535, 65535, 49152, 32]
    cases = []
    for block in itertools.product((1, 2, 8, 16, 32, 64), repeat=3):
        cases.append((limits, [3, 5, 7, *block, 0], 1024, 0))
    bounds = [*limits[4:7], *limits[1:4], limits[7]]
    for axis, bound in enumerate(bounds):
        for value in (0, 1, bound - 1, bound, bound + 1, U32, U32 + 1, I64, U64):
            geometry = [1, 1, 1, 1, 1, 1, 0]
            geometry[axis] = value
            cases.append((limits, geometry, 1024, 0))
    for dynamic in (0, 1, 16384, 49151, 49152, 49153, U64):
        for static in (0, 1, 32768, 49152, 49153, U64):
            cases.append((limits, [1, 1, 1, 256, 1, 1, dynamic], 1024, static))
    for threads in (0, 1, 255, 256, 257, 1024, U64):
        cases.append((limits, [1, 1, 1, 256, 1, 1, 0], threads, 0))
    cases.append((limits, [2147483647, 65535, 65535, 1024, 1, 1, 0], 1024, 0))
    rng = random.Random(303)
    for _ in range(5000):
        custom = limits if rng.randrange(2) else [rng.choice((0, 1, 32, 1024, U32, U64)) for _ in range(9)]
        geometry = [rng.choice((0, 1, 2, 8, 16, 32, 64, 256, 65535, U32, U64)) for _ in range(7)]
        cases.append((custom, geometry, rng.choice((0, 256, 1024, U64)), rng.choice((0, 16384, U64))))
    accepted = 0
    for capability, geometry, threads, shared in cases:
        expected = admitted(capability, geometry, threads, shared)
        result = probe((ctypes.c_uint64 * 9)(*capability), (ctypes.c_uint64 * 7)(*geometry), threads, shared)
        assert (result == 0) == expected, (capability, geometry, threads, shared, result, expected)
        accepted += expected
    assert accepted > 100
    print(f"CUDA geometry PASS: {len(cases)} exact-integer cases, {accepted} admitted, axis/product/shared limits")

    thread_probe = library.thread_probe
    thread_probe.argtypes = [ctypes.POINTER(ctypes.c_uint64), ctypes.POINTER(ctypes.c_uint64)]
    thread_probe.restype = None

    def check_thread(tid, bid, block, grid):
        context = (ctypes.c_uint64 * 12)(*tid, *bid, *block, *grid)
        output = (ctypes.c_uint64 * 9)()
        thread_probe(context, output)
        point = [b * size + t for t, b, size in zip(tid, bid, block)]
        extent = [b * g for b, g in zip(block, grid)]

        def flatten(point, extent):
            return sum(value * math.prod(extent[:axis]) for axis, value in enumerate(point))

        expected = point + extent + [flatten(tid, block), flatten(bid, grid), flatten(point, extent)]
        assert list(output) == expected, (tid, bid, block, grid, list(output), expected)
        return output[8]

    total = 0
    for block, grid in [((7, 1, 1), (3, 1, 1)), ((3, 2, 1), (2, 3, 1)), ((3, 2, 2), (2, 3, 2))]:
        seen = set()
        for bid in itertools.product(*(range(n) for n in grid)):
            for tid in itertools.product(*(range(n) for n in block)):
                index = check_thread(tid, bid, block, grid)
                assert index not in seen
                seen.add(index)
                total += 1
        assert seen == set(range(math.prod(block + grid)))
    for _ in range(2000):
        block = rng.choice(((1024, 1, 1), (32, 8, 4), (8, 8, 16)))
        grid = (rng.choice((1, 65535, 2147483647)), rng.randrange(1, 33), rng.randrange(1, 17))
        tid = tuple(rng.randrange(n) for n in block)
        bid = tuple(rng.randrange(n) for n in grid)
        check_thread(tid, bid, block, grid)
        check_thread(tuple(n - 1 for n in block), tuple(n - 1 for n in grid), block, grid)
        total += 2
    print(f"GPU thread context PASS: {total} coordinate oracles, bijective 1D/2D/3D indexing, wide products")
