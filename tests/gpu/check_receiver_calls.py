"""Check receivers, operators, closure snapshots, callbacks, and rejected escapes."""
import ctypes
import json
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
    result = subprocess.run([str(a) for a in args], capture_output=True, text=True, timeout=90)
    assert result.returncode == code, (args, result.returncode, result.stdout, result.stderr)
    return result


def library(text, path):
    path.write_text(text)
    run(OPT, "-passes=verify", "-disable-output", path)
    run(LLC, f"-mtriple={TRIPLE}", "-relocation-model=pic", "-filetype=obj", path, "-o", path.with_suffix(".o"))
    run(LINK, "-shared", path.with_suffix(".o"), "-o", path.with_suffix(".so"))
    return ctypes.CDLL(str(path.with_suffix(".so")))


def signed(value, bits):
    value &= (1 << bits) - 1
    return value - (1 << bits) if value >> (bits - 1) else value


def expected(value, owner):
    mask = (1 << 64) - 1
    if owner == "closure_snapshot_map":
        def snapshot(x):
            return (2 * (((x * 4) & mask) ^ 17) + (((x * 4 + 1) & mask) ^ 17)
                    + x + 1 + 99 * (x * 3 + 99))
        return snapshot(value) + snapshot((value + 2) & mask)
    if owner == "closure_guard_map":
        def choose(x):
            if x == 0:
                return 7 + signed(value, 16)
            quotient = 30 // abs(x)
            return (quotient if x > 0 else -quotient) + signed(value, 16)
        return choose(signed(value, 64)) + choose(signed(value, 16))
    if owner == "closure_inline_map":
        return value * 6 + 4 + signed(value, 8)
    if owner == "closure_unsigned_map":
        return (((value >> 5) ^ (value >> 7)) + ((((value + 3) & mask) >> 5) ^ (value >> 7))
                + (((value & 65535) >> 5) ^ (value >> 7)))
    low = signed(value, 16)
    score = (value * 5 + low) * 3 + value
    if owner == "receiver_branch_map":
        return 57 if value == 0 else score * 2 - 9
    add = value * 5 - (value + 7) + low + signed(signed(value, 64) >> 3, 16)
    return (score * 2 + add * 2 + value * 5 + low + low * 2 + value * 4 + low
            + value * 5 - signed(signed(value, 64) >> 3, 16) + low
            + value * 5 - ((signed(value, 64) >> 3) & 65535) + low
            + value * 5 * signed(value, 8) + 1)


with tempfile.TemporaryDirectory() as directory:
    temp = Path(directory)
    nv = temp / "receiver.ll"
    source = HERE / "gpu_receiver_calls.tv"
    run(TVC, "--emit-gpu-nvptx", source, "-o", nv)
    text = nv.read_text()
    descriptors = [json.loads(line.split(" ", 2)[2]) for line in text.splitlines()
                   if line.startswith("; traveler.kernel.v1 ")]
    assert {k["owner"] for k in descriptors} == {
        "receiver_map", "receiver_branch_map", "closure_snapshot_map",
        "closure_guard_map", "closure_inline_map", "closure_unsigned_map"}
    assert "alloca" not in text and "ptrtoint" not in text
    assert not re.search(r"\bcall\b(?![^\n]*@llvm\.nvvm\.)", text)
    for descriptor in descriptors:
        if descriptor["owner"].startswith("closure_"):
            body = re.search(r"define ptx_kernel void @" + re.escape(descriptor["symbol"]) +
                             r"\(.*?^\}", text, re.S | re.M)[0]
            loads = len(re.findall(r"\bload i64, ptr ", body))
            assert loads == (2 if descriptor["owner"] == "closure_snapshot_map" else 1), body
    run(OPT, "-passes=verify", "-disable-output", nv)
    run(LLC, "-mcpu=sm_90", nv, "-o", temp / "receiver.ptx")
    amd = temp / "receiver-amd.ll"
    run(TVC, "--emit-gpu", source, "-o", amd)
    run(OPT, "-passes=verify", "-disable-output", amd)
    run(LLC, "-mcpu=gfx1100", "-filetype=obj", amd, "-o", temp / "receiver-amd.o")
    native_text = text.replace("define ptx_kernel", "define").replace("ptr addrspace(1)", "ptr")
    native_text = re.sub(r"addrspacecast ptr (%\w+) to ptr", r"getelementptr i8, ptr \1, i64 0", native_text)
    for name in ("tid", "ctaid"):
        intrinsic = f"llvm.nvvm.read.ptx.sreg.{name}.x"
        native_text = native_text.replace(f"declare i32 @{intrinsic}()", f"@test_{name} = global i32 0")
        native_text = native_text.replace(f"call i32 @{intrinsic}()", f"load i32, ptr @test_{name}")
    device = library(native_text, temp / "native.ll")
    host_ir = temp / "host.ll"
    run(TVC, source, "-o", host_ir)
    host = library(host_ir.read_text(), temp / "host-shared.ll")
    tid = ctypes.c_int32.in_dll(device, "test_tid")
    ctaid = ctypes.c_int32.in_dll(device, "test_ctaid")
    rng = random.Random(310)
    mask = (1 << 64) - 1
    values = [0, 1, mask, 1 << 63, (1 << 63) - 1] + [rng.getrandbits(64) for _ in range(2043)]
    data = b"".join(value.to_bytes(8, "little") for value in values)
    inp = ctypes.create_string_buffer(data)
    out = ctypes.create_string_buffer(len(data))
    if CUDA:
        package = temp / "receiver.tvcp"
        run(sys.executable, HERE.parents[1] / "tools/cuda_package.py", nv, "--llc", LLC, "--sm", SM, "-o", package)
        gate = temp / "gate"
        run(TVC, HERE / "cuda_generic_gate.tv", "--emit", "obj", "-llc", LLC, "-o", temp / "gate.o")
        run(LINK, "-no-pie", temp / "gate.o", CUDA, f"-Wl,-rpath,{Path(CUDA).parent}", "-o", gate)
    for descriptor in descriptors:
        oracle = b"".join((expected(value, descriptor["owner"]) & mask).to_bytes(8, "little") for value in values)
        worker = getattr(device, descriptor["symbol"])
        worker.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_int32, ctypes.c_int32]
        for index in range(2304):
            tid.value, ctaid.value = index % 256, index // 256
            worker(inp, out, 0, 2048)
        assert out.raw == oracle, "expanded receiver differs from Python"
        ctypes.memset(out, 0, len(data))
        cpu = getattr(host, descriptor["owner"])
        cpu.argtypes = [ctypes.c_int32, ctypes.c_void_p, ctypes.c_void_p]
        for index in range(2048):
            cpu(index, inp, out)
        assert out.raw == oracle, "CPU receiver differs from Python"
        if CUDA:
            (temp / "input.bin").write_bytes(data)
            mode = "u" if descriptor["owner"] == "closure_unsigned_map" else "i"
            run(gate, package, descriptor["owner"], mode, temp / "input.bin", temp / "output.bin")
            assert (temp / "output.bin").read_bytes() == oracle, "CUDA receiver differs from Python"

    refusals = [
        "trait Write { fn change(self)->i64; } impl Write for Pair { "
        "fn change(self)->i64 { self.value=7; return self.value; } } "
        "fn helper(x:i64)->i64 { var p:Pair=Pair{value:x}; return p.change(); }",
        "fn mutate(p:*Pair)->i64 { p.value=7; return p.value; } "
        "fn forward(p:*Pair)->i64 { return mutate(p); } "
        "fn helper(x:i64)->i64 { var p:Pair=Pair{value:x}; return forward(&p); }",
        "fn mutate(p:Pair)->i64 { p.value=7; return p.value; } "
        "fn helper(x:i64)->i64 { let p:Pair=Pair{value:x}; return mutate(p); }",
        "fn helper(x:i64)->i64 { let p:Pair=Pair{value:x}; let alias:*Pair=&p; return alias.value; }",
        "fn helper(x:i64)->i64 { let p:Pair=Pair{value:x}; return (&p as u64) as i64; }",
        "fn escape(p:*Pair)->*Pair { return p; } "
        "fn helper(x:i64)->i64 { let p:Pair=Pair{value:x}; return escape(&p).value; }",
        "fn escape(p:Pair)->Pair { return p; } "
        "fn helper(x:i64)->i64 { let p:Pair=Pair{value:x}; return escape(p).value; }",
        "trait Add { fn add(self, other:Pair)->i64; } impl Add for Pair { "
        "fn add(self, other:Pair)->i64 { self.value=other.value; return self.value; } } "
        "fn helper(x:i64)->i64 { var p:Pair=Pair{value:x}; let q:Pair=Pair{value:9}; return p+q; }",
        "trait Read { fn read(self)->i64; } impl Read for Pair { "
        "fn read(self)->i64 { return Read__Pair__read(self); } } "
        "fn helper(x:i64)->i64 { let p:Pair=Pair{value:x}; return p.read(); }",
    ]
    bad = temp / "bad.tv"
    target = temp / "bad.ll"
    for body in refusals:
        bad.write_text("struct Pair { value:i64, } " + body +
                       " #[kernel] fn bad(i:i32,input:*i64,output:*i64) { output[i]=helper(input[i]); }"
                       " #[kernel] fn good(i:i32,input:*i64,output:*i64) { output[i]=input[i]; }")
        for backend in ("--emit-gpu-nvptx", "--emit-gpu"):
            target.write_text("previous artifact")
            result = run(TVC, backend, bad, "-o", target, code=1)
            assert "explicit kernel body failed typed device verification" in result.stderr, result.stderr
            assert target.read_text() == "previous artifact"

    closure_refusals = [
        "fn helper(x:i64)->i64 { var bias:i64=x; "
        "let f=|v:i64|->i64 { bias=v; return bias; }; return f(x); }",
        "let global:i64=7; fn helper(x:i64)->i64 { let f=|v:i64|->i64 v+global; return f(x); }",
        "fn helper(x:i64)->i64 { let p:Pair=Pair{value:x}; "
        "let f=|v:i64|->i64 v+p.value; return f(x); }",
        "fn helper(x:i64)->i64 { var a:[i64;2]=0; a[0]=x; "
        "let f=|v:i64|->i64 a[0]+v; return f(x); }",
        "fn helper(x:i64)->i64 { let wide:i128=x as i128; "
        "let f=|v:i64|->i64 v+(wide as i64); return f(x); }",
        "fn helper(x:i64)->i64 { let f=|v:i128|->i64 v as i64; return f(x as i128); }",
        "fn helper(x:i64)->i64 { let f=|v:i64|->i128 v as i128; return f(x) as i64; }",
        "fn helper(x:i64)->i64 { let g=|v:i64|->i64 v+1; "
        "let f=|v:i64|->i64 g(v); return f(x); }",
        "fn helper(x:i64)->i64 { let f=|v:i64|->i64 { "
        "let g=|w:i64|->i64 w+1; return g(v); }; return f(x); }",
        "fn generic<T>(x:T)->i64 { let f=|v:i64|->i64 v+1; return f(x as i64); } "
        "fn helper(x:i64)->i64 { return generic(x); }",
        "fn helper(x:i64)->i64 { let f=|v:i64|->i64 v+1; let alias=f; return alias(x); }",
    ]
    closure_refusals.append("fn helper(x:i64)->i64 { " +
                            " ".join(f"let f{i}=|v:i64|->i64 v+{i};" for i in range(17)) +
                            " return f16(x); }")
    closure_refusals.append("fn helper(x:i64)->i64 { let f=|" +
                            ",".join(f"p{i}:i64" for i in range(17)) +
                            "|->i64 p0; return f(" + ",".join(["x"] * 17) + "); }")
    closure_refusals.append("fn helper(x:i64)->i64 { " +
                            " ".join(f"let c{i}:i64=x+{i};" for i in range(17)) +
                            " let f=|v:i64|->i64 v+" + "+".join(f"c{i}" for i in range(17)) +
                            "; return f(x); }")
    for body in closure_refusals:
        bad.write_text("struct Pair { value:i64, } " + body +
                       " #[kernel] fn bad(i:i32,input:*i64,output:*i64) { output[i]=helper(input[i]); }"
                       " #[kernel] fn good(i:i32,input:*i64,output:*i64) { output[i]=input[i]; }")
        for backend in ("--emit-gpu-nvptx", "--emit-gpu"):
            target.write_text("previous artifact")
            run(TVC, backend, bad, "-o", target, code=1)
            assert target.read_text() == "previous artifact"

    bad.write_text("fn escape<C>(f:C)->C { return f; } "
                   "fn helper(x:i64)->i64 { let f=|v:i64|->i64 v+1; let g=escape(f); return g(x); } "
                   "#[kernel] fn bad(i:i32,input:*i64,output:*i64) { output[i]=helper(input[i]); }")
    for backend in ("--emit-gpu-nvptx", "--emit-gpu"):
        target.write_text("previous artifact")
        run(TVC, backend, bad, "-o", target, code=1)
        assert target.read_text() == "previous artifact"

print("device abstractions PASS: receivers, operators, closure snapshots/callbacks, CPU/Python parity, refusals")
if CUDA:
    print(f"resident abstractions CUDA PASS: native SM{SM}, {len(descriptors)} kernels, offset views")
