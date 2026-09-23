"""Verify cooperative context lowering, full-grid footprints, and resident launches."""
import copy
import ctypes
import importlib.util
import itertools
import json
import math
from pathlib import Path
import random
import re
import subprocess
import sys
import tempfile

TVC, LLC, OPT, LINK = sys.argv[1:5]
CUDA = sys.argv[5] if len(sys.argv) > 5 else None
SM = int(sys.argv[6]) if len(sys.argv) > 6 else 120
HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
spec = importlib.util.spec_from_file_location("package", ROOT / "tools/cuda_package.py")
package = importlib.util.module_from_spec(spec)
spec.loader.exec_module(package)
TRIPLE = re.search(r"Default target: (\S+)", subprocess.check_output([LLC, "--version"], text=True))[1]


def run(*args, code=0):
    result = subprocess.run([str(a) for a in args], capture_output=True, text=True, timeout=90)
    assert result.returncode == code, (args, result.returncode, result.stdout, result.stderr)
    return result


def library(text, path):
    path.write_text(text)
    run(OPT, "-passes=verify", "-disable-output", path)
    run(LLC, f"-mtriple={TRIPLE}", "-relocation-model=pic", "-filetype=obj", path, "-o", path.with_suffix(".o"))
    run(LINK, "-shared", path.with_suffix(".o"), "-o", path.with_suffix(".so"))
    return ctypes.CDLL(str(path.with_suffix(".so")))


with tempfile.TemporaryDirectory() as directory:
    temp = Path(directory)
    ir = temp / "cooperative.ll"
    run(TVC, "--emit-gpu-nvptx", HERE / "gpu_thread_context.tv", "-o", ir)
    text = ir.read_text()
    entries = package.descriptors(text)
    assert len(entries) == 2
    assert all(k["profile"] == "cooperative-grid-v1" and k["block"] is None for k in entries)
    assert all(k["execution"] == "cooperative-kernel" and k["index_bits"] == 64 for k in entries)
    assert all([p["source_type"] for p in k["parameters"]] == ["*u64", "*u64", "u64", "u64"] for k in entries)
    assert "alloca" not in text and "br i1" not in text
    assert not re.search(r"\bcall\b(?![^\n]*@llvm\.nvvm\.)", text)
    run(OPT, "-passes=verify", "-disable-output", ir)
    run(LLC, "-mcpu=sm_90", ir, "-o", temp / "cooperative.ptx")
    native = text.replace("define ptx_kernel", "define").replace("ptr addrspace(1)", "ptr")
    native = re.sub(r"addrspacecast ptr (%\w+) to ptr", r"getelementptr i8, ptr \1, i64 0", native)
    names = [f"{kind}.{axis}" for kind in ("tid", "ctaid", "ntid", "nctaid") for axis in "xyz"]
    for name in names:
        intrinsic = f"llvm.nvvm.read.ptx.sreg.{name}"
        var = "test_" + name.replace(".", "_")
        native = native.replace(f"declare i32 @{intrinsic}()", f"@{var} = global i32 0")
        native = native.replace(f"call i32 @{intrinsic}()", f"load i32, ptr @{var}")
    device = library(native, temp / "retargeted.ll")
    registers = [ctypes.c_uint32.in_dll(device, "test_" + name.replace(".", "_")) for name in names]
    run(TVC, HERE / "gpu_thread_context.tv", "-o", temp / "host.ll")
    host = library((temp / "host.ll").read_text(), temp / "host-shared.ll")
    cpu = host.context_cpu
    cpu.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_int32]
    run(TVC, HERE / "cuda_geometry_gate.tv", "--emit", "obj", "-llc", LLC, "-o", temp / "gate.o")
    run(LINK, "-no-pie", "-Wall", "-Wextra", "-Werror", temp / "gate.o", HERE / "cuda_driver_mock.c", "-o", temp / "mock")
    image = temp / "module.tvcp"
    package.build(ir, image, LLC, SM if CUDA else 90)
    if CUDA:
        run(LINK, "-no-pie", temp / "gate.o", CUDA, f"-Wl,-rpath,{Path(CUDA).parent}", "-o", temp / "cuda")
    rng = random.Random(312)
    mask = (1 << 64) - 1
    shapes = [((3, 1, 1), (7, 1, 1)), ((3, 2, 1), (8, 7, 1)),
              ((2, 3, 2), (4, 3, 2)), ((2, 1, 1), (1, 8, 8)), ((1, 1, 1), (32, 8, 4))]
    for grid, block in shapes:
        extent = tuple(a * b for a, b in zip(grid, block))
        count = math.prod(extent)
        values = [rng.getrandbits(64) for _ in range(count)]
        inp = (ctypes.c_uint64 * count)(*values)
        for entry in entries:
            mode = int(entry["owner"] == "extent_map")
            expected = [0] * count
            out = (ctypes.c_uint64 * count)()
            cpu_out = (ctypes.c_uint64 * count)()
            worker = getattr(device, entry["symbol"])
            worker.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint64, ctypes.c_uint64]
            for bid in itertools.product(*(range(n) for n in grid)):
                for tid in itertools.product(*(range(n) for n in block)):
                    fields = (*tid, *bid, *block, *grid)
                    for reg, value in zip(registers, fields):
                        reg.value = value
                    xyz = [b * size + t for b, size, t in zip(bid, block, tid)]
                    index = xyz[0] + extent[0] * (xyz[1] + extent[1] * xyz[2])
                    local = tid[0] + block[0] * (tid[1] + block[1] * tid[2])
                    group = bid[0] + grid[0] * (bid[1] + grid[1] * bid[2])
                    expected[index] = ((values[index] ^ (extent[0] + (extent[1] << 20) + (extent[2] << 40))) if mode
                                       else values[index] + xyz[0] + xyz[1] * 17 + xyz[2] * 257 + local * 65537 + group * 1048576) & mask
                    worker(inp, out, 0, count)
                    cpu((ctypes.c_uint64 * 12)(*fields), inp, cpu_out, mode)
            assert list(out) == list(cpu_out) == expected, (grid, block, entry["owner"])
            (temp / "input").write_bytes(bytes(inp))
            oracle = b"".join(v.to_bytes(8, "little") for v in expected)
            for gate in ("mock", "cuda") if CUDA else ("mock",):
                run(temp / gate, image, entry["owner"], temp / "input", temp / "output", *grid, *block)
                assert (temp / "output").read_bytes() == oracle, (gate, grid, block, entry["owner"])

    image_bytes = image.read_bytes()
    header_size = int.from_bytes(image_bytes[8:12], "little")
    manifest = json.loads(image_bytes[12:12 + header_size])
    for key, value in [("execution", "independent-kernel"), ("block", [256, 1, 1]),
                       ("index_bits", 32), ("dimensions", 1), ("dynamic_shared_bytes", 1),
                       ("line", 1 << 31), ("domain", {"min_lo": 0, "max_hi": 1 << 63, "empty": "no-launch"})]:
        bad = copy.deepcopy(entries[0]); bad[key] = value
        try:
            package.validate_kernel(bad)
        except (ValueError, TypeError):
            pass
        else:
            raise AssertionError((key, value))
        mutated = copy.deepcopy(manifest); mutated["kernels"][0] = bad
        encoded = json.dumps(mutated, separators=(",", ":")).encode()
        malformed = temp / "malformed.tvcp"
        malformed.write_bytes(image_bytes[:8] + len(encoded).to_bytes(4, "little") + encoded + image_bytes[12 + header_size:])
        result = run(temp / "mock", malformed, "coordinate_map", temp / "input", temp / "output", *grid, *block, code=1)
        assert result.stdout == "4\n2\n", result.stdout
    header = f'import "{ROOT / "src/lib/gpu/thread.tv"}";\n'
    effects = temp / "effects.tv"
    effects.write_text(header + 'fn extra(t:GpuThread,value:u64)->u64 {return gpu_global_index(t);}\n'
                       '#[kernel] fn effects(thread:GpuThread,input:*u64,output:*u64) {\n'
                       'input[extra(thread,input[gpu_global_index(thread)])]=1;\n'
                       'output[gpu_global_index(thread)]=2;}')
    run(TVC, "--emit-gpu-nvptx", effects, "-o", temp / "effects.ll")
    effect = package.descriptors((temp / "effects.ll").read_text())[0]
    assert effect["parameters"][0]["access"] == "read-write"
    failures = [
        "output[thread.thread_x] = input[gpu_global_index(thread)];",
        "output[gpu_global_index(thread)+1] = input[gpu_global_index(thread)];",
        "output[gpu_global_index(thread)] = input[thread.block_x];",
    ]
    for body in failures:
        bad = temp / "bad.tv"
        bad.write_text(header + '#[kernel] fn bad(thread:GpuThread,input:*u64,output:*u64) {' + body + '}\n'
                       'fn good(input:*u64,output:*u64) { for i in 0..2048 { output[i]=input[i]; } }')
        target = temp / "bad.ll"; target.write_text("previous artifact")
        run(TVC, "--emit-gpu-nvptx", bad, "-o", target, code=1)
        assert target.read_text() == "previous artifact"
    for backend in ("--emit-gpu", "--emit-gpu-agx", "--emit-gpu-vulkan"):
        target = temp / "bad.ll"; target.write_text("previous artifact")
        run(TVC, backend, HERE / "gpu_thread_context.tv", "-o", target, code=1)
        assert target.read_text() == "previous artifact"
    print("cooperative geometry PASS: typed context, CPU/Python/device parity, 1D/2D/3D resident mock, refusals")
    if CUDA:
        print(f"cooperative geometry CUDA PASS: native SM{SM}, two kernels, five shapes, offset views")
