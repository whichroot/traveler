#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Encode and execute the measured G16X integer compare/select form."""

import pathlib
import sys


STOP = bytes.fromhex("0e000000")
BASE_CONDITION = {
    "ugt": 4,
    "ult": 5,
    "sgt": 6,
    "slt": 7,
    "eq": 7,
}
INVERTED_RELATION = {
    "ule": "ugt",
    "uge": "ult",
    "sle": "sgt",
    "sge": "slt",
    "ne": "eq",
}


class Unmeasured(ValueError):
    pass


def greg(reg: int) -> tuple[str, int]:
    return ("g", reg)


def immediate(value: int) -> tuple[str, int]:
    return ("imm", value)


def selected_operand(value: tuple[str, int], true_side: bool) -> tuple[int, int]:
    kind, item = value
    if kind == "g":
        if not 0 <= item <= 15:
            raise Unmeasured("selected G register is outside measured r0..r15")
        return (0x82 if true_side else 0x80, item * 2)
    if kind == "imm":
        if not 0 <= item <= 127:
            raise Unmeasured("selected immediate is outside measured 0..127")
        return (0x22 if true_side else 0x20, 0x80 | item)
    raise Unmeasured("selected operand must be greg() or immediate()")


def cmpsel_g(dest: int, lhs: int, rhs: int, relation: str,
              when_true: tuple[str, int],
              when_false: tuple[str, int]) -> bytes:
    """Emit `dest = relation(lhs, rhs) ? when_true : when_false`."""
    for name, reg in (("destination", dest), ("left source", lhs),
                      ("right source", rhs)):
        if not 0 <= reg <= 15:
            raise Unmeasured(f"{name} is outside measured r0..r15")

    invert = relation in INVERTED_RELATION
    base = INVERTED_RELATION.get(relation, relation)
    if base not in BASE_CONDITION:
        raise Unmeasured(f"integer relation {relation!r} is not measured")
    if invert:
        when_true, when_false = when_false, when_true

    true_mode, true_value = selected_operand(when_true, True)
    false_mode, false_value = selected_operand(when_false, False)
    if base == "eq":
        true_mode |= 0x04

    return bytes([
        0x02 | (dest << 4),
        (lhs << 1) | 1,
        0x1F,
        (rhs << 1) | 1,
        true_mode,
        true_value,
        BASE_CONDITION[base],
        0x00,
        false_mode,
        false_value,
    ])


def check_encoder() -> None:
    # Each reference is an Apple-compiler capture from a distinct source shape.
    references = (
        (cmpsel_g(0, 0, 2, "ult", immediate(7), immediate(3)),
         "02011f05228705002083"),
        (cmpsel_g(2, 2, 4, "ult", immediate(7), immediate(3)),
         "22051f09228705002083"),
        (cmpsel_g(0, 2, 0, "ult", immediate(7), immediate(3)),
         "02051f01228705002083"),
        (cmpsel_g(0, 2, 0, "eq", immediate(7), immediate(3)),
         "02051f01268707002083"),
        (cmpsel_g(0, 0, 2, "ult", greg(4), greg(3)),
         "02011f05820805008006"),
        (cmpsel_g(0, 0, 2, "ult", greg(4), immediate(3)),
         "02011f05820805002083"),
        (cmpsel_g(0, 0, 2, "ult", immediate(7), greg(4)),
         "02011f05228705008008"),
    )
    for encoded, reference in references:
        if encoded != bytes.fromhex(reference):
            raise ValueError("CF0 encoder differs from compiler reference")

    # Non-strict and not-equal relations are exactly an observed condition with
    # the selected operands exchanged; these pin that inversion lowering.
    if cmpsel_g(1, 0, 2, "ule", immediate(7), immediate(3)) \
            != cmpsel_g(1, 0, 2, "ugt", immediate(3), immediate(7)):
        raise ValueError("unsigned inversion lowering changed")
    if cmpsel_g(1, 0, 2, "ne", greg(4), greg(6)) \
            != cmpsel_g(1, 0, 2, "eq", greg(6), greg(4)):
        raise ValueError("equality inversion lowering changed")

    refusals = (
        lambda: cmpsel_g(16, 0, 2, "ult", immediate(1), immediate(0)),
        lambda: cmpsel_g(0, 16, 2, "ult", immediate(1), immediate(0)),
        lambda: cmpsel_g(0, 0, 2, "bogus", immediate(1), immediate(0)),
        lambda: cmpsel_g(0, 0, 2, "ult", immediate(128), immediate(0)),
        lambda: cmpsel_g(0, 0, 2, "ult", greg(16), immediate(0)),
    )
    for refuse in refusals:
        try:
            refuse()
        except Unmeasured:
            continue
        raise ValueError("CF0 encoder accepted an unmeasured field")


def prologue(reg: int) -> bytes:
    return bytes([0x0C | (reg << 4), 0xA0, 0x10, 0x06])


def mov_imm(reg: int, value: int) -> bytes:
    return bytes([0x0C | (reg << 4), value])


def mov_wide(value: int) -> bytes:
    return bytes([
        0x0C, 0x80 | (value & 0x7F), 0x02,
        ((value >> 25) & 0x7F) << 1,
        ((value >> 7) & 0x0F) << 1,
        ((value >> 11) & 0x03) << 2,
        (value >> 13) & 0xFF,
        (value >> 21) & 0x0F,
    ])


def store_g(value: int, index: int) -> bytes:
    return bytes([
        0xE7, 0x10, 0x54, value * 2, 0x00, index,
        0x20, 0x00, 0x11, 0x00, 0x00, 0x90, 0x11, 0x00,
    ])


def program(dest: int, lhs: int, rhs: int, relation: str,
            when_true: tuple[str, int], when_false: tuple[str, int],
            lhs_value: int, rhs_value: int,
            true_value: int = 71, false_value: int = 39) -> bytes:
    used = {dest, lhs, rhs}
    for kind, reg in (when_true, when_false):
        if kind == "g":
            used.add(reg)
    tid = next(reg for reg in range(15, -1, -1) if reg not in used)

    code = bytearray(prologue(tid))
    if lhs_value > 127:
        if lhs != 0:
            raise ValueError("wide test values use the measured r0 move")
        code.extend(mov_wide(lhs_value))
    else:
        code.extend(mov_imm(lhs, lhs_value))
    code.extend(mov_imm(rhs, rhs_value))
    if when_true[0] == "g":
        code.extend(mov_imm(when_true[1], true_value))
    if when_false[0] == "g":
        code.extend(mov_imm(when_false[1], false_value))
    code.extend(cmpsel_g(dest, lhs, rhs, relation, when_true, when_false))
    code.extend(store_g(dest, tid))
    code.extend(STOP)
    return bytes(code)


def execute(harness: pathlib.Path) -> None:
    sys.path.insert(0, str(harness.resolve()))
    import probe_load
    from agx_control_probe import COMPARE

    canary_a = [0, 1, 2, 0xFFFFFFFF, 100, 200, 42, 42]
    canary_b = [0, 0, 0xFFFFFFFF, 0xFFFFFFFF, 101, 199, 42, 43]
    canary_want = [1 if a < b else 0 for a, b in zip(canary_a, canary_b)]

    def canary() -> None:
        got = probe_load.run_with_data(
            COMPARE, canary_a + canary_b, grid=8, words=8)[:8]
        if got != canary_want:
            raise ValueError("CF0 compare canary failed")

    def run(code: bytes, want: int) -> None:
        got = probe_load.run_with_data(code, [], grid=8, words=8)[:8]
        if got != [want] * 8:
            raise ValueError(f"CF0 hardware mismatch: {got} != {[want] * 8}")

    canary()

    relation_cases = (
        ("ult", 3, 5, 7), ("ule", 5, 5, 7),
        ("ugt", 5, 3, 7), ("uge", 5, 5, 7),
        ("slt", 3, 5, 7), ("sle", 5, 5, 7),
        ("sgt", 5, 3, 7), ("sge", 5, 5, 7),
        ("eq", 5, 5, 7), ("ne", 5, 3, 7),
    )
    for relation, lhs_value, rhs_value, want in relation_cases:
        run(program(4, 0, 2, relation, immediate(7), immediate(3),
                    lhs_value, rhs_value), want)

    signed_cases = (
        ("ult", 0xFFFFFFFF, 0, 3),
        ("ugt", 0xFFFFFFFF, 0, 7),
        ("slt", 0xFFFFFFFF, 0, 7),
        ("sgt", 0xFFFFFFFF, 0, 3),
        ("slt", 0x80000000, 1, 7),
    )
    for relation, lhs_value, rhs_value, want in signed_cases:
        run(program(4, 0, 2, relation, immediate(7), immediate(3),
                    lhs_value, rhs_value), want)

    select_cases = (
        (greg(6), greg(8), 3, 5, 71),
        (greg(6), greg(8), 5, 3, 39),
        (greg(6), immediate(127), 3, 5, 71),
        (greg(6), immediate(127), 5, 3, 127),
        (immediate(127), greg(8), 3, 5, 127),
        (immediate(127), greg(8), 5, 3, 39),
    )
    for when_true, when_false, lhs_value, rhs_value, want in select_cases:
        run(program(4, 0, 2, "ult", when_true, when_false,
                    lhs_value, rhs_value), want)

    # Compiler captures establish r0/r2/r4. Hardware mutations establish the
    # top of every compact CF0 register field independently.
    register_cases = (
        (15, 2, 4, greg(6), greg(8), 3, 5, 71),
        (8, 15, 2, greg(4), greg(6), 3, 5, 71),
        (8, 2, 15, greg(4), greg(6), 3, 5, 71),
        (8, 2, 4, greg(15), greg(6), 3, 5, 71),
        (8, 2, 4, greg(6), greg(15), 5, 3, 39),
    )
    for dest, lhs, rhs, when_true, when_false, lhs_value, rhs_value, want \
            in register_cases:
        run(program(dest, lhs, rhs, "ult", when_true, when_false,
                    lhs_value, rhs_value), want)

    canary()


def main() -> int:
    check_encoder()
    if len(sys.argv) == 2 and sys.argv[1] == "--check-only":
        print("AGX CF0 PASS: compare/select fields and refusals pinned")
        return 0
    if len(sys.argv) != 2:
        raise SystemExit("usage: agx_cf0_probe.py HARNESS|--check-only")
    execute(pathlib.Path(sys.argv[1]))
    print("AGX CF0 hardware PASS: relations, inversion, operands, and r15 exact")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
