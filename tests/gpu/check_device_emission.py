"""Check Stage-0 decisions, exit status, and transactional output publication."""
import json
from pathlib import Path
import subprocess
import sys
import tempfile

TVC = sys.argv[1]
ROOT = Path(__file__).resolve().parents[2]
HERE = Path(__file__).resolve().parent


def compile(source, mode, output=None):
    args = [TVC, mode, str(source)]
    if output is not None:
        args += ["-o", str(output)]
    result = subprocess.run(args, capture_output=True, text=True)
    rows = [json.loads(line) for line in result.stderr.splitlines() if line.startswith("{")]
    assert rows and all(r["schema"] == "traveler.device.v1" for r in rows), result
    assert all(r["target"] == ("amdgcn" if mode == "--emit-gpu" else "nvptx") for r in rows)
    workers = [r for r in rows if r["kind"] == "worker"]
    summary = rows[-1]
    assert summary["kind"] == "module", rows
    assert summary["candidates"] == len(workers), rows
    assert summary["emitted"] == sum(r["status"] == "emitted" for r in workers), rows
    assert summary["refused"] == sum(r["status"] == "refused" for r in workers), rows
    assert all(r["fn"] and r["line"] > 0 for r in workers), rows
    assert all(r["reason"] for r in workers if r["status"] == "refused"), rows
    return result, workers, summary


with tempfile.TemporaryDirectory() as directory:
    temp = Path(directory)
    empty = temp / "empty.tv"
    empty.write_text("fn main() {}\n")
    mixed = temp / "mixed.tv"
    mixed.write_text((HERE / "gpu_elem_call_refuse.tv").read_text() +
                     "\nfn device_ok(a: *i32, b: *i32) { for i in 0..1024 { b[i] = a[i] + 1; } }\n")
    for mode in ["--emit-gpu", "--emit-gpu-nvptx"]:
        for source, reason in [
            (HERE / "gpu_elem_call_refuse.tv", "uncarried-call"),
            (HERE / "gpu_private_refuse.tv", "body-shape"),
            (HERE / "gpu_prefetch_refuse.tv", None),
            (HERE / "gpu_i64_iter_refuse.tv", "i64-iterator"),
            (ROOT / "examples/rns_dyn_matmul.tv", None),
            (ROOT / "examples/closure_prove_through.tv", None),
            (empty, None),
        ]:
            for existing in [False, True]:
                output = temp / "result.ll"
                output.unlink(missing_ok=True)
                if existing:
                    output.write_text("previous successful artifact\n")
                result, workers, summary = compile(source, mode, output)
                assert result.returncode == 1 and not result.stdout, result
                assert summary["emitted"] == 0 and summary["reason"] == "no-usable-workers", summary
                if reason:
                    assert any(r["reason"] == reason for r in workers), workers
                if existing:
                    assert output.read_text() == "previous successful artifact\n"
                else:
                    assert not output.exists()
                assert not list(temp.glob("*.tvctmp*"))
            result, _, _ = compile(source, mode)
            assert result.returncode == 1 and result.stdout == "", result
        result, workers, summary = compile(mixed, mode)
        assert result.returncode == 0 and summary["emitted"] > 0 and summary["refused"] > 0, result
        assert "define " in result.stdout and "@dnq32r" not in result.stdout
        assert any(r["fn"] == "device_ok" and r["status"] == "emitted" for r in workers)
        output = temp / "result.ll"
        written, _, _ = compile(mixed, mode, output)
        assert written.returncode == 0 and not written.stdout
        assert output.read_text() == result.stdout
        assert not list(temp.glob("*.tvctmp*"))
        bad, _, _ = compile(mixed, mode, temp / "missing" / "result.ll")
        assert bad.returncode == 1 and not bad.stdout

print("device emission PASS: decisions, empty/mixed modules, and output publication")
