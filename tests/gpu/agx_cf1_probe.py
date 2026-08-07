#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Encode and execute the measured G16X structured mask-stack form."""

import pathlib
import sys


STOP = bytes.fromhex("0e000000")
MASK_BEGIN = bytes.fromhex("0f055401")
MASK_ELSE = bytes.fromhex("0f040419")
MASK_POP = bytes.fromhex("0f0604010000")
MAX_MEASURED_DEPTH = 4
STRICT_CONDITION = {
    "ugt": 4,
    "ult": 5,
    "sgt": 6,
    "slt": 7,
}


class Unmeasured(ValueError):
    pass


def if_icmp_g(lhs: int, rhs: int, relation: str) -> bytes:
    """Set the execution predicate from a strict G/G integer comparison."""
    for name, reg in (("left source", lhs), ("right source", rhs)):
        if not 0 <= reg <= 15:
            raise Unmeasured(f"{name} is outside measured r0..r15")
    if relation not in STRICT_CONDITION:
        raise Unmeasured(f"control relation {relation!r} is not measured")
    return bytes([
        0x0A,
        0x81 | (lhs << 1),
        0x22,
        0x81 | (rhs << 1),
        STRICT_CONDITION[relation],
        0x00,
    ])


class MaskStack:
    """Validate and encode the measured structured mask nesting subset."""

    def __init__(self) -> None:
        self.frames: list[bool] = []

    def begin(self) -> bytes:
        if len(self.frames) >= MAX_MEASURED_DEPTH:
            raise Unmeasured("mask nesting exceeds measured depth 4")
        self.frames.append(False)
        return MASK_BEGIN

    def otherwise(self) -> bytes:
        if not self.frames:
            raise Unmeasured("else has no open mask frame")
        if self.frames[-1]:
            raise Unmeasured("mask frame has more than one else")
        self.frames[-1] = True
        return MASK_ELSE

    def pop(self) -> bytes:
        if not self.frames:
            raise Unmeasured("mask pop has no open frame")
        self.frames.pop()
        return MASK_POP

    def finish(self) -> None:
        if self.frames:
            raise Unmeasured("mask stack is not balanced")


def check_encoder() -> None:
    # Full references come from unrelated Apple-compiler source shapes.
    references = (
        (if_icmp_g(1, 2, "ult"), "0a8322850500"),
        (if_icmp_g(7, 2, "ult"), "0a8f22850500"),
        (if_icmp_g(2, 1, "ult"), "0a8522830500"),
    )
    for encoded, reference in references:
        if encoded != bytes.fromhex(reference):
            raise ValueError("CF1 branch encoder differs from compiler reference")

    # Compiler captures using another source mode independently pin conditions.
    condition_references = {
        "ugt": "0a03320504c0",
        "ult": "0a03320505c0",
        "sgt": "0a03320506c0",
        "slt": "0a03320507c0",
    }
    for relation, reference in condition_references.items():
        if if_icmp_g(1, 2, relation)[4] != bytes.fromhex(reference)[4]:
            raise ValueError("CF1 condition differs from compiler reference")

    stack = MaskStack()
    if stack.begin() != MASK_BEGIN or stack.otherwise() != MASK_ELSE \
            or stack.pop() != MASK_POP:
        raise ValueError("CF1 mask records changed")
    stack.finish()

    refusals = (
        lambda: if_icmp_g(-1, 2, "ult"),
        lambda: if_icmp_g(1, 16, "ult"),
        lambda: if_icmp_g(1, 2, "eq"),
        lambda: if_icmp_g(1, 2, "ule"),
        _refuse_depth_five,
        _refuse_else_underflow,
        _refuse_duplicate_else,
        _refuse_pop_underflow,
        _refuse_unbalanced,
    )
    for refuse in refusals:
        try:
            refuse()
        except Unmeasured:
            continue
        raise ValueError("CF1 encoder accepted an unmeasured structure")

    expected_lengths = (68, 182, 110, 64, 68)
    actual_lengths = tuple(map(len, (
        diamond_program(), nested_program(), sequential_program(),
        no_else_program(), empty_then_program(),
    )))
    if actual_lengths != expected_lengths:
        raise ValueError(f"CF1 program lengths changed: {actual_lengths}")


def _refuse_depth_five() -> None:
    stack = MaskStack()
    for _ in range(MAX_MEASURED_DEPTH + 1):
        stack.begin()


def _refuse_else_underflow() -> None:
    MaskStack().otherwise()


def _refuse_duplicate_else() -> None:
    stack = MaskStack()
    stack.begin()
    stack.otherwise()
    stack.otherwise()


def _refuse_pop_underflow() -> None:
    MaskStack().pop()


def _refuse_unbalanced() -> None:
    stack = MaskStack()
    stack.begin()
    stack.finish()


def prologue(reg: int) -> bytes:
    return bytes([0x0C | (reg << 4), 0xA0, 0x10, 0x06])


def mov_imm(reg: int, value: int) -> bytes:
    return bytes([0x0C | (reg << 4), value])


def alu_imm(dest: int, src: int, value: int) -> bytes:
    return bytes([
        0x9F, 0x11, 0x54, dest * 2, 0x02,
        value * 2, src << 3, 0x88, 0x11, 0x04,
    ])


def alu_add(dest: int, lhs: int, rhs: int) -> bytes:
    return bytes([
        0x9F, 0x11, 0x54, dest * 2, 0x02,
        rhs << 2, lhs << 3, 0xA8, 0x17, 0x05,
    ])


def load_r(dest: int, index: int, tid_direct: bool) -> bytes:
    return bytes([
        0x67, 0x10 if tid_direct else 0x00, 0x44, dest, 0x01,
        index if tid_direct else 0x80 | index,
        0x20, 0x00, 0x51, 0x01, 0x00, 0x40, 0x46, 0x00,
    ])


def move_r_to_g(dest: int, src: int) -> bytes:
    return bytes([
        0xA7, 0x00, 0x56, dest * 2, 0x02, src * 2,
        0x00, 0x00, 0xF0, 0x11, 0x01, 0x00,
    ])


def store_g(value: int, index: int) -> bytes:
    return bytes([
        0xE7, 0x10, 0x54, value * 2, 0x00, index,
        0x20, 0x00, 0x11, 0x00, 0x00, 0x90, 0x11, 0x00,
    ])


def diamond_program() -> bytes:
    stack = MaskStack()
    code = bytearray(prologue(0))
    code.extend(alu_imm(1, 0, 0))
    code.extend(mov_imm(2, 4))
    code.extend(if_icmp_g(1, 2, "ult"))
    code.extend(stack.begin())
    code.extend(mov_imm(4, 11))
    code.extend(stack.otherwise())
    code.extend(mov_imm(4, 22))
    code.extend(stack.pop())
    stack.finish()
    code.extend(alu_imm(5, 4, 100))
    code.extend(store_g(5, 0))
    code.extend(STOP)
    return bytes(code)


def _nested_region(stack: MaskStack, level: int) -> bytes:
    lhs = 1 + level * 2
    rhs = lhs + 1
    code = bytearray(if_icmp_g(lhs, rhs, "ult"))
    code.extend(stack.begin())
    if level == MAX_MEASURED_DEPTH - 1:
        code.extend(mov_imm(9, 1))
    else:
        code.extend(_nested_region(stack, level + 1))
    code.extend(stack.otherwise())
    code.extend(mov_imm(9, level + 2))
    code.extend(stack.pop())
    return bytes(code)


def nested_program() -> bytes:
    stack = MaskStack()
    code = bytearray(prologue(0))
    for level, bound in enumerate((7, 6, 5, 4)):
        code.extend(alu_imm(1 + level * 2, 0, 0))
        code.extend(mov_imm(2 + level * 2, bound))
    code.extend(mov_imm(15, 29))
    code.extend(_nested_region(stack, 0))
    stack.finish()
    code.extend(alu_add(10, 9, 15))
    code.extend(alu_imm(11, 10, 100))
    code.extend(store_g(11, 0))
    code.extend(STOP)
    return bytes(code)


def sequential_program() -> bytes:
    stack = MaskStack()
    code = bytearray(prologue(0))
    for lhs, rhs, bound in ((1, 2, 2), (3, 4, 6)):
        code.extend(alu_imm(lhs, 0, 0))
        code.extend(mov_imm(rhs, bound))
    code.extend(if_icmp_g(1, 2, "ult"))
    code.extend(stack.begin())
    code.extend(mov_imm(8, 10))
    code.extend(stack.otherwise())
    code.extend(mov_imm(8, 20))
    code.extend(stack.pop())
    code.extend(if_icmp_g(3, 4, "ult"))
    code.extend(stack.begin())
    code.extend(alu_imm(8, 8, 1))
    code.extend(stack.otherwise())
    code.extend(alu_imm(8, 8, 2))
    code.extend(stack.pop())
    stack.finish()
    code.extend(store_g(8, 0))
    code.extend(STOP)
    return bytes(code)


def no_else_program() -> bytes:
    stack = MaskStack()
    code = bytearray(prologue(0))
    code.extend(mov_imm(4, 20))
    code.extend(alu_imm(1, 0, 0))
    code.extend(mov_imm(2, 4))
    code.extend(if_icmp_g(1, 2, "ult"))
    code.extend(stack.begin())
    code.extend(mov_imm(4, 11))
    code.extend(stack.pop())
    stack.finish()
    code.extend(alu_imm(5, 4, 100))
    code.extend(store_g(5, 0))
    code.extend(STOP)
    return bytes(code)


def empty_then_program() -> bytes:
    stack = MaskStack()
    code = bytearray(prologue(0))
    code.extend(mov_imm(4, 11))
    code.extend(alu_imm(1, 0, 0))
    code.extend(mov_imm(2, 4))
    code.extend(if_icmp_g(1, 2, "ult"))
    code.extend(stack.begin())
    code.extend(stack.otherwise())
    code.extend(mov_imm(4, 22))
    code.extend(stack.pop())
    stack.finish()
    code.extend(alu_imm(5, 4, 100))
    code.extend(store_g(5, 0))
    code.extend(STOP)
    return bytes(code)


def relation_program(relation: str) -> bytes:
    stack = MaskStack()
    code = bytearray(prologue(15))
    code.extend(load_r(0, 15, True))
    code.extend(move_r_to_g(1, 0))
    code.extend(alu_imm(14, 15, 8))
    code.extend(load_r(4, 14, False))
    code.extend(move_r_to_g(2, 4))
    code.extend(if_icmp_g(1, 2, relation))
    code.extend(stack.begin())
    code.extend(mov_imm(4, 11))
    code.extend(stack.otherwise())
    code.extend(mov_imm(4, 22))
    code.extend(stack.pop())
    stack.finish()
    code.extend(alu_imm(5, 4, 100))
    code.extend(store_g(5, 15))
    code.extend(STOP)
    return bytes(code)


def register_survival_program(canary: int) -> bytes:
    available = [reg for reg in range(16) if reg != canary]
    tid = available[0]
    pairs = tuple(zip(available[1:9:2], available[2:10:2]))
    selected = available[9]
    result = available[10]
    stack = MaskStack()
    code = bytearray(prologue(tid))
    code.extend(mov_imm(canary, 77))
    for (lhs, rhs), bound in zip(pairs, (7, 6, 5, 4)):
        code.extend(alu_imm(lhs, tid, 0))
        code.extend(mov_imm(rhs, bound))

    def nested(level: int) -> bytes:
        lhs, rhs = pairs[level]
        region = bytearray(if_icmp_g(lhs, rhs, "ult"))
        region.extend(stack.begin())
        if level == MAX_MEASURED_DEPTH - 1:
            region.extend(mov_imm(selected, 11))
        else:
            region.extend(nested(level + 1))
        region.extend(stack.otherwise())
        region.extend(mov_imm(selected, 22))
        region.extend(stack.pop())
        return bytes(region)

    code.extend(nested(0))
    stack.finish()
    code.extend(alu_imm(result, canary, 1))
    code.extend(store_g(result, tid))
    code.extend(STOP)
    return bytes(code)


def signed_i32(value: int) -> int:
    return value if value < 0x80000000 else value - 0x100000000


def relation_holds(lhs: int, rhs: int, relation: str) -> bool:
    if relation == "ult":
        return lhs < rhs
    if relation == "ugt":
        return lhs > rhs
    if relation == "slt":
        return signed_i32(lhs) < signed_i32(rhs)
    if relation == "sgt":
        return signed_i32(lhs) > signed_i32(rhs)
    raise ValueError(relation)


def execute(harness: pathlib.Path) -> None:
    sys.path.insert(0, str(harness.resolve()))
    import probe_load
    from agx_control_probe import COMPARE

    a = [0, 1, 2, 0xFFFFFFFF, 100, 200, 42, 42]
    b = [0, 0, 0xFFFFFFFF, 0xFFFFFFFF, 101, 199, 42, 43]
    compare_want = [1 if lhs < rhs else 0 for lhs, rhs in zip(a, b)]

    def canary() -> None:
        got = probe_load.run_with_data(COMPARE, a + b, grid=8, words=8)[:8]
        if got != compare_want:
            raise ValueError("CF1 compiler-derived canary failed")

    def run(name: str, code: bytes, data: list[int], want: list[int]) -> None:
        got = probe_load.run_with_data(code, data, grid=8, words=8)[:8]
        if got != want:
            raise ValueError(f"CF1 {name} mismatch: {got} != {want}")

    canary()

    for relation in STRICT_CONDITION:
        want = [111 if relation_holds(lhs, rhs, relation) else 122
                for lhs, rhs in zip(a, b)]
        run(relation, relation_program(relation), a + b, want)

    structured = (
        ("diamond", diamond_program(), [111] * 4 + [122] * 4),
        ("nested", nested_program(), [130] * 4 + [134, 133, 132, 131]),
        ("sequential", sequential_program(),
         [11, 11, 21, 21, 21, 21, 22, 22]),
        ("no-else", no_else_program(), [111] * 4 + [120] * 4),
        ("empty-then", empty_then_program(), [111] * 4 + [122] * 4),
    )
    for name, code, want in structured:
        run(name, code, [], want)

    # If any of four nested mask levels occupied a G register, one of these
    # would lose its canary.
    for reg in range(16):
        run(f"r{reg}-survival", register_survival_program(reg), [], [78] * 8)

    canary()


def main() -> int:
    check_encoder()
    if len(sys.argv) == 2 and sys.argv[1] == "--check-only":
        print("AGX CF1 PASS: mask stack, depth bound, and refusals pinned")
        return 0
    if len(sys.argv) != 2:
        raise SystemExit("usage: agx_cf1_probe.py HARNESS|--check-only")
    execute(pathlib.Path(sys.argv[1]))
    print("AGX CF1 hardware PASS: strict branches, nesting, and G state exact")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
