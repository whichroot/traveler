"""Check semantic interfaces against signatures and atomic publication."""
import json
from pathlib import Path
import re
import subprocess
import sys
import tempfile

TVC = sys.argv[1]
HERE = Path(__file__).resolve().parent
PREFIX = "; traveler.kernel.v1 "


def compile(source, *extra):
    return subprocess.run([TVC, "--emit-gpu-nvptx", str(source), *extra],
                          capture_output=True, text=True)


def check(ir):
    rows = [json.loads(line[len(PREFIX):]) for line in ir.splitlines()
            if line.startswith(PREFIX)]
    signatures = dict(re.findall(r"define ptx_kernel void @(\w+)\(([^\n]*)\) #0", ir))
    assert rows and len(rows) == len(signatures)
    assert len({r["symbol"] for r in rows}) == len(rows)
    for row in rows:
        assert row["schema"] == "traveler.kernel.v1" and row["abi"] == 1
        assert row["stage"] == "semantic-ir" and row["artifact"] is None
        assert row["target"] == "nvptx64-nvidia-cuda"
        assert row["block"] == [256, 1, 1] and row["dimensions"] == 1
        assert row["lanes_per_cell"] == 1 and row["dynamic_shared_bytes"] == 0
        assert row["index_bits"] == 32 and row["line"] > 0
        assert row["domain"] == {"min_lo": 0, "max_hi": 2147483392, "empty": "no-launch"}
        params = row["parameters"]
        assert signatures[row["symbol"]] == ", ".join(
            f'{p["llvm_type"]} %t{i}' for i, p in enumerate(params))
        assert [p["name"] for p in params[-2:]] == ["lo", "hi"]
        for i, p in enumerate(params):
            assert p["ordinal"] == i and p["synthetic"] == (i >= len(params) - 2)
            if p["kind"] == "pointer":
                bits = 8 if p["element_type"] == "bool" else int(p["element_type"][1:])
                assert p["source_type"] == "*" + p["element_type"]
                assert p["size"] == p["alignment"] == 8 and p["address_space"] == 1
                assert p["element_size"] == (bits + 7) // 8
                assert p["element_alignment"] == min((bits + 7) // 8, 16)
                assert p["access"] in ("read", "write", "read-write")
                assert p["footprint"] == {"start_parameter": len(params) - 2,
                                          "end_parameter": len(params) - 1,
                                          "byte_scale": (bits + 7) // 8}
            else:
                assert p["bits"] in (1, 8, 16, 32, 64)
                assert p["size"] == p["alignment"] == (p["bits"] + 7) // 8
                assert p["signed"] == (p["bits"] != 1 and p["source_type"].startswith("i"))
        expected = [[i, j] for i, p in enumerate(params) for j, q in enumerate(params)
                    if i < j and p["kind"] == q["kind"] == "pointer"
                    and ("write" in p["access"] or "write" in q["access"])]
        assert row["disjoint"] == expected
    return rows


with tempfile.TemporaryDirectory() as directory:
    temp = Path(directory)
    source = temp / "kernels.tv"
    source.write_text("""
fn mixed(a: *i32, b: *i32, c: *i32, gain: i32) {
    for i in 0..1024 { c[i] = b[i] * gain; a[i] = a[i] + b[i]; }
}
fn small(a: *u8, b: *u8, gain: u8) {
    for i in 0..1024 { b[i] = a[i] + gain; }
}
fn medium(a: *i16, b: *i16, gain: i16) {
    for i in 0..1024 { b[i] = a[i] + gain; }
}
fn large(a: *u64, b: *u64, gain: u64) {
    for i in 0..1024 { b[i] = a[i] + gain; }
}
fn boolean(a: *i32, b: *i32, flag: bool) {
    for i in 0..1024 { b[i] = a[i] + (flag as i32); }
}
""")
    plain = compile(source)
    described = compile(source, "--gpu-interface")
    assert described.returncode == plain.returncode == 0, described.stderr
    assert "".join(line for line in described.stdout.splitlines(keepends=True)
                   if not line.startswith(PREFIX)) == plain.stdout
    rows = check(described.stdout)
    assert len(rows) == 5
    mixed = next(r for r in rows if r["owner"] == "mixed")
    assert {p["name"]: p["access"] for p in mixed["parameters"] if p["kind"] == "pointer"} == {
        "a": "read-write", "b": "read", "c": "write"}
    assert [p["name"] for p in mixed["parameters"][:4]] != ["a", "b", "c", "gain"]
    output = temp / "kernels.ll"
    written = compile(source, "--gpu-interface", "-o", str(output))
    assert written.returncode == 0 and not written.stdout
    assert output.read_text() == described.stdout
    helper = compile(HERE / "gpu_scalar_calls.tv", "--gpu-interface")
    assert helper.returncode == 0, helper.stderr
    check(helper.stdout)
    for fixture in ("gpu_i512_calls.tv", "gpu_u512_calls.tv"):
        wide = compile(HERE / fixture, "--gpu-interface")
        assert wide.returncode == 0, wide.stderr
        check(wide.stdout)

    for text in [
        "fn offset(a: *i32, b: *i32) { for i in 0..1024 { b[i] = a[i + 1]; } }",
    ]:
        bad = temp / "bad.tv"
        bad.write_text(text)
        assert compile(bad).returncode == 0
        for mixed_module in (False, True):
            bad.write_text(text + (source.read_text() if mixed_module else ""))
            for existing in (False, True):
                output.unlink(missing_ok=True)
                if existing:
                    output.write_text("previous artifact\n")
                refused = compile(bad, "--gpu-interface", "-o", str(output))
                assert refused.returncode == 1 and not refused.stdout, refused
                assert "kernel interface requires" in refused.stderr
                assert output.read_text() == "previous artifact\n" if existing else not output.exists()
                assert not list(temp.glob("*.tvctmp*"))
            refused = compile(bad, "--gpu-interface")
            assert refused.returncode == 1 and not refused.stdout

    for flags in ([], ["--emit-gpu"], ["--emit-gpu-agx"], ["--emit-gpu-vulkan"],
                  ["--emit-gpu-nvptx", "--eval"], ["--emit-gpu-nvptx", "--diagnostics"]):
        refused = subprocess.run([TVC, str(source), "--gpu-interface", *flags],
                                 capture_output=True, text=True)
        assert refused.returncode == 1 and not refused.stdout

print("kernel interface PASS: ABI order/layout, effects, footprints, and atomic publication")
