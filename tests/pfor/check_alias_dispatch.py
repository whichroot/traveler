"""Check dispatch branches, real worker entry, and serial overlap semantics."""
import ctypes as C
import json
import os
from pathlib import Path
import platform
import re
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[2]
TVC, LLC, LINK = sys.argv[1:4]
SOURCE = Path(__file__).with_name("pfor_alias_projection.tv")
os.environ["TRAVELER_THREADS"] = "4"


def run(*args):
    return subprocess.check_output(list(map(str, args)), text=True)


report = [json.loads(line) for line in run(TVC, SOURCE, "--pfor-alias-report").splitlines()]
by_fn = {r["fn"]: r for r in report if r["admitted"]}
assert all(r["reason"] for r in report if not r["admitted"])
for name in ["blocked", "rectangular"]:
    assert by_fn[name]["alias"] == "checked", by_fn[name]
assert by_fn["opaque"]["reason"] == "callee-footprint", by_fn
assert by_fn["wrapped"]["alias"] == "serial", by_fn
assert by_fn["shadowed"]["alias"] == "serial", by_fn
dynamic = [json.loads(line) for line in
           run(TVC, ROOT / "examples/rns_dyn_matmul.tv", "--pfor-alias-report").splitlines()]
assert any(r["fn"] == "rns_mac_dyn_dyn" and r["alias"] == "checked" for r in dynamic), dynamic

HOOKS = """
@probe_entries = global i32 0
@probe_parallel = global i32 0
define void @probe_entry() {
entry:
  %old = atomicrmw add ptr @probe_entries, i32 1 monotonic
  ret void
}
define void @probe_serial(ptr %ctx, i32 %lo, i32 %hi) {
entry:
  call void @probe_entry()
  ret void
}
define void @probe_dispatch(ptr %fn, ptr %ctx, i32 %lo, i32 %hi) {
entry:
  %old = atomicrmw add ptr @probe_parallel, i32 1 monotonic
  ret void
}
"""

with tempfile.TemporaryDirectory() as directory:
    temp = Path(directory)
    original = SOURCE.read_text()
    for name, changed in [
        ("callee-offset", original.replace("w[k * N + j]", "w[k * N + j + 1]")),
        ("callee-step", original.replace("k = k + 1;", "k = k + 2;")),
        ("callee-width", original.replace("fn rectangular(x: *i64, w: *i64", "fn rectangular(x: *i32, w: *i32")),
    ]:
        assert changed != original
        path = temp / f"{name}.tv"
        path.write_text(changed)
        probe = subprocess.run([TVC, str(path), "--pfor-alias-report"], capture_output=True, text=True)
        if probe.returncode:
            assert name == "callee-width" and "error" in probe.stderr, probe.stderr
        else:
            rows = [json.loads(line) for line in probe.stdout.splitlines()]
            row = next(r for r in rows if r["fn"] == "rectangular")
            assert row["alias"] in ["serial", "not-admitted"] and row["reason"], (name, row)
    run(TVC, SOURCE, "-o", temp / "source.ll")
    ir = (temp / "source.ll").read_text()
    real = re.sub(r"(define internal void @__pfor_worker_\d+\([^\n]+\) \{\nentry:\n)",
                  r"\1  call void @probe_entry()\n", ir)
    assert real != ir
    guards = re.sub(r"call void @__pfor_worker_\d+\(", "call void @probe_serial(", ir)
    guards = guards.replace("call void @__parallel_for(", "call void @probe_dispatch(")
    libraries = []
    for name, text in [("real", real), ("guards", guards)]:
        (temp / f"{name}.ll").write_text(text + HOOKS)
        target = []
        if platform.system() == "Linux":
            target = [f"-mtriple={platform.machine()}-linux-gnu"]
        run(LLC, "-O2", *target, "-relocation-model=pic", "-filetype=obj",
            temp / f"{name}.ll", "-o", temp / f"{name}.o")
        shared = "-dynamiclib" if platform.system() == "Darwin" else "-shared"
        run(LINK, shared, temp / f"{name}.o", "-o", temp / f"{name}.so")
        lib = C.CDLL(str(temp / f"{name}.so"))
        lib.blocked.argtypes = [C.c_void_p] * 3
        for fn in [lib.rectangular, lib.opaque]:
            fn.argtypes = [C.c_void_p] * 3 + [C.c_int] * 3
            fn.restype = None
        lib.blocked.restype = None
        libraries.append(lib)
    real, guards = libraries
    entries = C.c_int.in_dll(real, "probe_entries")
    parallel = C.c_int.in_dll(guards, "probe_parallel")

    def gate(expected, fn, *args):
        parallel.value = 0
        fn(*args)
        assert parallel.value == expected, (expected, parallel.value, args)

    gate(1, guards.blocked, 0x100000, 0x300000, 0x400000)
    gate(0, guards.blocked, 0x100000, 0x300000, 0x100008)
    gate(0, guards.blocked, 0x100000, 0x300000, 0x300008)
    gate(1, guards.blocked, 0x100000, 0x300000, 0x200000)
    gate(0, guards.blocked, 2**64 - 1024, 0x300000, 0x400000)
    gate(1, guards.rectangular, 0x100000, 0x200000, 0x400000, 4, 1024, 2048)
    gate(0, guards.rectangular, 0x100000, 0x200000, 0x200008, 4, 1024, 2048)
    gate(0, guards.rectangular, 0x100000, 0x200000, 0x100008, 4, 1024, 2048)
    gate(1, guards.rectangular, 0x100000, 0x100000, 0x400000, 4, 1024, 2048)
    gate(0, guards.rectangular, 0x100000, 0x100000, 0x100040, 4, 1024, 2048)
    for k, n, total in [(4, 0, 1024), (-1, 1024, 1024), (4, -1, 1024),
                        (2147483647, 2, 1024), (4, 1, 2147483647)]:
        gate(0, guards.rectangular, 0x100000, 0x200000, 0x400000, k, n, total)
    gate(0, guards.rectangular, 2**64 - 8, 0x200000, 0x400000, 4, 1024, 2048)
    gate(0, guards.opaque, 0x100000, 0x200000, 0x400000, 4, 1024, 2048)

    weights = (C.c_int64 * (1024 * 128 + 1024))(*[i % 5 for i in range(1024 * 128 + 1024)])
    x = (C.c_int64 * 128)(*[i % 3 for i in range(128)])
    out = (C.c_int64 * 2048)()
    expected = [sum(weights[row * 128 + k] * x[k] for k in range(128)) for row in range(1024)]
    entries.value = 0
    real.blocked(weights, x, out)
    assert list(out)[:1024] == expected and entries.value > 1, entries.value
    serial = list(weights)
    for row in range(1024):
        serial[128 + row] = sum(serial[row * 128 + k] * x[k] for k in range(128))
    entries.value = 0
    real.blocked(weights, x, C.addressof(weights) + 128 * 8)
    assert list(weights) == serial and entries.value == 1, (entries.value,
        [(i, a, b) for i, (a, b) in enumerate(zip(weights, serial)) if a != b][:8])

    weights = (C.c_int64 * 8192)(*[i % 7 for i in range(8192)])
    x = (C.c_int64 * 128)(*[1] * 128)
    expected = [sum(x[(o // 1024) * 4 + k] * weights[k * 1024 + o % 1024]
                    for k in range(4)) for o in range(2048)]
    entries.value = 0
    real.rectangular(x, weights, out, 4, 1024, 2048)
    assert list(out) == expected and entries.value > 1, entries.value
    serial = list(weights)
    for o in range(2048):
        serial[o + 1] = sum(x[(o // 1024) * 4 + k] * serial[k * 1024 + o % 1024]
                            for k in range(4))
    entries.value = 0
    real.rectangular(x, weights, C.addressof(weights) + 8, 4, 1024, 2048)
    assert list(weights) == serial and entries.value == 1, (entries.value,
        [(i, a, b) for i, (a, b) in enumerate(zip(weights, serial)) if a != b][:8])

print("alias dispatch PASS: worker entries, overlap, bounds, and callee fallback")
