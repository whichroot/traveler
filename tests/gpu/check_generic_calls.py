"""Check private generic environments and scalar local bindings against oracles."""
import ctypes
import json
import os
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
TRIPLE = re.search(r"Default target: (\S+)", subprocess.check_output([LLC, "--version"], text=True))[1]


def run(*args, code=0):
    p = subprocess.run([str(a) for a in args], capture_output=True, text=True, timeout=90)
    assert p.returncode == code, (args, p.returncode, p.stdout, p.stderr)
    return p


def library(ir, path):
    path.write_text(ir)
    run(OPT, "-passes=verify", "-disable-output", path)
    obj = path.with_suffix(".o")
    run(LLC, f"-mtriple={TRIPLE}", "-relocation-model=pic", "-filetype=obj", path, "-o", obj)
    out = path.with_suffix(".so")
    run(LINK, "-shared", obj, "-o", out)
    return ctypes.CDLL(str(out))


def retarget(ir):
    ir = ir.replace("define ptx_kernel", "define").replace("ptr addrspace(1)", "ptr")
    ir = re.sub(r"addrspacecast ptr (%\w+) to ptr", r"getelementptr i8, ptr \1, i64 0", ir)
    for name in ("tid", "ctaid"):
        intrinsic = f"llvm.nvvm.read.ptx.sreg.{name}.x"
        ir = ir.replace(f"declare i32 @{intrinsic}()", f"@test_{name} = global i32 0")
        ir = ir.replace(f"call i32 @{intrinsic}()", f"load i32, ptr @test_{name}")
    return ir


def signed(x, bits):
    x &= (1 << bits) - 1
    return x - (1 << bits) if x >> (bits - 1) else x


def round_value(x, bits, is_signed):
    value = (x * 3) & ((1 << bits) - 1)
    shifted = (signed(value, bits) if is_signed else value) >> 5
    return ((value + 1) ^ shifted) & ((1 << bits) - 1)


def guarded(x):
    value = signed(x, 64)
    if value == 0:
        result = 19
    elif value == 1:
        result = 23
    else:
        denominator = signed(value - 1, 64)
        quotient = abs(value) // abs(denominator)
        if (value < 0) != (denominator < 0):
            quotient = -quotient
        result = quotient + round_value(x, 64, True)
    return (result + (10 if value == 0 else value + 1)) & ((1 << 64) - 1)


with tempfile.TemporaryDirectory() as directory:
    temp = Path(directory)
    os.environ["TRAVELER_THREADS"] = "4"
    source = HERE / "gpu_generic_calls.tv"
    nv = temp / "generic.ll"
    run(TVC, "--emit-gpu-nvptx", "--gpu-interface", source, "-o", nv)
    ir = nv.read_text()
    descriptors = [json.loads(line.split(" ", 2)[2]) for line in ir.splitlines()
                   if line.startswith("; traveler.kernel.v1 ")]
    assert {k["owner"] for k in descriptors} == {
        "generic_i64", "generic_u64", "generic_mixed", "generic_guarded",
        "explicit_guarded", "explicit_direct", "explicit_aggregate", "explicit_array", "explicit_static",
        "explicit_generic_direct", "explicit_generic_factored"}
    for descriptor in descriptors:
        explicit = descriptor["owner"].startswith("explicit_")
        assert descriptor["execution"] == ("independent-kernel" if explicit else "independent-pfor")
        assert descriptor["symbol"].startswith("__traveler_kernel_" if explicit else "__pfor_gpu_worker_")
    assert not re.search(r"\bcall\b(?![^\n]*@(?:llvm\.nvvm\.|llvm\.trap\(|__tv_checked_[0-3]_(?:1|8|16|32|64)\())", ir)
    assert "alloca" not in ir
    run(LLC, "-mcpu=sm_90", nv, "-o", temp / "generic.ptx")
    amd = temp / "generic-amd.ll"
    run(TVC, "--emit-gpu", source, "-o", amd)
    run(OPT, "-passes=verify", "-disable-output", amd)
    run(LLC, "-mcpu=gfx1100", "-filetype=obj", amd, "-o", temp / "generic-amd.o")
    native = library(retarget(ir), temp / "native.ll")
    host_path = temp / "host.ll"
    run(TVC, source, "-o", host_path)
    host = library(host_path.read_text(), temp / "host-shared.ll")
    if CUDA:
        package = temp / "generic.tvcp"
        run(sys.executable, HERE.parents[1] / "tools/cuda_package.py", nv,
            "--llc", LLC, "--sm", SM, "-o", package)
        gate = temp / "gate"
        gate_obj = temp / "gate.o"
        run(TVC, HERE / "cuda_generic_gate.tv", "--emit", "obj", "-llc", LLC, "-o", gate_obj)
        run(LINK, "-no-pie", gate_obj, CUDA, f"-Wl,-rpath,{Path(CUDA).parent}", "-o", gate)
    tid = ctypes.c_int32.in_dll(native, "test_tid")
    ctaid = ctypes.c_int32.in_dll(native, "test_ctaid")
    rng = random.Random(2026)
    mask = (1 << 64) - 1
    values = [0, 1, mask, 1 << 63, (1 << 63) - 1] + [rng.getrandbits(64) for _ in range(2043)]
    data = b"".join(x.to_bytes(8, "little") for x in values)
    inp = ctypes.create_string_buffer(data)
    output = ctypes.create_string_buffer(len(data))
    for descriptor in descriptors:
        is_signed = descriptor["owner"] == "generic_i64"
        expected = b"".join(((round_value(x, 64, is_signed) +
                             (signed(round_value(x, 16, True), 16) << 20)) & mask).to_bytes(8, "little") for x in values)
        if descriptor["owner"] == "generic_mixed":
            expected = b"".join(((signed(x, 16) +
                                 (((x * 3 + 1) & mask) ^ (signed(x, 64) >> 5))) & mask).to_bytes(8, "little") for x in values)
        if descriptor["owner"] in ("generic_guarded", "explicit_guarded"):
            expected = b"".join(guarded(x).to_bytes(8, "little") for x in values)
        if descriptor["owner"] == "explicit_direct":
            expected = b"".join((((x * 3 + 1) ^ (signed(x, 64) >> 5)) & mask).to_bytes(8, "little") for x in values)
        if descriptor["owner"] == "explicit_aggregate":
            expected = b"".join(((18 if x == 0 else x * 3 + signed(x, 16) - 5 + signed(x ^ 3, 16)) & mask).to_bytes(8, "little") for x in values)
        if descriptor["owner"] == "explicit_array":
            expected = b"".join(((x * 3) & mask).to_bytes(8, "little") for x in values)
        if descriptor["owner"] == "explicit_static":
            expected = b"".join((((x * 5) ^ 17) & mask).to_bytes(8, "little") for x in values)
        if descriptor["owner"] in ("explicit_generic_direct", "explicit_generic_factored"):
            expected = b"".join(round_value(x, 64, True).to_bytes(8, "little") for x in values)
        worker = getattr(native, descriptor["symbol"])
        worker.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_int32, ctypes.c_int32]
        for i in range(2304):
            tid.value, ctaid.value = i % 256, i // 256
            worker(inp, output, 0, 2048)
        assert output.raw == expected, "expanded specialization differs from Python"
        ctypes.memset(output, 0, len(data))
        fn = getattr(host, descriptor["owner"])
        if descriptor["execution"] == "independent-kernel":
            fn.argtypes = [ctypes.c_int32, ctypes.c_void_p, ctypes.c_void_p]
            for i in range(2048):
                fn(i, inp, output)
        else:
            fn.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
            fn(inp, output)
        assert output.raw == expected, "host specialization differs from Python"
        if CUDA:
            input_file = temp / "input.bin"
            output_file = temp / "output.bin"
            input_file.write_bytes(data)
            run(gate, package, descriptor["owner"],
                "u" if descriptor["owner"] == "generic_u64" else "i", input_file, output_file)
            assert output_file.read_bytes() == expected, "CUDA specialization differs from Python"

    entry = temp / "entry.tv"
    entry.write_text("#[kernel] fn only(i:i32,input:*i64,output:*i64) { output[i]=input[i]+1; }")
    only = temp / "only.ll"
    run(TVC, "--emit-gpu-nvptx", entry, "-o", only)
    assert '"execution":"independent-kernel"' in only.read_text()
    run(OPT, "-passes=verify", "-disable-output", only)
    for backend in ("--emit-gpu-agx", "--emit-gpu-vulkan"):
        only.write_text("previous artifact")
        run(TVC, backend, entry, "-o", only, code=1)
        assert only.read_text() == "previous artifact"
    bad_entries = [
        "#[kernel] fn bad(i:i64,input:*i64,output:*i64) { output[i]=input[i]; }",
        "#[kernel] fn bad<T>(i:i32,input:*T,output:*T) { output[i]=input[i]; }",
        "#[kernel] fn bad(i:i32,input:*i64,output:*i64) { output[i+1]=input[i]; }",
        "#[kernel] fn bad(i:i32,input:*i64,output:*i64) { output[i]=input[i+1]; }",
        "#[kernel] fn bad(i:i32,input:*i64,output:*i64) { output[i]=helper(input[i]); } "
        "fn helper(x:i64)->i64 { return x ** 3; }",
        "#[kernel(extra)] fn bad(i:i32,input:*i64,output:*i64) { output[i]=input[i]; }",
        "#[kernel] struct NotAFunction { value:i64, }",
    ]
    aggregate_refusals = [
        "fn helper(x:i64)->i64 { var a:[i64;4]=0; return a[x & 3]; }",
        "fn helper(x:i64)->i64 { var a:[i64;4]=0; a[x & 3]=x; return a[0]; }",
        "fn helper(x:i64)->i64 { var a:[i64;4]=0; return a[4]; }",
        "fn helper(x:i64)->i64 { var a:[i64;17]=0; return a[0]; }",
        "fn helper(x:i64)->i64 { var a:[i64;4]=1; return a[0]; }",
        "struct Pair { value:i64, } fn helper(x:i64)->i64 { "
        "var a:Pair=Pair{value:x}; var b:Pair=a; b.value=7; return a.value; }",
        "struct Pair { ptr:*i64, } fn helper(x:i64)->i64 { "
        "let a:Pair=Pair{ptr:null}; return x; }",
        "struct Inner { value:i64, } struct Outer { inner:Inner, } "
        "fn helper(x:i64)->i64 { let a:Outer=Outer{inner:Inner{value:x}}; return a.inner.value; }",
        "let table:*i64=null; fn helper(x:i64)->i64 { return table[0]+x; }",
        "var state:i64=0; fn helper(x:i64)->i64 { state=x; return x; }",
    ]
    for helper in aggregate_refusals:
        bad_entries.append(helper + " #[kernel] fn bad(i:i32,input:*i64,output:*i64) { output[i]=helper(input[i]); }")
    for bad in bad_entries:
        entry.write_text(bad + "\nfn good(input:*i64,output:*i64) { for i in 0..2048 { output[i]=input[i]; } }")
        for backend in ("--emit-gpu-nvptx", "--emit-gpu"):
            only.write_text("previous artifact")
            run(TVC, backend, entry, "-o", only, code=1)
            assert only.read_text() == "previous artifact", "invalid explicit entry published a partial module"
    entry.write_text("#[kernel]")
    run(TVC, "--emit-gpu-nvptx", entry, "-o", only, code=1)
    assert only.read_text() == "previous artifact"

print("generic device calls PASS: nested environments, lazy branches, explicit entries, CPU/Python parity, NVPTX/AMDGCN lowering")
if CUDA:
    print(f"generic resident CUDA PASS: native SM{SM}, {len(descriptors)} kernels, offset views, Python parity")
