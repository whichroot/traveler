"""Check resident resources with a driver double and an optional real CUDA lane."""
import importlib.util
from pathlib import Path
import random
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[2]
TVC, LLC, LINK = sys.argv[1:4]
spec = importlib.util.spec_from_file_location("cuda_package", ROOT / "tools/cuda_package.py")
package = importlib.util.module_from_spec(spec)
spec.loader.exec_module(package)


def run(*args):
    result = subprocess.run([str(a) for a in args], capture_output=True, text=True)
    assert result.returncode == 0, (args, result.returncode, result.stdout, result.stderr)
    return result.stdout


with tempfile.TemporaryDirectory() as directory:
    temp = Path(directory)
    ir = temp / "kernels.ll"
    run(TVC, "--emit-gpu-nvptx", "--gpu-interface", ROOT / "tests/gpu/cuda_resident_kernels.tv", "-o", ir)
    descriptors = package.descriptors(ir.read_text())
    assert [[p["name"] for p in k["parameters"]] for k in descriptors] == [
        ["input", "scale", "middle", "lo", "hi"],
        ["accumulated", "middle", "bias", "output", "lo", "hi"]]
    image = temp / "pipeline.tvcp"
    package.build(ir, image, LLC, 90)
    host = temp / "host.ll"
    obj = temp / "host.o"
    binary = temp / "gate"
    run(TVC, ROOT / "tests/gpu/cuda_resident_faults.tv", "-o", host)
    run(LLC, "-filetype=obj", host, "-o", obj)
    run(LINK, "-no-pie", "-Wall", "-Wextra", "-Werror", obj,
        ROOT / "tests/gpu/cuda_driver_mock.c", "-o", binary)
    result = run(binary, image)
    assert result == "120\nresident pipeline PASS\nresident faults PASS\n", result
    print("CUDA resident mock PASS: resident pipeline, no implicit copies, lifetime/range checks, driver failures, context restoration")

    if len(sys.argv) > 4:
        cuda = sys.argv[4]
        sm = int(sys.argv[5])
        assert sm >= 50
        package.build(ir, image, LLC, sm)
        run(TVC, ROOT / "tests/gpu/cuda_resident_gate.tv", "-o", host)
        run(LLC, "-filetype=obj", host, "-o", obj)
        run(LINK, "-no-pie", obj, cuda, "-Wl,-rpath," + str(Path(cuda).parent), "-o", binary)
        result = run(binary, image)
        assert result == f"{sm}\nresident pipeline PASS\n", result
        print(f"CUDA resident hardware PASS: native sm_{sm} package and actual device capability agree")
        run(TVC, ROOT / "tests/gpu/cuda_resident_wide.tv", "-o", host)
        run(LLC, "-filetype=obj", host, "-o", obj)
        run(LINK, "-no-pie", obj, cuda, "-Wl,-rpath," + str(Path(cuda).parent), "-o", binary)
        rng = random.Random(512)
        mask = (1 << 512) - 1
        mask256 = (1 << 256) - 1
        values = [0, 1, (1 << 255) - 1, 1 << 255, mask256, 1 << 256,
                  (1 << 511) - 1, 1 << 511, mask] + [rng.getrandbits(512) for _ in range(257)]
        data = temp / "wide.in"
        result_path = temp / "wide.out"
        data.write_bytes(b"".join(x.to_bytes(64, "little") for x in values))
        for signed in (True, False):
            fixture = "gpu_i512_calls.tv" if signed else "gpu_u512_calls.tv"
            run(TVC, "--emit-gpu-nvptx", "--gpu-interface", ROOT / "tests/gpu" / fixture, "-o", ir)
            package.build(ir, image, LLC, sm)
            run(binary, image, "i" if signed else "u", data, result_path)
            expected = []
            for value in values:
                high, low = value >> 256, value & mask256
                if signed:
                    high = high - (1 << 256) if high >> 255 else high
                    low = low - (1 << 256) if low >> 255 else low
                expected.append(((value + high * low + int(signed)) & mask).to_bytes(64, "little"))
            assert result_path.read_bytes() == b"".join(expected), fixture
        print("CUDA resident wide PASS: signed/unsigned 512-bit arithmetic matches Python on hardware")
        boolean = ROOT / "tests/gpu/cuda_boolean_kernel.tv"
        run(TVC, "--emit-gpu-nvptx", "--gpu-interface", boolean, "-o", ir)
        package.build(ir, image, LLC, sm)
        data.write_bytes(b"".join(x.to_bytes(4, "little", signed=True) for x in range(-200, 201)))
        run(binary, image, "b", data, result_path)
        assert result_path.read_bytes() == b"".join((x + 1).to_bytes(4, "little", signed=True) for x in range(-200, 201))
        print("CUDA resident boolean PASS: one-byte boolean capture ABI on hardware")
