#!/usr/bin/env python3
"""Build an atomic Traveler CUDA package from compiler-described LLVM IR."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import struct
import subprocess
import tempfile

MAGIC = b"TVCP0001"
PREFIX = "; traveler.kernel.v1 "
MAX_HEADER = 262144
MAX_PTX = 16777216


def require(condition, message):
    if not condition:
        raise ValueError(message)


def integer(value, low, high):
    return type(value) is int and low <= value <= high


def unique_object(pairs):
    obj = {}
    for key, value in pairs:
        require(key not in obj, f"duplicate key: {key}")
        obj[key] = value
    return obj


def validate_kernel(k):
    require(type(k) is dict, "kernel must be an object")
    require(set(k) == {"schema", "abi", "stage", "compiler", "target", "artifact",
                       "symbol", "owner", "specialization", "line", "column", "execution",
                       "profile", "block", "dimensions", "lanes_per_cell", "dynamic_shared_bytes",
                       "index_bits", "domain", "parameters", "disjoint"}, "kernel fields")
    for key, value in {"schema": "traveler.kernel.v1", "abi": 1, "stage": "semantic-ir",
                       "compiler": "tvc_self", "target": "nvptx64-nvidia-cuda", "artifact": None,
                       "execution": "independent-pfor", "profile": "direct-index-v1",
                       "block": [256, 1, 1], "dimensions": 1, "lanes_per_cell": 1,
                       "dynamic_shared_bytes": 0, "index_bits": 32,
                       "domain": {"min_lo": 0, "max_hi": 2147483392, "empty": "no-launch"}}.items():
        require(k[key] == value and type(k[key]) is type(value), f"unsupported {key}")
    require(all(type(v) is int for v in k["block"]), "block dimensions")
    require(type(k["domain"]["min_lo"]) is int and type(k["domain"]["max_hi"]) is int, "domain integers")
    require(isinstance(k["symbol"], str) and re.fullmatch(r"__pfor_gpu_worker_[0-9]+", k["symbol"]), "entry symbol")
    require(isinstance(k["owner"], str) and 0 < len(k["owner"]) <= 255, "owner")
    require(integer(k["line"], 1, 2147483647) and integer(k["column"], 0, 2147483647), "location")
    require(type(k["specialization"]) is list and len(k["specialization"]) <= 32, "specialization")
    for sub in k["specialization"]:
        require(type(sub) is dict and set(sub) == {"name", "type"}, "specialization fields")
        require(all(type(v) is str and 0 < len(v) <= 255 for v in sub.values()), "specialization types")
    params = k["parameters"]
    require(type(params) is list and 3 <= len(params) <= 34, "parameter limit")
    base = {"ordinal", "name", "source_type", "synthetic", "kind", "llvm_type", "size", "alignment"}
    for i, p in enumerate(params):
        require(type(p) is dict and base <= p.keys(), "parameter fields")
        require(type(p["ordinal"]) is int and p["ordinal"] == i, "parameter order")
        require(type(p["synthetic"]) is bool and p["synthetic"] == (i >= len(params) - 2), "synthetic parameter")
        require(type(p["name"]) is str and 0 < len(p["name"]) <= 255, "parameter name")
        if p["synthetic"]:
            require(p["name"] == ("lo" if i == len(params) - 2 else "hi") and p["source_type"] == "i32", "bounds ABI")
        if p["kind"] == "integer":
            require(set(p) == base | {"bits", "signed"}, "integer fields")
            require(type(p["source_type"]) is str and re.fullmatch(r"(?:bool|[iu](?:8|16|32|64))", p["source_type"]), "integer type")
            bits = 8 if p["source_type"] == "bool" else int(p["source_type"][1:])
            require(type(p["bits"]) is int and p["bits"] == bits and p["llvm_type"] == f"i{bits}", "integer width")
            require(type(p["signed"]) is bool and p["signed"] == (bits != 1 and p["source_type"].startswith("i")), "signedness")
            size = (bits + 7) // 8
        else:
            require(p["kind"] == "pointer" and not p["synthetic"], "parameter kind")
            require(set(p) == base | {"address_space", "element_type", "element_size", "element_alignment", "access", "footprint"}, "pointer fields")
            require(type(p["element_type"]) is str and re.fullmatch(r"(?:bool|[iu](?:8|16|32|64|128|256|512))", p["element_type"]), "element type")
            size = 8
            elem = 1 if p["element_type"] == "bool" else int(p["element_type"][1:]) // 8
            require(p["source_type"] == "*" + p["element_type"] and p["llvm_type"] == "ptr addrspace(1)", "pointer ABI")
            require(type(p["address_space"]) is int and p["address_space"] == 1, "address space")
            require(type(p["element_size"]) is int and p["element_size"] == elem and type(p["element_alignment"]) is int and p["element_alignment"] == min(elem, 16), "element layout")
            require(p["access"] in ("read", "write", "read-write"), "pointer effect")
            require(p["footprint"] == {"start_parameter": len(params) - 2, "end_parameter": len(params) - 1, "byte_scale": elem}, "footprint")
            require(all(type(v) is int for v in p["footprint"].values()), "footprint integers")
        require(type(p["size"]) is int and type(p["alignment"]) is int and p["size"] == p["alignment"] == size, "ABI layout")
    require(len({p["name"] for p in params[:-2]}) == len(params) - 2, "duplicate capture name")
    pairs = [[i, j] for i, p in enumerate(params) for j, q in enumerate(params)
             if i < j and p["kind"] == q["kind"] == "pointer" and
             ("write" in p["access"] or "write" in q["access"])]
    require(k["disjoint"] == pairs and all(type(v) is int for pair in k["disjoint"] for v in pair), "disjointness")


def descriptors(ir):
    entries = [json.loads(line[len(PREFIX):], object_pairs_hook=unique_object)
               for line in ir.splitlines() if line.startswith(PREFIX)]
    require(1 <= len(entries) <= 64, "entry limit")
    for k in entries:
        validate_kernel(k)
    signatures = dict(re.findall(r"define ptx_kernel void @(\w+)\(([^\n]*)\) #0", ir))
    require(len(signatures) == len(entries) == len({k["symbol"] for k in entries}), "entry table mismatch")
    for k in entries:
        signature = ", ".join(f'{p["llvm_type"]} %t{i}' for i, p in enumerate(k["parameters"]))
        require(signatures.get(k["symbol"]) == signature, "descriptor/signature mismatch")
    return entries


def build(ir_path, output, llc, sm):
    require(integer(sm, 50, 999), "invalid target SM")
    ir = Path(ir_path).read_text()
    entries = descriptors(ir)
    with tempfile.TemporaryDirectory() as temp:
        source = Path(temp) / "module.ll"
        target = Path(temp) / "module.ptx"
        source.write_text(ir)
        subprocess.run([llc, "-mtriple=nvptx64-nvidia-cuda", f"-mcpu=sm_{sm}",
                        str(source), "-o", str(target)], check=True)
        ptx = target.read_bytes()
    require(0 < len(ptx) <= MAX_PTX and b"\0" not in ptx, "PTX size or embedded NUL")
    target = re.search(rb"(?m)^\s*\.target sm_(\d+)\s*$", ptx)
    version = re.search(rb"(?m)^\s*\.version (\d+)\.(\d+)\s*$", ptx)
    require(target and int(target[1]) == sm and version, "PTX target/version mismatch")
    require(set(re.findall(rb"\.visible \.entry (\w+)\(", ptx)) == {k["symbol"].encode() for k in entries}, "PTX entry mismatch")
    manifest = {"schema": "traveler.cuda.package.v1", "abi": 1, "sm": sm,
                "ptx_major": int(version[1]), "ptx_minor": int(version[2]),
                "ptx_bytes": len(ptx), "ptx_sha256": hashlib.sha256(ptx).hexdigest(),
                "kernels": entries}
    header = json.dumps(manifest, separators=(",", ":"), ensure_ascii=True).encode()
    require(len(header) <= MAX_HEADER, "manifest limit")
    path = Path(output)
    fd, staged = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(MAGIC + struct.pack("<I", len(header)) + header + ptx)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(staged, path)
    finally:
        if os.path.exists(staged):
            os.unlink(staged)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("ir")
    parser.add_argument("-o", "--output", required=True)
    parser.add_argument("--llc", default="llc")
    parser.add_argument("--sm", type=int, required=True)
    args = parser.parse_args()
    try:
        build(args.ir, args.output, args.llc, args.sm)
    except (ValueError, OSError, subprocess.CalledProcessError) as error:
        parser.exit(1, f"cuda package: {error}\n")
