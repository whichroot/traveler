#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Check the portable host/artifact AGX code-content pin."""

import re
import sys
from pathlib import Path


MASK64 = (1 << 64) - 1


def read_worker(path: str) -> tuple[int, int, bytes]:
    field = 0
    grid = 0
    code = bytearray()
    selected = False
    in_code = False
    for raw in Path(path).read_text(encoding="ascii").splitlines():
        line = raw.strip()
        if line == "worker __pfor_gpu_worker_0":
            selected = True
        elif selected and line.startswith("field "):
            field = int(line.removeprefix("field "))
        elif selected and line.startswith("grid "):
            grid = int(line.removeprefix("grid "))
        elif selected and line == "code":
            in_code = True
        elif selected and line == "end __pfor_gpu_worker_0":
            break
        elif in_code:
            code.extend(bytes.fromhex(line))
    if field == 0 or grid == 0 or not code:
        raise ValueError(f"worker 0 is incomplete in {path}")
    return field, grid, bytes(code)


def fnv1a(data: bytes) -> int:
    value = 14695981039346656037
    for byte in data:
        value = ((value ^ byte) * 1099511628211) & MASK64
    return value


def main() -> int:
    if len(sys.argv) != 4:
        raise SystemExit("usage: agx_dispatch_pin.py HOST_LL RIGHT_AGX WRONG_AGX")
    host = Path(sys.argv[1]).read_text(encoding="ascii")
    calls = re.findall(
        r"call i32 @agx_try_parallel_for\(i32 0, [^\n]*, "
        r"i64 (-?\d+), i64 (-?\d+)\)",
        host,
    )
    if len(calls) != 1:
        raise ValueError(f"expected one pinned worker-0 call, found {len(calls)}")
    host_field = int(calls[0][0]) & MASK64
    host_hash = int(calls[0][1]) & MASK64
    right_field, right_grid, right_code = read_worker(sys.argv[2])
    wrong_field, wrong_grid, wrong_code = read_worker(sys.argv[3])
    if (right_field, right_grid) != (wrong_field, wrong_grid):
        raise ValueError("wrong artifact does not preserve field/grid metadata")
    if host_field != right_field or right_grid != 768:
        raise ValueError("host and artifact metadata differ")
    if host_hash != fnv1a(right_code):
        raise ValueError("host code pin does not match the same-source artifact")
    if host_hash == fnv1a(wrong_code):
        raise ValueError("different code unexpectedly matches the host code pin")
    print("AGX dispatch pin PASS: same metadata, different code refused")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
