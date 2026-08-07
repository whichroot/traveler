#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Independently validate LANG0 fields, structure, and output bytes."""

import argparse
import hashlib
import pathlib
import struct


MASK_BEGIN = bytes.fromhex("0f055401")
MASK_ELSE = bytes.fromhex("0f040419")
MASK_POP = bytes.fromhex("0f0604010000")
STOP = bytes.fromhex("0e000000")
CONDITION = {"ugt": 4, "ult": 5, "sgt": 6, "slt": 7, "eq": 7}
INVERT = {"uge": "ult", "ule": "ugt", "sge": "slt",
          "sle": "sgt", "ne": "eq"}


class Unmeasured(ValueError):
    pass


def cmpsel_bool(dest: int, lhs: int, rhs: int, relation: str) -> bytes:
    if any(not 0 <= reg <= 15 for reg in (dest, lhs, rhs)):
        raise Unmeasured("compare register is outside r0..r15")
    invert = relation in INVERT
    base = INVERT.get(relation, relation)
    if base not in CONDITION:
        raise Unmeasured("comparison is not measured")
    true_value, false_value = (0x80, 0x81) if invert else (0x81, 0x80)
    true_mode = 0x22 | (0x04 if base == "eq" else 0)
    return bytes([
        0x02 | (dest << 4), (lhs << 1) | 1, 0x1F,
        (rhs << 1) | 1, true_mode, true_value,
        CONDITION[base], 0x00, 0x20, false_value,
    ])


def if_bool(value: int, bound: int, truth: bool) -> bytes:
    if any(not 0 <= reg <= 15 for reg in (value, bound)):
        raise Unmeasured("control register is outside r0..r15")
    return bytes([
        0x0A, 0x81 | (value << 1), 0x22,
        0x81 | (bound << 1), 4 if truth else 5, 0x00,
    ])


def check_encoder() -> None:
    if cmpsel_bool(9, 7, 8, "ult") != bytes.fromhex(
            "920f1f11228105002080"):
        raise ValueError("LANG0 compare/select field encoder changed")
    if cmpsel_bool(9, 7, 8, "ne") != bytes.fromhex(
            "920f1f11268007002081"):
        raise ValueError("LANG0 inverted equality encoder changed")
    if if_bool(9, 7, True) != bytes.fromhex("0a93228f0400"):
        raise ValueError("LANG0 true-control encoder changed")
    if if_bool(9, 7, False) != bytes.fromhex("0a93228f0500"):
        raise ValueError("LANG0 false-control encoder changed")
    for refuse in (
        lambda: cmpsel_bool(16, 1, 2, "ult"),
        lambda: cmpsel_bool(1, 2, 3, "bogus"),
        lambda: if_bool(1, 16, True),
    ):
        try:
            refuse()
        except Unmeasured:
            continue
        raise ValueError("LANG0 encoder accepted an unmeasured field")


def parse_artifact(path: pathlib.Path) -> list[dict]:
    workers: list[dict] = []
    worker: dict | None = None
    in_code = False
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if line.startswith("worker "):
            worker = {"name": line.split()[1], "captures": [], "code": []}
            workers.append(worker)
        elif worker is not None and line.startswith("capture "):
            worker["captures"].append(line)
        elif worker is not None and line.startswith("bytes "):
            worker["bytes"] = int(line.split()[1])
        elif worker is not None and line.startswith("instructions "):
            worker["instructions"] = int(line.split()[1])
        elif line == "code":
            in_code = True
        elif worker is not None and line.startswith("end "):
            in_code = False
            worker = None
        elif in_code and worker is not None:
            worker["code"].append(bytes.fromhex(line))
    return workers


def decode_cmpsel(inst: bytes) -> tuple[int, int, int, str] | None:
    if len(inst) != 10 or inst[0] & 0x0F != 2 or inst[2] != 0x1F:
        return None
    dest, lhs, rhs = inst[0] >> 4, inst[1] >> 1, inst[3] >> 1
    inverted = (inst[5], inst[9]) == (0x80, 0x81)
    if (inst[5], inst[9]) not in ((0x81, 0x80), (0x80, 0x81)):
        raise ValueError("LANG0 compare does not select canonical bools")
    equality = bool(inst[4] & 0x04)
    if equality:
        relation = "ne" if inverted else "eq"
    else:
        base = {4: "ugt", 5: "ult", 6: "sgt", 7: "slt"}.get(inst[6])
        if base is None:
            raise ValueError("LANG0 compare condition is not measured")
        reverse = {"ugt": "ule", "ult": "uge", "sgt": "sle", "slt": "sge"}
        relation = reverse[base] if inverted else base
    return dest, lhs, rhs, relation


def check_artifact(path: pathlib.Path) -> None:
    workers = parse_artifact(path)
    expected = (
        (360, 53, "capture branch_out slot 0",
         "d5d6f69955763f30d8a4d214c316081e0a7f48a223965fe171c326e60bdb788e"),
        (1122, 199, "capture loop_out slot 0",
         "305703470cfb10bfc1a55dbcdbbc1edc555033e6d5e387c9e6b91f86a23f7ad2"),
        (3076, 583, "capture nested_out slot 0",
         "1fb59d475feb03fd719483695e68f81dd84f786ea5002389f0fb90aec5bd2211"),
        (1066, 175, "capture exit_out slot 0",
         "b3fb8ac5a325e478f94d3580e59a8841cb3ebe9f76e7e93fefa5c40ed80559e4"),
    )
    if len(workers) != len(expected):
        raise ValueError(f"expected four LANG0 workers, saw {len(workers)}")
    saw_compare = saw_control = saw_else = False
    for worker, (byte_count, instruction_count, output_capture, code_hash) in zip(
            workers, expected):
        code = worker["code"]
        if worker.get("bytes") != byte_count \
                or sum(map(len, code)) != byte_count:
            raise ValueError("LANG0 byte count changed")
        if worker.get("instructions") != instruction_count \
                or len(code) != instruction_count:
            raise ValueError("LANG0 instruction count changed")
        if hashlib.sha256(b"".join(code)).hexdigest() != code_hash:
            raise ValueError("LANG0 machine-code hash changed")
        captures = worker["captures"]
        if captures != [
            "capture left slot 1",
            "capture right slot 1 word-offset 768",
            output_capture,
        ]:
            raise ValueError("LANG0 capture/extents contract changed")
        if code[-1] != STOP:
            raise ValueError("LANG0 worker has no stop")
        depth = maximum = 0
        for inst in code:
            if inst.startswith(bytes.fromhex("8f0454")):
                raise ValueError("LANG0 bounded loops unexpectedly emitted a backedge")
            if inst == MASK_BEGIN:
                depth += 1
                maximum = max(maximum, depth)
            elif inst == MASK_ELSE:
                if depth == 0:
                    raise ValueError("LANG0 else underflow")
                saw_else = True
            elif inst == MASK_POP:
                depth -= 1
                if depth < 0:
                    raise ValueError("LANG0 mask underflow")
            decoded = decode_cmpsel(inst)
            if decoded is not None:
                if cmpsel_bool(*decoded) != inst:
                    raise ValueError("LANG0 compare failed independent re-encoding")
                saw_compare = True
            if len(inst) == 6 and inst[0] == 0x0A and inst[2] == 0x22:
                value = (inst[1] & 0x7E) >> 1
                bound = (inst[3] & 0x7E) >> 1
                truth = inst[4] == 4
                if inst[4] not in (4, 5) or if_bool(value, bound, truth) != inst:
                    raise ValueError("LANG0 control failed independent re-encoding")
                saw_control = True
        if depth != 0 or maximum > 4:
            raise ValueError("LANG0 mask stack is unbalanced or too deep")
    if not (saw_compare and saw_control and saw_else):
        raise ValueError("LANG0 artifact missed a required structured field")


def expected_output() -> bytes:
    modulus = 2013265921
    mask = (1 << 64) - 1
    state = 0x6A09E667F3BCC909
    left: list[int] = []
    right: list[int] = []
    for _ in range(768):
        state = (state * 6364136223846793005 + 1442695040888963407) & mask
        left.append(state % modulus)
        state = (state * 6364136223846793005 + 1442695040888963407) & mask
        right.append(state % modulus)
    left[:4] = [0, 3, 7, 19]
    right[:4] = [0, 5, 11, 2]
    branch = [b if ((a < b and a != 7) or b == 11) else a
              for a, b in zip(left, right)]
    loop = list(right)
    nested: list[int] = []
    for a, b in zip(left, right):
        score = 0
        for _k in range(3):
            for m in range(3):
                if m == 1 and a < b:
                    continue
                if m == 2 and b < a:
                    break
                score += 1
        nested.append(b if score < 7 else a)
    exit_nested = [b if a < b else a for a, b in zip(left, right)]
    return struct.pack("<3072I", *(branch + loop + nested + exit_nested))


def check_output(path: pathlib.Path) -> None:
    got = path.read_bytes()
    want = expected_output()
    if got != want:
        raise ValueError("LANG0 output differs from the independent oracle")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check-only", action="store_true")
    parser.add_argument("--check-artifact", type=pathlib.Path)
    parser.add_argument("--check-output", type=pathlib.Path)
    args = parser.parse_args()
    check_encoder()
    if args.check_artifact is not None:
        check_artifact(args.check_artifact)
    if args.check_output is not None:
        check_output(args.check_output)
    print("AGX LANG0 PASS: fields, masks, bounded loops, and oracle pinned")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
