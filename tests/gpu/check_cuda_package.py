"""Cross-check the Traveler package loader and SHA-256 against Python oracles."""
import copy
import hashlib
import importlib.util
import json
from pathlib import Path
import random
import struct
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[2]
TVC, LLC, LINK = sys.argv[1:4]
spec = importlib.util.spec_from_file_location("cuda_package", ROOT / "tools/cuda_package.py")
package = importlib.util.module_from_spec(spec)
spec.loader.exec_module(package)


def run(*args):
    return subprocess.run([str(a) for a in args], capture_output=True)


def compile(source, target):
    r = run(TVC, source, "--emit", "exe", "-llc", LLC, "-cc", LINK, "-o", target)
    assert r.returncode == 0, r.stderr.decode()


def encode(manifest, ptx):
    header = json.dumps(manifest, separators=(",", ":")).encode()
    return package.MAGIC + struct.pack("<I", len(header)) + header + ptx


with tempfile.TemporaryDirectory() as directory:
    temp = Path(directory)
    gate = temp / "gate"
    compile(ROOT / "tests/gpu/cuda_package_gate.tv", gate)
    sha = temp / "sha"
    compile(ROOT / "tests/gpu/sha256_gate.tv", sha)
    rng = random.Random(314159)
    for data in [b"", b"abc", b"a" * 1000000] + [rng.randbytes(n) for n in (1, 55, 56, 57, 63, 64, 65, 119, 120, 127, 128, 4097)]:
        path = temp / "input"
        path.write_bytes(data)
        result = run(sha, path)
        assert result.returncode == 0, result
        assert bytes(map(int, result.stdout.split())) == hashlib.sha256(data).digest()

    ir = temp / "kernel.ll"
    result = run(TVC, "--emit-gpu-nvptx", "--gpu-interface", ROOT / "tests/gpu/gpu_scalar_calls.tv", "-o", ir)
    assert result.returncode == 0, result.stderr
    output = temp / "kernel.tvcp"
    package.build(ir, output, LLC, 90)
    result = run(gate, output)
    assert result.returncode == 0 and result.stdout == b"90\n1\n", result
    good = output.read_bytes()
    length = struct.unpack("<I", good[8:12])[0]
    manifest = json.loads(good[12:12 + length])
    ptx = good[12 + length:]
    invalid = [b"", good[:11], good[:-1], good + b"x", b"BADMAGIC" + good[8:],
               good[:8] + b"\xff" * 4 + good[12:]]

    def mutation(edit):
        changed = copy.deepcopy(manifest)
        edit(changed)
        invalid.append(encode(changed, ptx))

    mutation(lambda m: m.update(schema="traveler.cuda.package.v2"))
    mutation(lambda m: m.update(sm=120))
    mutation(lambda m: m.update(ptx_minor=9))
    mutation(lambda m: m.update(ptx_bytes=2**63))
    mutation(lambda m: m.update(kernels=[]))
    mutation(lambda m: m["kernels"].append(copy.deepcopy(m["kernels"][0])))
    for key, value in [("schema", "traveler.kernel.v2"), ("abi", True), ("block", [128, 1, 1]),
                       ("disjoint", []), ("parameters", []), ("artifact", {}), ("owner", ""),
                       ("specialization", [{}]), ("symbol", "missing")]:
        mutation(lambda m, key=key, value=value: m["kernels"][0].update({key: value}))
    for key, value in [("size", 4), ("alignment", 1), ("ordinal", 1), ("address_space", 0),
                       ("element_alignment", 1), ("access", "none"), ("synthetic", True),
                       ("source_type", "*i32"), ("footprint", None)]:
        mutation(lambda m, key=key, value=value: m["kernels"][0]["parameters"][0].update({key: value}))
    header = good[12:12 + length]
    for altered in [header.replace(b'"abi":1', b'"abi":1,"abi":1', 1),
                    header.replace(b'"abi":1', b'"abi":01', 1),
                    b'[' * 10000 + b']' * 10000,
                    header.replace(b'"abi":1', b'"abi":18446744073709551617', 1)]:
        invalid.append(package.MAGIC + struct.pack("<I", len(altered)) + altered + ptx)
    for data in invalid:
        bad = temp / "bad.tvcp"
        bad.write_bytes(data)
        result = run(gate, bad)
        assert result.returncode == 2 and not result.stdout, result
    missing = run(gate, temp / "missing.tvcp")
    assert missing.returncode == 1

    original = ir.read_text()
    for text in [original.replace('"symbol":', '"unknown_symbol":'),
                 original.replace('"abi":1', '"abi":2'),
                 original.replace('"ordinal":0', '"ordinal":1'),
                 original.replace('"llvm_type":"i32"', '"llvm_type":"i16"')]:
        ir.write_text(text)
        try:
            package.build(ir, output, LLC, 90)
            raise AssertionError("invalid descriptor published")
        except ValueError:
            pass
        assert output.read_bytes() == good
        assert not list(temp.glob("*.tmp"))

print("CUDA package PASS: SHA-256 oracles, PTX binding, bounded schemas, refusals, atomic publication")
