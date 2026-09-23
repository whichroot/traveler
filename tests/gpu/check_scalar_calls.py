"""Check typed helper expansion, native-body oracles, and device artifacts."""
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
HERE = Path(__file__).resolve().parent
TRIPLE = subprocess.check_output([LLC, "--version"], text=True)
TRIPLE = re.search(r"Default target: (\S+)", TRIPLE).group(1)


def run(*args, code=0):
    result = subprocess.run(args, capture_output=True, text=True, timeout=60)
    assert result.returncode == code, (args, result.returncode, result.stdout, result.stderr)
    return result


def signed(value, bits):
    value &= (1 << bits) - 1
    return value - (1 << bits) if value >> (bits - 1) else value


def shared(ir, name, temp):
    path = temp / f"{name}.ll"
    path.write_text(ir)
    run(OPT, "-passes=verify", "-disable-output", str(path))
    obj = temp / f"{name}.o"
    run(LLC, f"-mtriple={TRIPLE}", "-relocation-model=pic", "-filetype=obj", str(path), "-o", str(obj))
    library = temp / f"{name}.so"
    run(LINK, "-shared", str(obj), "-o", str(library))
    return ctypes.CDLL(str(library))


def native_device_body(ir, name, temp):
    ir = re.sub(r'target triple = "[^"]+"', f'target triple = "{TRIPLE}"', ir)
    ir = ir.replace("define ptx_kernel", "define")
    for register in ("tid", "ctaid"):
        intrinsic = f"llvm.nvvm.read.ptx.sreg.{register}.x"
        ir = ir.replace(f"declare i32 @{intrinsic}()", f"@test_{register} = global i32 0")
        ir = ir.replace(f"call i32 @{intrinsic}()", f"load i32, ptr @test_{register}")
    ir = re.sub(r"addrspacecast ptr addrspace\(1\) (%\w+) to ptr", r"getelementptr i8, ptr \1, i64 0", ir)
    ir = ir.replace("ptr addrspace(1)", "ptr")
    return shared(ir, name, temp)


def check_execution(source, bits, oracle, owner, temp):
    mask = (1 << bits) - 1
    rng = random.Random(bits)
    values = [0, 1, mask, 1 << (bits - 1), (1 << (bits - 1)) - 1]
    values += [(1 << k) - 1 for k in range(64, bits, 64)]
    values += [rng.getrandbits(bits) for _ in range(2048 - len(values))]
    width = bits // 8
    data = b"".join(x.to_bytes(width, "little") for x in values)
    inp = ctypes.create_string_buffer(data)
    output = ctypes.create_string_buffer(len(data))
    expected = b"".join((oracle(x) & mask).to_bytes(width, "little") for x in values)
    nv = temp / f"{owner}-nv.ll"
    result = run(TVC, "--emit-gpu-nvptx", str(source), "-o", str(nv))
    assert '"status":"emitted"' in result.stderr
    ir = nv.read_text()
    assert not re.search(r"\bcall\b(?![^\n]*@llvm\.nvvm\.)", ir), ir
    assert "alloca" not in ir.replace("registers-only", "")
    if owner == "scalar_calls":
        assert len(re.findall(r" = load i64,", ir)) == 1, ir
        assert "sdiv i64" in ir, "unused argument evaluation disappeared"
    elif bits == 512:
        assert "mul i512" in ir and "add i512" in ir
        assert ("zext i256" if owner == "unsigned_wide_calls" else "sext i256") in ir
    run(OPT, "-passes=verify", "-disable-output", str(nv))
    ptx = temp / f"{owner}.ptx"
    run(LLC, "-mcpu=sm_90", str(nv), "-o", str(ptx))
    assert not re.search(r"\.extern\s+\.func|\bcall(?:\.uni)?\b", ptx.read_text())
    amd = temp / f"{owner}-amd.ll"
    run(TVC, "--emit-gpu", str(source), "-o", str(amd))
    run(OPT, "-passes=verify", "-disable-output", str(amd))
    amd_obj = temp / f"{owner}-amd.o"
    run(LLC, "-mcpu=gfx1100", "-filetype=obj", str(amd), "-o", str(amd_obj))
    nm = str(Path(LLC).with_name("llvm-nm"))
    assert not run(nm, "--undefined-only", str(amd_obj)).stdout.strip()

    native = native_device_body(ir, owner, temp)
    kernel = native.__pfor_gpu_worker_0
    kernel.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_int32, ctypes.c_int32]
    kernel.restype = None
    tid = ctypes.c_int32.in_dll(native, "test_tid")
    ctaid = ctypes.c_int32.in_dll(native, "test_ctaid")
    for index in range(2048 + 256):
        tid.value, ctaid.value = index % 256, index // 256
        kernel(inp, output, 0, 2048)
    assert output.raw == expected, f"expanded {bits}-bit body differs from Python oracle"
    ctypes.memset(output, 0, len(data))
    for index in range(2048):
        tid.value, ctaid.value = index % 256, index // 256
        kernel(inp, output, 7, 2045)
    assert output.raw == bytes(7 * width) + expected[7 * width:2045 * width] + bytes(3 * width)

    host = temp / f"{owner}-cpu.ll"
    run(TVC, str(source), "-o", str(host))
    cpu = shared(host.read_text(), f"{owner}-cpu", temp)
    function = getattr(cpu, owner)
    function.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    function.restype = None
    function(inp, output)
    assert output.raw == expected, f"CPU {bits}-bit body differs from Python oracle"


def refused(source, temp, detail=None, candidate=True):
    for mode in ("--emit-gpu-nvptx", "--emit-gpu"):
        out = temp / "refused.ll"
        out.write_text("previous artifact\n")
        result = run(TVC, mode, str(source), "-o", str(out), code=1)
        assert out.read_text() == "previous artifact\n"
        if candidate:
            assert '"reason":"uncarried-call"' in result.stderr, (source.name, result.stderr)
        else:
            assert '"reason":"no-usable-workers"' in result.stderr
        if detail:
            assert f"device-call-refused: {detail}" in result.stderr, result.stderr


with tempfile.TemporaryDirectory() as directory:
    temp = Path(directory)
    os.environ["TRAVELER_THREADS"] = "4"
    check_execution(HERE / "gpu_scalar_calls.tv", 64,
                    lambda x: 2 * (signed(x, 64) * 3 + 1) ^ (signed(x, 64) >> 5),
                    "scalar_calls", temp)
    check_execution(HERE / "gpu_i512_calls.tv", 512,
                    lambda x: x + signed(x >> 256, 256) * signed(x, 256) + 1,
                    "wide_calls", temp)
    check_execution(HERE / "gpu_u512_calls.tv", 512,
                    lambda x: x + (x >> 256) * (x & ((1 << 256) - 1)),
                    "unsigned_wide_calls", temp)
    for ty in ("i8", "u16", "i32", "u32", "u64", "i128", "u256"):
        bits = int(ty[1:])
        source = temp / f"{ty}.tv"
        source.write_text(
            f"fn helper(x:{ty})->{ty} {{ return ((~x) ^ (x << 1)) + ((x < 0) as {ty}); }}\n"
            f"#[export] fn typed_calls_{ty}(input:*{ty},output:*{ty}) {{ "
            "for i in 0..2048 { output[i] = helper(input[i]); } }\n")
        check_execution(source, bits,
                        lambda x, b=bits, t=ty: ((~x) ^ (x << 1)) + int(t[0] == "i" and signed(x, b) < 0),
                        f"typed_calls_{ty}", temp)

    kernel = "\nfn work(a: *i64, b: *i64) { for i in 0..2048 { b[i] = helper(a[i]); } }\n"
    cases = {
        "conditional": "fn helper(x:i64)->i64 { if x < 0 { return 0; } return x; }",
        "bounded_generic": "fn helper<T: Any>(x:T)->T { return x; }",
        "hidden_read": "let table: *i64 = null; fn helper(x:i64)->i64 { return table[x]; }",
        "short_circuit": "fn helper(x:i64)->i64 { return ((x != 0) && (10 / x > 0)) as i64; }",
    }
    for name, helper in cases.items():
        source = temp / f"{name}.tv"
        source.write_text(helper + kernel)
        refused(source, temp, candidate=name != "hidden_read")

    explosive = temp / "explosive.tv"
    declarations = ["fn e0(x:i64)->i64 { return x+1; }"]
    for i in range(1, 13):
        declarations.append(f"fn e{i}(x:i64)->i64 {{ return e{i-1}(x)+e{i-1}(x); }}")
    declarations.append("fn helper(x:i64)->i64 { return e12(x); }")
    explosive.write_text("\n".join(declarations) + kernel)
    refused(explosive, temp, "expansion-limit")
    mixed = temp / "mixed.tv"
    mixed.write_text(explosive.read_text() + (HERE / "gpu_scalar_calls.tv").read_text())
    for mode in ("--emit-gpu-nvptx", "--emit-gpu"):
        result = run(TVC, mode, str(mixed), "-o", str(temp / "mixed.ll"))
        decisions = [json.loads(line) for line in result.stderr.splitlines() if line.startswith("{")]
        assert decisions[-1]["emitted"] == 1 and decisions[-1]["refused"] == 1
        run(OPT, "-passes=verify", "-disable-output", str(temp / "mixed.ll"))

    printing = temp / "print512.tv"
    printing.write_text("fn main() { let one:u512=1; print((one << 511) - 1); let neg:i512=0-1; print(neg); }\n")
    printed_ir = temp / "print512.ll"
    run(TVC, str(printing), "-o", str(printed_ir))
    obj = temp / "print512.o"
    run(LLC, f"-mtriple={TRIPLE}", "-filetype=obj", str(printed_ir), "-o", str(obj))
    exe = temp / "print512"
    flags = ["-no-pie"] if sys.platform.startswith("linux") else []
    run(LINK, *flags, str(obj), "-o", str(exe))
    assert run(str(exe)).stdout == f"{(1 << 511) - 1}\n-1\n"
    result = run(TVC, "--eval", str(printing), code=97)
    assert "512-bit-eval" in result.stderr

print("scalar device calls PASS: typed expansion, 512-bit oracle, PTX/AMD closure, refusals")
