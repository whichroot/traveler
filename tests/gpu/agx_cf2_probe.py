#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Encode and execute the measured G16X loop and local-exit forms."""

import pathlib
import sys

import agx_cf1_probe as cf1
import agx_control_probe


LOOP_INITIAL = bytes.fromhex("0a8122090522")
ACC_ZERO = bytes.fromhex("1b000000")
LOOP_TOP = bytes.fromhex("0f05541a")
LOOP_INC = bytes.fromhex("9f015400030200881504")
LOOP_CMP = bytes.fromhex("0a0122050500")
LOOP_ADD_LOAD = bytes.fromhex("9f015602020c08a81705")
MAX_LOOP_DEPTH = 2


class Unmeasured(ValueError):
    pass


def constant_trips(trips: int, maximum: int) -> None:
    if not 1 <= trips <= maximum:
        raise Unmeasured(f"trip count is outside measured 1..{maximum}")


def forward_skip(origin: int, target: int) -> bytes:
    """Emit `jmp_exec_none target`; its offset base is the instruction start."""
    offset = target - origin
    if not 0 < offset <= 0xFFFFFFFF:
        raise Unmeasured("forward skip target is outside measured u32 reach")
    return bytes.fromhex("0f0154") + offset.to_bytes(4, "little") + b"\0\0\0"


def backedge(origin: int, target: int, depth: int = 0,
             dynamic: bool = False) -> bytes:
    """Emit a loop backedge with the measured depth and dynamic-mode fields."""
    if not 0 <= depth <= MAX_LOOP_DEPTH:
        raise Unmeasured("loop depth is outside measured 0..2")
    offset = target - origin
    if not -(1 << 47) <= offset < 0:
        raise Unmeasured("backedge target is outside measured signed-48 reach")
    selector = 0x02 + depth * 4 + (0x20 if dynamic else 0)
    return bytes([0x8F, 0x04, 0x54, selector, 0x0F, 0x00, 0x54]) + \
        (offset & 0xFFFFFFFFFFFF).to_bytes(6, "little") + b"\0"


def pop_exec(nest: int) -> bytes:
    if nest not in (1, 2):
        raise Unmeasured("pop nesting is outside measured mask/loop forms")
    return bytes([0x0F, 0x06, 0x04, nest, 0x00, 0x00])


MASK_POP = pop_exec(1)
LOOP_POP = pop_exec(2)


def loop_bound(trips: int, dest: int = 0x04, mode: int = 0x03,
               nested: bool = False) -> bytes:
    return bytes([
        0x9F, 0x01, 0x54, dest, mode, trips * 2,
        0x00, 0x8C if nested else 0x88, 0x11, 0x04,
    ])


def loop_load() -> bytes:
    return bytes.fromhex("6700040601002000510100404600")


def loop_store(value: int, index: int) -> bytes:
    return bytes([
        0xE7, 0x10, 0x54, value * 2, 0x00, index,
        0x21, 0x00, 0x11, 0x00, 0x00, 0x90, 0x11, 0x00,
    ])


def sum_program(trips: int) -> bytes:
    constant_trips(trips, 31)
    code = bytearray(cf1.prologue(0))
    code.extend(LOOP_INITIAL)
    code.extend(ACC_ZERO)
    code.extend(cf1.MASK_BEGIN)
    skip = len(code)
    code.extend(forward_skip(skip, skip + 1))
    code.extend(loop_bound(trips))
    top = len(code)
    code.extend(LOOP_TOP)
    code.extend(loop_load())
    code.extend(LOOP_INC)
    code.extend(LOOP_CMP)
    code.extend(LOOP_ADD_LOAD)
    branch = len(code)
    code.extend(backedge(branch, top))
    code.extend(LOOP_POP)
    final_pop = len(code)
    code.extend(MASK_POP)
    code.extend(cf1.prologue(0))
    code.extend(loop_store(1, 0))
    code.extend(cf1.STOP)
    code[skip:skip + 10] = forward_skip(skip, final_pop)
    return bytes(code)


def nested_sum_program(trips: int) -> bytes:
    constant_trips(trips, 16)
    code = bytearray(cf1.prologue(0))
    code.extend(LOOP_INITIAL)
    code.extend(ACC_ZERO)
    code.extend(cf1.MASK_BEGIN)
    skip = len(code)
    code.extend(forward_skip(skip, skip + 1))
    code.extend(loop_bound(trips, nested=True))
    code.extend(bytes.fromhex("3b802100"))
    outer_top = len(code)
    code.extend(LOOP_TOP)
    code.extend(bytes.fromhex(
        "9f01240c0302188c1104"
        "0a0d22850500"
        "7b002100"
    ))
    inner_top = len(code)
    code.extend(LOOP_TOP)
    code.extend(bytes.fromhex(
        "9f01240a021c1aa81105"
        "6700440a01852000510100404600"
        "9f01540e030238881504"
        "2a0f22050500"
        "9f015602021408a81705"
    ))
    inner_branch = len(code)
    code.extend(backedge(inner_branch, inner_top, depth=1))
    code.extend(LOOP_POP)
    code.extend(bytes.fromhex("3b0c0900"))
    outer_branch = len(code)
    code.extend(backedge(outer_branch, outer_top))
    code.extend(LOOP_POP)
    final_pop = len(code)
    code.extend(MASK_POP)
    code.extend(cf1.prologue(0))
    code.extend(loop_store(1, 0))
    code.extend(cf1.STOP)
    code[skip:skip + 10] = forward_skip(skip, final_pop)
    return bytes(code)


def triple_sum_program(trips: int) -> bytes:
    constant_trips(trips, 9)
    code = bytearray(cf1.prologue(0))
    code.extend(LOOP_INITIAL)
    code.extend(ACC_ZERO)
    code.extend(cf1.MASK_BEGIN)
    skip = len(code)
    code.extend(forward_skip(skip, skip + 1))
    code.extend(loop_bound(trips, nested=True))
    code.extend(bytes.fromhex("3b802100"))
    outer_top = len(code)
    code.extend(LOOP_TOP)
    code.extend(bytes.fromhex(
        "9f01040c0302188c1104"
        "0a0d22850500"
        "7b802100"
    ))
    middle_top = len(code)
    code.extend(LOOP_TOP)
    code.extend(bytes.fromhex(
        "9f01040a031c1aa81105"
        "9f01640e030238881504"
        "2a0f22850500"
        "9b002100"
    ))
    inner_top = len(code)
    code.extend(LOOP_TOP)
    code.extend(bytes.fromhex(
        "9f012410021448ac1105"
        "6700441001882000510100404600"
        "9f015412030248881504"
        "4a1322050500"
        "9f015602022008a81705"
    ))
    inner_branch = len(code)
    code.extend(backedge(inner_branch, inner_top, depth=2))
    code.extend(LOOP_POP)
    middle_branch = len(code)
    code.extend(backedge(middle_branch, middle_top, depth=1))
    code.extend(LOOP_POP)
    code.extend(bytes.fromhex("3b0c0900"))
    outer_branch = len(code)
    code.extend(backedge(outer_branch, outer_top))
    code.extend(LOOP_POP)
    final_pop = len(code)
    code.extend(MASK_POP)
    code.extend(cf1.prologue(0))
    code.extend(loop_store(1, 0))
    code.extend(cf1.STOP)
    code[skip:skip + 10] = forward_skip(skip, final_pop)
    return bytes(code)


def divergent_program() -> bytes:
    code = bytearray(cf1.prologue(0))
    code.extend(bytes.fromhex("0a8123800600072200001b0000000f055421"))
    skip = len(code)
    code.extend(forward_skip(skip, skip + 1))
    code.extend(bytes.fromhex("2b000000"))
    top = len(code)
    code.extend(LOOP_TOP)
    code.extend(bytes.fromhex(
        "6700040601022000510100404600"
        "9f015404030210881504"
        "0a052301060007000000"
        "9f015602020c08a81705"
    ))
    branch = len(code)
    code.extend(backedge(branch, top, dynamic=True))
    code.extend(LOOP_POP)
    final_pop = len(code)
    code.extend(MASK_POP)
    code.extend(bytes.fromhex(
        "e700540200002100110000901100"
        "0e000000"
    ))
    code[skip:skip + 10] = forward_skip(skip, final_pop)
    return bytes(code)


def if_in_loop_program(trips: int) -> bytes:
    constant_trips(trips, 16)
    code = bytearray(cf1.prologue(0))
    code.extend(LOOP_INITIAL)
    code.extend(ACC_ZERO)
    code.extend(cf1.MASK_BEGIN)
    skip = len(code)
    code.extend(forward_skip(skip, skip + 1))
    code.extend(loop_bound(trips))
    top = len(code)
    code.extend(LOOP_TOP)
    code.extend(bytes.fromhex(
        "6700040601002000510100404600"
        "9f015400030200881504"
        "0a0122050500"
        "32072fe4208105c2"
        "9f015402020c08a81705"
    ))
    branch = len(code)
    code.extend(backedge(branch, top))
    code.extend(LOOP_POP)
    final_pop = len(code)
    code.extend(MASK_POP)
    code.extend(cf1.prologue(0))
    code.extend(loop_store(1, 0))
    code.extend(cf1.STOP)
    code[skip:skip + 10] = forward_skip(skip, final_pop)
    return bytes(code)


def loop_carried_program(trips: int) -> bytes:
    constant_trips(trips, 16)
    code = bytearray(cf1.prologue(0))
    code.extend(LOOP_INITIAL)
    code.extend(bytes.fromhex("1c82220000000000"))
    code.extend(cf1.MASK_BEGIN)
    skip = len(code)
    code.extend(forward_skip(skip, skip + 1))
    code.extend(bytes.fromhex("1c03"))
    code.extend(loop_bound(trips, dest=0x0A))
    code.extend(bytes.fromhex("2c01"))
    top = len(code)
    code.extend(LOOP_TOP)
    code.extend(bytes.fromhex(
        "6700040601002000510100404600"
        "9f015400030200881504"
        "0a01220b0500"
        "9f015604030c10a81705"
        "9f01540203080aa81505"
    ))
    branch = len(code)
    code.extend(backedge(branch, top))
    code.extend(LOOP_POP)
    code.extend(bytes.fromhex("1b031b05"))
    final_pop = len(code)
    code.extend(MASK_POP)
    code.extend(cf1.prologue(0))
    code.extend(loop_store(1, 0))
    code.extend(cf1.STOP)
    code[skip:skip + 10] = forward_skip(skip, final_pop)
    return bytes(code)


def loop_in_if_program(trips: int) -> bytes:
    constant_trips(trips, 16)
    code = bytearray(bytes.fromhex(
        "0ca01006"
        "9f115404021000881104"
        "6700540201002000510100404600"
        "6700540401822000510100404600"
        "0a033a0505c0"
        "1ccd220000000000"
    ))
    code.extend(cf1.MASK_BEGIN)
    outer_skip = len(code)
    code.extend(forward_skip(outer_skip, outer_skip + 1))
    code.extend(bytes.fromhex("0a81220905021b000000"))
    code.extend(cf1.MASK_BEGIN)
    inner_skip = len(code)
    code.extend(forward_skip(inner_skip, inner_skip + 1))
    code.extend(loop_bound(trips, nested=True))
    code.extend(bytes.fromhex("3b002100"))
    top = len(code)
    code.extend(LOOP_TOP)
    code.extend(bytes.fromhex(
        "9f01240a0220188c1104"
        "6700440a01852000510100404600"
        "9f015406030218881504"
        "0a0722050500"
        "9f015602021408a81705"
    ))
    branch = len(code)
    code.extend(backedge(branch, top))
    code.extend(LOOP_POP)
    final_pop = len(code)
    code.extend(LOOP_POP)
    code.extend(bytes.fromhex(
        "e700540200002100110000901100"
        "0e000000"
    ))
    code[outer_skip:outer_skip + 10] = forward_skip(outer_skip, final_pop)
    code[inner_skip:inner_skip + 10] = forward_skip(inner_skip, final_pop)
    return bytes(code)


def exit_program(kind: str, trips: int, has_device_store: bool = False) -> bytes:
    constant_trips(trips, 16)
    if has_device_store:
        raise Unmeasured("device stores inside exit-bearing loops are unmeasured")
    if kind not in ("continue", "break"):
        raise Unmeasured(f"local exit kind {kind!r} is not measured")

    code = bytearray(cf1.prologue(0))
    code.extend(LOOP_INITIAL)
    code.extend(ACC_ZERO)
    code.extend(cf1.MASK_BEGIN)
    skip = len(code)
    code.extend(forward_skip(skip, skip + 1))
    code.extend(loop_bound(trips, mode=0x02))
    top = len(code)
    code.extend(LOOP_TOP)
    code.extend(loop_load())
    code.extend(bytes.fromhex("9f01560a030c1aec1005"))
    if kind == "continue":
        code.extend(bytes.fromhex(
            "9f01540c030c0aac1105"
            "9f01540a031400ac1305"
            "9f015400030200881504"
            "9f01540c030230881504"
            "0a0122050500"
        ))
    else:
        code.extend(bytes.fromhex(
            "9f01540a031400ac1305"
            "9f015400030200881504"
            "9f01540c030c0aac1105"
            "42012505228105002080"
            "9f01540c030230881504"
            "1a870380268007028008"
        ))
    code.extend(bytes.fromhex(
        "5b0b3b0d"
        "9f01540c030c1aac1015"
        "9f01540a031430a81705"
        "9f01540a030e28881504"
        "12070f80840a0702"
    ))
    branch = len(code)
    code.extend(backedge(branch, top))
    code.extend(LOOP_POP)
    final_pop = len(code)
    code.extend(MASK_POP)
    code.extend(cf1.prologue(0))
    code.extend(loop_store(1, 0))
    code.extend(cf1.STOP)
    code[skip:skip + 10] = forward_skip(skip, final_pop)
    return bytes(code)


NESTED_REFERENCE = bytes.fromhex(
    "0ca010060a81220905221b0000000f0554010f015492000000000000"
    "9f0154040312008c11043b8021000f05541a9f01240c0302188c1104"
    "0a0d228505007b0021000f05541a9f01240a021c1aa811056700440a"
    "018520005101004046009f01540e0302388815042a0f220505009f01"
    "5602021408a817058f0454060f0054caffffffffff000f0604020000"
    "3b0c09008f0454020f00549affffffffff000f06040200000f060401"
    "00000ca01006e7105402000021001100009011000e000000"
)

TRIPLE_REFERENCE = bytes.fromhex(
    "0ca010060a81220905221b0000000f0554010f0154c8000000000000"
    "9f0154040312008c11043b8021000f05541a9f01040c0302188c1104"
    "0a0d228505007b8021000f05541a9f01040a031c1aa811059f01640e"
    "0302388815042a0f228505009b0021000f05541a9f012410021448ac"
    "110567004410018820005101004046009f0154120302488815044a13"
    "220505009f015602022008a817058f04540a0f0054caffffffffff00"
    "0f06040200008f0454060f005494ffffffffff000f06040200003b0c"
    "09008f0454020f005464ffffffffff000f06040200000f0604010000"
    "0ca01006e7105402000021001100009011000e000000"
)

IF_IN_LOOP_REFERENCE = bytes.fromhex(
    "0ca010060a81220905221b0000000f0554010f01545c000000000000"
    "9f0154040312008811040f05541a6700040601002000510100404600"
    "9f0154000302008815040a012205050032072fe4208105c29f015402"
    "020c08a817058f0454020f0054ccffffffffff000f06040200000f06"
    "040100000ca01006e7105402000021001100009011000e000000"
)

LOOP_CARRIED_REFERENCE = bytes.fromhex(
    "0ca010060a81220905221c822200000000000f0554010f0154660000"
    "000000001c039f01540a0312008811042c010f05541a670004060100"
    "20005101004046009f0154000302008815040a01220b05009f015604"
    "030c10a817059f01540203080aa815058f0454020f0054caffffffff"
    "ff000f06040200001b031b050f06040100000ca01006e71054020000"
    "21001100009011000e000000"
)

LOOP_IN_IF_REFERENCE = bytes.fromhex(
    "0ca010069f1154040210008811046700540201002000510100404600"
    "67005404018220005101004046000a033a0505c01ccd220000000000"
    "0f0554010f01547a0000000000000a81220905021b0000000f055401"
    "0f0154620000000000009f0154040312008c11043b0021000f05541a"
    "9f01240a0220188c11046700440a018520005101004046009f015406"
    "0302188815040a07220505009f015602021408a817058f0454020f00"
    "54caffffffffff000f06040200000f0604020000e700540200002100"
    "1100009011000e000000"
)

CONTINUE_REFERENCE = bytes.fromhex(
    "0ca010060a81220905221b0000000f0554010f01549c000000000000"
    "9f0154040220008811040f05541a6700040601002000510100404600"
    "9f01560a030c1aec10059f01540c030c0aac11059f01540a031400ac"
    "13059f0154000302008815049f01540c0302308815040a0122050500"
    "5b0b3b0d9f01540c030c1aac10159f01540a031430a817059f01540a"
    "030e2888150412070f80840a07028f0454020f00548cffffffffff00"
    "0f06040200000f06040100000ca01006e71054020000210011000090"
    "11000e000000"
)

BREAK_REFERENCE = bytes.fromhex(
    "0ca010060a81220905221b0000000f0554010f0154aa000000000000"
    "9f0154040220008811040f05541a6700040601002000510100404600"
    "9f01560a030c1aec10059f01540a031400ac13059f01540003020088"
    "15049f01540c030c0aac1105420125052281050020809f01540c0302"
    "308815041a8703802680070280085b0b3b0d9f01540c030c1aac1015"
    "9f01540a031430a817059f01540a030e2888150412070f80840a0702"
    "8f0454020f00547effffffffff000f06040200000f06040100000ca0"
    "1006e7105402000021001100009011000e000000"
)


def check_encoder() -> None:
    references = (
        (sum_program(9), agx_control_probe.LOOP9),
        (nested_sum_program(9), NESTED_REFERENCE),
        (triple_sum_program(9), TRIPLE_REFERENCE),
        (divergent_program(), agx_control_probe.LOOP_DIVERGENT),
        (if_in_loop_program(9), IF_IN_LOOP_REFERENCE),
        (loop_carried_program(9), LOOP_CARRIED_REFERENCE),
        (loop_in_if_program(9), LOOP_IN_IF_REFERENCE),
        (exit_program("continue", 16), CONTINUE_REFERENCE),
        (exit_program("break", 16), BREAK_REFERENCE),
    )
    for encoded, reference in references:
        if encoded != reference:
            raise ValueError("CF2 encoder differs from compiler reference")

    field_references = (
        (forward_skip(18, 102), "0f015454000000000000"),
        (backedge(82, 38), "8f0454020f0054d4ffffffffff00"),
        (backedge(120, 66, depth=1),
         "8f0454060f0054caffffffffff00"),
        (backedge(154, 100, depth=2),
         "8f04540a0f0054caffffffffff00"),
        (backedge(84, 36, dynamic=True),
         "8f0454220f0054d0ffffffffff00"),
        (pop_exec(1), "0f0604010000"),
        (pop_exec(2), "0f0604020000"),
    )
    for encoded, reference in field_references:
        if encoded != bytes.fromhex(reference):
            raise ValueError("CF2 control field differs from compiler reference")

    refusals = (
        lambda: forward_skip(10, 10),
        lambda: forward_skip(0, 1 << 32),
        lambda: backedge(10, 11),
        lambda: backedge((1 << 47) + 1, 0),
        lambda: backedge(100, 50, depth=3),
        lambda: pop_exec(0),
        lambda: sum_program(0),
        lambda: triple_sum_program(10),
        lambda: exit_program("return", 8),
        lambda: exit_program("continue", 8, has_device_store=True),
    )
    for refuse in refusals:
        try:
            refuse()
        except Unmeasured:
            continue
        raise ValueError("CF2 encoder accepted an unmeasured field")

    expected_lengths = (130, 192, 246, 128, 138, 152, 206, 202, 216)
    actual_lengths = tuple(len(encoded) for encoded, _ in references)
    if actual_lengths != expected_lengths:
        raise ValueError(f"CF2 program lengths changed: {actual_lengths}")


def execute(harness: pathlib.Path) -> None:
    sys.path.insert(0, str(harness.resolve()))
    import probe_load
    from agx_control_probe import COMPARE

    canary_a = [0, 1, 2, 0xFFFFFFFF, 100, 200, 42, 42]
    canary_b = [0, 0, 0xFFFFFFFF, 0xFFFFFFFF, 101, 199, 42, 43]
    canary_want = [1 if lhs < rhs else 0
                   for lhs, rhs in zip(canary_a, canary_b)]

    def canary() -> None:
        got = probe_load.run_with_data(
            COMPARE, canary_a + canary_b, grid=8, words=8)[:8]
        if got != canary_want:
            raise ValueError("CF2 compiler-derived canary failed")

    def run(name: str, code: bytes, data: list[int], want: list[int]) -> None:
        got = probe_load.run_with_data(code, data, grid=8, words=8)[:8]
        if got != want:
            raise ValueError(f"CF2 {name} mismatch: {got} != {want}")

    canary()

    for trips in (1, 2, 8, 16, 31):
        data = [100 + i for i in range(8 + trips)]
        want = [sum(data[lane:lane + trips]) for lane in range(8)]
        run(f"sum-{trips}", sum_program(trips), data, want)

    for trips in (1, 2, 8, 16):
        data = [10 + i for i in range(2 * (7 + trips - 1) + 1)]
        want = [sum(data[a + b]
                    for a in range(lane, lane + trips)
                    for b in range(lane, lane + trips))
                for lane in range(8)]
        run(f"nested-{trips}", nested_sum_program(trips), data, want)

    for trips in (1, 2, 4, 9):
        data = [10 + i for i in range(3 * (7 + trips - 1) + 1)]
        want = [sum(data[a + b + c]
                    for a in range(lane, lane + trips)
                    for b in range(lane, lane + trips)
                    for c in range(lane, lane + trips))
                for lane in range(8)]
        run(f"triple-{trips}", triple_sum_program(trips), data, want)

    data = [100 + i for i in range(8)]
    run("divergent-zero", divergent_program(), data,
        [sum(data[:lane]) for lane in range(8)])

    mixed = [50, 150, 99, 100, 7, 700, 42, 142, 13, 113, 88, 188,
             1, 101, 77, 177, 20, 120, 90, 190, 3, 103, 60, 160]
    for trips in (1, 2, 8, 16):
        data = mixed[:8 + trips]
        want = [sum(value if value < 100 else 1
                    for value in data[lane:lane + trips])
                for lane in range(8)]
        run(f"if-in-loop-{trips}", if_in_loop_program(trips), data, want)

    for trips in (1, 2, 8, 16):
        data = [10 + i for i in range(8 + trips)]
        want = []
        for lane in range(8):
            first, second = 1, 3
            for value in data[lane:lane + trips]:
                first = (first + value) & 0xFFFFFFFF
                second = (second + first) & 0xFFFFFFFF
            want.append(first ^ second)
        run(f"carried-{trips}", loop_carried_program(trips), data, want)

    cond_a = [0, 2, 4, 6, 10, 12, 14, 16]
    cond_b = [1, 3, 5, 7, 9, 11, 13, 15]
    for trips in (1, 2, 8, 16):
        payload = [100 + i for i in range(8 + trips)]
        data = cond_a + cond_b + payload
        want = [sum(data[lane + 16:lane + 16 + trips])
                if cond_a[lane] < cond_b[lane] else 77
                for lane in range(8)]
        run(f"loop-in-if-{trips}", loop_in_if_program(trips), data, want)

    exit_data = [5, 7, 0, 11, 13, 0, 17, 19, 23, 29, 0, 31,
                 37, 41, 43, 0, 47, 53, 59, 61, 0, 67, 71, 73]

    def exit_body(total: int, value: int, index: int) -> int:
        total = (total + value + 1) & 0xFFFFFFFF
        total ^= (value * 3 + index) & 0xFFFFFFFF
        return (total + value * 5 + 7) & 0xFFFFFFFF

    for trips in (1, 2, 8, 16):
        data = exit_data[:8 + trips]
        continued = []
        broken = []
        for lane in range(8):
            continue_value = 0
            break_value = 0
            stopped = False
            for offset, value in enumerate(data[lane:lane + trips]):
                if value != 0:
                    continue_value = exit_body(
                        continue_value, value, lane + offset)
                if not stopped:
                    if value == 0:
                        stopped = True
                    else:
                        break_value = exit_body(
                            break_value, value, lane + offset)
            continued.append(continue_value)
            broken.append(break_value)
        run(f"continue-{trips}", exit_program("continue", trips),
            data, continued)
        run(f"break-{trips}", exit_program("break", trips), data, broken)

    canary()


def main() -> int:
    check_encoder()
    if len(sys.argv) == 2 and sys.argv[1] == "--check-only":
        print("AGX CF2 PASS: loops, fixups, exits, and refusals pinned")
        return 0
    if len(sys.argv) != 2:
        raise SystemExit("usage: agx_cf2_probe.py HARNESS|--check-only")
    execute(pathlib.Path(sys.argv[1]))
    print("AGX CF2 hardware PASS: nesting, divergence, carried values, and exits exact")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
