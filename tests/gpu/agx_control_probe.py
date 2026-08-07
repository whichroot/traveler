#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Execute the compiler-derived G16X compare/select/control specimens."""

import pathlib
import sys


COMPARE = bytes.fromhex(
    "0ca010069f115404021000881104"
    "6700540201002000510100404600"
    "6700440401822000510100404600"
    "12031d05228105c0208013000001"
    "e7005402000021001100009011000e000000"
)

SELECT = bytes.fromhex(
    "1ca010069f115404021008881104"
    "6700540001012000510100404600"
    "6700540401822000510100404600"
    "02013f05229005c02098"
    "9f015400020008a81305"
    "6700440001802000510100404600"
    "e7005600000121001100009011000e000000"
)

# volatile if (a < b) out=t+1; else out=f+2; post[i]=a+100
FORWARD_REJOIN = bytes.fromhex(
    "0ca010069f115404021000881104"
    "6700540201002000510100404600"
    "6700540401822000510100404600"
    "0a03320505c00f055401"
    "9f0154040220008811046700540401822000510100404600"
    "9f015604020210881504e700540400002000510000901100"
    "0f042419"
    "9f0154040230008811046700440401822000510100404600"
    "9f015604020410881504e700540400002000510000901100"
    "0f0604010000"
    "9f01540202c8088815049f015400024000881504"
    "e7005402000021001100009011000e000000"
)

# for (k=i; k<i+9; ++k) s += w[k]
LOOP9 = bytes.fromhex(
    "0ca010060a81220905221b0000000f055401"
    "0f0154540000000000009f0154040312008811040f05541a"
    "6700040601002000510100404600"
    "9f0154000302008815040a0122050500"
    "9f015602020c08a81705"
    "8f0454020f0054d4ffffffffff00"
    "0f06040200000f06040100000ca01006"
    "e7105402000021001100009011000e000000"
)

# The compiler-derived trip-9/16/31 blocks differ only at byte 33, whose value
# is trip_count*2. This is the first authored control mutation: trip 8.
LOOP8_AUTHORED = LOOP9[:33] + bytes([16]) + LOOP9[34:]

# Eight authored straight-line loads reuse return slot 0 only after moving its
# prior value to G registers. Three accumulator registers rotate because ALU
# sources are consuming.
REUSE8_AUTHORED = bytes.fromhex(
    "1ca010066710540001012000510100404600"
    "a700560402000000f0110100"
    "1ca010066710540001012000510100404600"
    "a700560602000000f01101009f115408020c10a81705"
    "1ca010066710540001012000510100404600"
    "a700560402000000f01101009f115406020820a81705"
    "1ca010066710540001012000510100404600"
    "a700560802000000f01101009f115404021018a81705"
    "1ca010066710540001012000510100404600"
    "a700560602000000f01101009f115408020c10a81705"
    "1ca010066710540001012000510100404600"
    "a700560402000000f01101009f115406020820a81705"
    "1ca010066710540001012000510100404600"
    "a700560802000000f01101009f115404021018a81705"
    "1ca010066710540001012000510100404600"
    "a700560602000000f01101009f115408020c10a81705"
    "1ca01006e7105408000120001100009011000e000000"
)

# for (k=0; k<i; ++k) s += w[k], so lanes take 0..7 trips.
LOOP_DIVERGENT = bytes.fromhex(
    "0ca010060a8123800600072200001b0000000f055421"
    "0f0154520000000000002b0000000f05541a"
    "6700040601022000510100404600"
    "9f0154040302108815040a052301060007000000"
    "9f015602020c08a81705"
    "8f0454220f0054d0ffffffffff00"
    "0f06040200000f0604010000"
    "e7005402000021001100009011000e000000"
)


def check_specimens() -> None:
    expected_lengths = (74, 94, 196, 130, 130, 332, 128)
    actual_lengths = tuple(map(len, (
        COMPARE, SELECT, FORWARD_REJOIN, LOOP9, LOOP8_AUTHORED,
        REUSE8_AUTHORED, LOOP_DIVERGENT,
    )))
    if actual_lengths != expected_lengths:
        raise ValueError(f"control specimen lengths changed: {actual_lengths}")
    for marker in (bytes.fromhex("0f055401"), bytes.fromhex("0f042419"),
                   bytes.fromhex("0f0604010000")):
        if marker not in FORWARD_REJOIN:
            raise ValueError("forward-control marker absent")
    loop_top = LOOP9.index(bytes.fromhex("0f05541a"))
    branch_lead = bytes.fromhex("8f0454020f0054")
    branch = LOOP9.index(branch_lead)
    offset = int.from_bytes(LOOP9[branch + 7:branch + 13], "little",
                            signed=True)
    if loop_top != 38 or branch != 82 or offset != loop_top - branch:
        raise ValueError("backward-branch base contract changed")
    changed = [i for i in range(len(LOOP9))
               if LOOP9[i] != LOOP8_AUTHORED[i]]
    if changed != [33] or LOOP9[33] != 18 or LOOP8_AUTHORED[33] != 16:
        raise ValueError("authored trip-8 bound is not the one-field mutation")


def execute(harness: pathlib.Path) -> None:
    sys.path.insert(0, str(harness.resolve()))
    import probe_load

    a = [0, 1, 2, 0xFFFFFFFF, 100, 200, 42, 42]
    b = [0, 0, 0xFFFFFFFF, 0xFFFFFFFF, 101, 199, 42, 43]
    want_compare = [1 if x < y else 0 for x, y in zip(a, b)]
    got = probe_load.run_with_data(COMPARE, a + b, grid=8, words=8)[:8]
    if got != want_compare:
        raise ValueError(f"compare mismatch: {got} != {want_compare}")

    true_values = [1000 + i for i in range(8)]
    false_values = [2000 + i for i in range(8)]
    want_select = [true_values[i] if a[i] < b[i] else false_values[i]
                   for i in range(8)]
    got = probe_load.run_with_data(
        SELECT, a + b + true_values + false_values, grid=8, words=8,
    )[:8]
    if got != want_select:
        raise ValueError(f"select mismatch: {got} != {want_select}")

    want_branch = [true_values[i] + 1 if a[i] < b[i]
                   else false_values[i] + 2 for i in range(8)]
    want_branch += [0] * 24
    want_branch += [(value + 100) & 0xFFFFFFFF for value in a]
    got = probe_load.run_with_data(
        FORWARD_REJOIN, a + b + true_values + false_values,
        grid=8, words=40,
    )[:40]
    if got != want_branch:
        raise ValueError("forward branch/rejoin mismatch")

    words = [100 + i for i in range(17)]
    want_loop9 = [sum(words[i:i + 9]) for i in range(8)]
    got = probe_load.run_with_data(LOOP9, words, grid=8, words=8)[:8]
    if got != want_loop9:
        raise ValueError(f"loop9 mismatch: {got} != {want_loop9}")

    # Fresh-process pre-canary, one authored mutation, fresh-process post-canary.
    got = probe_load.run_with_data(COMPARE, a + b, grid=8, words=8)[:8]
    if got != want_compare:
        raise ValueError("trip-8 pre-canary failed")
    words = [100 + i for i in range(16)]
    want_loop8 = [sum(words[i:i + 8]) for i in range(8)]
    got = probe_load.run_with_data(
        LOOP8_AUTHORED, words, grid=8, words=8,
    )[:8]
    if got != want_loop8:
        raise ValueError(f"authored loop8 mismatch: {got} != {want_loop8}")
    got = probe_load.run_with_data(COMPARE, a + b, grid=8, words=8)[:8]
    if got != want_compare:
        raise ValueError("trip-8 post-canary failed")

    got = probe_load.run_with_data(COMPARE, a + b, grid=8, words=8)[:8]
    if got != want_compare:
        raise ValueError("reuse-8 pre-canary failed")
    words = [3, 5, 7, 11, 13, 17, 19, 23]
    want_reuse = [8 * value for value in words]
    got = probe_load.run_with_data(
        REUSE8_AUTHORED, words, grid=8, words=8,
    )[:8]
    if got != want_reuse:
        raise ValueError(f"authored reuse8 mismatch: {got} != {want_reuse}")
    got = probe_load.run_with_data(COMPARE, a + b, grid=8, words=8)[:8]
    if got != want_compare:
        raise ValueError("reuse-8 post-canary failed")

    words = [100 + i for i in range(8)]
    want_divergent = [sum(words[:i]) for i in range(8)]
    got = probe_load.run_with_data(
        LOOP_DIVERGENT, words, grid=8, words=8,
    )[:8]
    if got != want_divergent:
        raise ValueError(f"divergent loop mismatch: {got} != {want_divergent}")


def main() -> int:
    check_specimens()
    if len(sys.argv) == 2 and sys.argv[1] == "--check-only":
        print("AGX control specimens PASS: structure and branch base pinned")
        return 0
    if len(sys.argv) != 2:
        raise SystemExit("usage: agx_control_probe.py HARNESS|--check-only")
    execute(pathlib.Path(sys.argv[1]))
    print("AGX control PASS: compare, select, forward rejoin, and loops exact")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
