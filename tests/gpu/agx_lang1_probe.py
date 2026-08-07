#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Independently validate LANG1 scalarization artifacts and output."""

import argparse
import hashlib
import json
import pathlib
import struct

from agx_lang0_probe import (
    MASK_BEGIN,
    MASK_ELSE,
    MASK_POP,
    STOP,
    check_encoder,
    cmpsel_bool,
    decode_cmpsel,
    parse_artifact,
)


def check_artifact(path: pathlib.Path) -> None:
    workers = parse_artifact(path)
    if len(workers) != 1:
        raise ValueError(f"expected one LANG1 struct worker, saw {len(workers)}")
    worker = workers[0]
    code = worker["code"]
    if worker.get("bytes") != 350 or sum(map(len, code)) != 350:
        raise ValueError("LANG1 struct byte count changed")
    if worker.get("instructions") != 43 or len(code) != 43:
        raise ValueError("LANG1 struct instruction count changed")
    digest = hashlib.sha256(b"".join(code)).hexdigest()
    if digest != "06b52f54b44c166a1dd26d59c85205a7490eab5a14a6aa811227b31b21f17d08":
        raise ValueError(f"LANG1 struct machine-code hash changed: {digest}")
    if worker["captures"] != [
        "capture left slot 1",
        "capture right slot 1 word-offset 768",
        "capture output slot 0",
    ]:
        raise ValueError("LANG1 struct capture/extents contract changed")
    if code[-1] != STOP:
        raise ValueError("LANG1 struct worker has no stop")
    depth = maximum = compares = 0
    for inst in code:
        if inst == MASK_BEGIN:
            depth += 1
            maximum = max(maximum, depth)
        elif inst == MASK_ELSE:
            if depth == 0:
                raise ValueError("LANG1 struct else underflow")
        elif inst == MASK_POP:
            depth -= 1
            if depth < 0:
                raise ValueError("LANG1 struct mask underflow")
        decoded = decode_cmpsel(inst)
        if decoded is not None:
            if cmpsel_bool(*decoded) != inst:
                raise ValueError("LANG1 struct compare failed independent encoding")
            compares += 1
    if depth != 0 or maximum > 4 or compares != 2:
        raise ValueError("LANG1 struct control shape changed")


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
    return struct.pack("<768I", *(max(a, b) for a, b in zip(left, right)))


def check_output(path: pathlib.Path) -> None:
    if path.read_bytes() != expected_output():
        raise ValueError("LANG1 struct output differs from independent oracle")


def check_match_artifact(path: pathlib.Path) -> None:
    workers = parse_artifact(path)
    expected = (
        (274, 33, "6811925529833bf18b6ac0d04712a137373b71b07f737d902ce0119d9715783a", 2, 1),
        (434, 58, "99492fca6ec36c6893d097c658d65b729dcdb839cf5a4638686355de3d6cddc9", 4, 2),
    )
    if len(workers) != len(expected):
        raise ValueError(f"expected two LANG1 match workers, saw {len(workers)}")
    for worker, (nbytes, ninst, digest, ncmp, nelse) in zip(workers, expected):
        code = worker["code"]
        if worker.get("bytes") != nbytes or sum(map(len, code)) != nbytes:
            raise ValueError("LANG1 match byte count changed")
        if worker.get("instructions") != ninst or len(code) != ninst:
            raise ValueError("LANG1 match instruction count changed")
        if hashlib.sha256(b"".join(code)).hexdigest() != digest:
            raise ValueError("LANG1 match machine-code hash changed")
        if worker["captures"] != [
            "capture left slot 1",
            "capture right slot 1 word-offset 768",
            "capture output slot 0",
        ]:
            raise ValueError("LANG1 match capture/extents contract changed")
        depth = maximum = compares = elses = 0
        for inst in code:
            if inst == MASK_BEGIN:
                depth += 1
                maximum = max(maximum, depth)
            elif inst == MASK_ELSE:
                elses += 1
                if depth == 0:
                    raise ValueError("LANG1 match else underflow")
            elif inst == MASK_POP:
                depth -= 1
            decoded = decode_cmpsel(inst)
            if decoded is not None:
                if cmpsel_bool(*decoded) != inst:
                    raise ValueError("LANG1 match compare failed independent encoding")
                compares += 1
        if depth != 0 or maximum != 2 or compares != ncmp or elses != nelse:
            raise ValueError("LANG1 match control shape changed")


def check_match_output(path: pathlib.Path) -> None:
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
    right[:4] = [13, 5, 11, 2]
    one = struct.pack("<768I", *(max(a, b) for a, b in zip(left, right)))
    if path.read_bytes() != one + one:
        raise ValueError("LANG1 integer/enum match output differs from oracle")


def check_array_artifact(path: pathlib.Path) -> None:
    workers = parse_artifact(path)
    if len(workers) != 1:
        raise ValueError(f"expected one LANG1 array worker, saw {len(workers)}")
    worker = workers[0]
    code = worker["code"]
    if worker.get("bytes") != 384 or sum(map(len, code)) != 384:
        raise ValueError("LANG1 array byte count changed")
    if worker.get("instructions") != 52 or len(code) != 52:
        raise ValueError("LANG1 array instruction count changed")
    digest = hashlib.sha256(b"".join(code)).hexdigest()
    if digest != "86f68e7bd37ecaf875bb5289ef692650465f612305d27ddb30e483821649542f":
        raise ValueError(f"LANG1 array machine-code hash changed: {digest}")
    if worker["captures"] != [
        "capture left slot 1",
        "capture right slot 1 word-offset 768",
        "capture output slot 0",
    ]:
        raise ValueError("LANG1 array capture/extents contract changed")
    depth = maximum = compares = 0
    for inst in code:
        if inst == MASK_BEGIN:
            depth += 1
            maximum = max(maximum, depth)
        elif inst == MASK_POP:
            depth -= 1
        decoded = decode_cmpsel(inst)
        if decoded is not None:
            if cmpsel_bool(*decoded) != inst:
                raise ValueError("LANG1 array compare failed independent encoding")
            compares += 1
    if depth != 0 or maximum != 2 or compares != 2:
        raise ValueError("LANG1 array control shape changed")


def check_array_output(path: pathlib.Path) -> None:
    raw = pathlib.Path(path).read_bytes()
    # The array fixture shares the match fixture's deterministic max oracle.
    modulus = 2013265921
    mask = (1 << 64) - 1
    state = 0x6A09E667F3BCC909
    values: list[int] = []
    for index in range(768):
        state = (state * 6364136223846793005 + 1442695040888963407) & mask
        left = state % modulus
        state = (state * 6364136223846793005 + 1442695040888963407) & mask
        right = state % modulus
        if index == 0:
            left, right = 0, 13
        elif index == 1:
            left, right = 3, 5
        elif index == 2:
            left, right = 7, 11
        elif index == 3:
            left, right = 19, 2
        values.append(max(left, right))
    if raw != struct.pack("<768I", *values):
        raise ValueError("LANG1 fixed-array output differs from oracle")


def check_dynamic_array_artifact(path: pathlib.Path) -> None:
    workers = parse_artifact(path)
    if len(workers) != 1:
        raise ValueError(f"expected one LANG1 dynamic-array worker, saw {len(workers)}")
    worker = workers[0]
    code = worker["code"]
    if worker.get("bytes") != 1790 or sum(map(len, code)) != 1790:
        raise ValueError("LANG1 dynamic-array byte count changed")
    if worker.get("instructions") != 250 or len(code) != 250:
        raise ValueError("LANG1 dynamic-array instruction count changed")
    digest = hashlib.sha256(b"".join(code)).hexdigest()
    if digest != "bd0a9718c62d81faba1749436f3712d45c73df29775e59e71a07c92bc9e09bf4":
        raise ValueError(f"LANG1 dynamic-array machine-code hash changed: {digest}")
    if worker["captures"] != [
        "capture left slot 1",
        "capture right slot 1 word-offset 768",
        "capture output slot 0",
    ]:
        raise ValueError("LANG1 dynamic-array capture/extents contract changed")
    control = (
        sum(inst == MASK_BEGIN for inst in code),
        sum(inst == MASK_ELSE for inst in code),
        sum(inst == MASK_POP for inst in code),
        sum(decode_cmpsel(inst) is not None for inst in code),
    )
    if control != (18, 3, 18, 18):
        raise ValueError(f"LANG1 dynamic-array mux shape changed: {control}")
    if code[-1] != STOP:
        raise ValueError("LANG1 dynamic-array worker has no stop")


def check_dynamic_array_output(path: pathlib.Path) -> None:
    modulus = 2013265921
    mask = (1 << 64) - 1
    state = 0x6A09E667F3BCC909
    values: list[int] = []
    for index in range(768):
        state = (state * 6364136223846793005 + 1442695040888963407) & mask
        left = state % modulus
        state = (state * 6364136223846793005 + 1442695040888963407) & mask
        right = state % modulus
        values.append(right if (index // 192) % 2 == 0 else left)
    values[:4] = [13, 5, 11, 2]
    if path.read_bytes() != struct.pack("<768I", *values):
        raise ValueError("LANG1 dynamic-array read/write output differs from oracle")


def check_call_artifact(path: pathlib.Path) -> None:
    workers = parse_artifact(path)
    if len(workers) != 1:
        raise ValueError(f"expected one LANG1 call worker, saw {len(workers)}")
    worker = workers[0]
    code = worker["code"]
    if worker.get("bytes") != 240 or sum(map(len, code)) != 240:
        raise ValueError("LANG1 call byte count changed")
    if worker.get("instructions") != 25 or len(code) != 25:
        raise ValueError("LANG1 call instruction count changed")
    digest = hashlib.sha256(b"".join(code)).hexdigest()
    if digest != "e2e43c5acc082fc96dd95c497590af21d0123afead333e6d6cb9d07653724c37":
        raise ValueError(f"LANG1 call machine-code hash changed: {digest}")
    if worker["captures"] != [
        "capture left slot 1",
        "capture right slot 1 word-offset 768",
        "capture output slot 0",
    ]:
        raise ValueError("LANG1 call capture/extents contract changed")
    if code[-1] != STOP:
        raise ValueError("LANG1 call worker has no stop")


def check_call_output(path: pathlib.Path) -> None:
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
    right[:4] = [13, 5, 11, 2]
    expected = struct.pack("<768I", *(max(a, b) for a, b in zip(left, right)))
    if path.read_bytes() != expected:
        raise ValueError("LANG1 direct/generic call output differs from oracle")


def check_operator_artifact(path: pathlib.Path) -> None:
    workers = parse_artifact(path)
    if len(workers) != 1:
        raise ValueError(f"expected one LANG1 operator worker, saw {len(workers)}")
    worker = workers[0]
    code = worker["code"]
    if worker.get("bytes") != 250 or sum(map(len, code)) != 250:
        raise ValueError("LANG1 operator byte count changed")
    if worker.get("instructions") != 27 or len(code) != 27:
        raise ValueError("LANG1 operator instruction count changed")
    digest = hashlib.sha256(b"".join(code)).hexdigest()
    if digest != "5d352351d6bf4f0981cce6bf9acbfda1c30ab4767960c80aa8bddb78fb3a3e86":
        raise ValueError(f"LANG1 operator machine-code hash changed: {digest}")
    if worker["captures"] != [
        "capture left slot 1",
        "capture right slot 1 word-offset 768",
        "capture output slot 0",
    ]:
        raise ValueError("LANG1 operator capture/extents contract changed")
    if code[-1] != STOP:
        raise ValueError("LANG1 operator worker has no stop")


def check_operator_output(path: pathlib.Path) -> None:
    modulus = 2013265921
    mask = (1 << 64) - 1
    state = 0x6A09E667F3BCC909
    values: list[int] = []
    for _ in range(768):
        state = (state * 6364136223846793005 + 1442695040888963407) & mask
        _left = state % modulus
        state = (state * 6364136223846793005 + 1442695040888963407) & mask
        values.append(state % modulus)
    if path.read_bytes() != struct.pack("<768I", *values):
        raise ValueError("LANG1 trait/operator output differs from oracle")


def check_closure_artifact(path: pathlib.Path) -> None:
    workers = parse_artifact(path)
    if len(workers) != 1:
        raise ValueError(f"expected one LANG1 closure worker, saw {len(workers)}")
    worker = workers[0]
    code = worker["code"]
    if worker.get("bytes") != 146 or sum(map(len, code)) != 146:
        raise ValueError("LANG1 closure byte count changed")
    if worker.get("instructions") != 16 or len(code) != 16:
        raise ValueError("LANG1 closure instruction count changed")
    digest = hashlib.sha256(b"".join(code)).hexdigest()
    if digest != "81aa726328f05277d7c18c5a22e939fb00a7f82798f40955eeae8583d55406b5":
        raise ValueError(f"LANG1 closure machine-code hash changed: {digest}")
    if worker["captures"] != [
        "capture left slot 1",
        "capture right slot 1 word-offset 768",
        "capture output slot 0",
    ]:
        raise ValueError("LANG1 closure capture/extents contract changed")
    if code[-1] != STOP:
        raise ValueError("LANG1 closure worker has no stop")


def check_closure_output(path: pathlib.Path) -> None:
    check_operator_output(path)


def check_c1_artifact(path: pathlib.Path) -> None:
    workers = parse_artifact(path)
    if len(workers) != 1:
        raise ValueError(f"expected one LANG1-C1 worker, saw {len(workers)}")
    worker = workers[0]
    code = worker["code"]
    if worker.get("bytes") != 1070 or sum(map(len, code)) != 1070:
        raise ValueError("LANG1-C1 byte count changed")
    if worker.get("instructions") != 124 or len(code) != 124:
        raise ValueError("LANG1-C1 instruction count changed")
    digest = hashlib.sha256(b"".join(code)).hexdigest()
    if digest != "83a7ac1d7355fd24ce135c8520d4d41cc72dce7d6e6ab7171f2300375e6120f0":
        raise ValueError(f"LANG1-C1 machine-code hash changed: {digest}")
    if worker["captures"] != [
        "capture left slot 1",
        "capture right slot 1 word-offset 768",
        "capture output slot 0",
    ]:
        raise ValueError("LANG1-C1 capture/extents contract changed")
    begins = sum(inst == MASK_BEGIN for inst in code)
    elses = sum(inst == MASK_ELSE for inst in code)
    pops = sum(inst == MASK_POP for inst in code)
    compares = sum(decode_cmpsel(inst) is not None for inst in code)
    if (begins, elses, pops, compares) != (4, 3, 4, 4):
        raise ValueError("LANG1-C1 terminal-diamond control shape changed")
    if code[-1] != STOP:
        raise ValueError("LANG1-C1 worker has no stop")


def check_c1_output(path: pathlib.Path) -> None:
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
    right[:4] = [13, 5, 11, 2]
    values = [
        max(a, b) if index < 384 else min(a, b)
        for index, (a, b) in enumerate(zip(left, right))
    ]
    if path.read_bytes() != struct.pack("<768I", *values):
        raise ValueError("LANG1-C1 callee-control output differs from oracle")


def check_c1_operator_artifact(path: pathlib.Path) -> None:
    workers = parse_artifact(path)
    if len(workers) != 1:
        raise ValueError(f"expected one LANG1-C1 operator worker, saw {len(workers)}")
    worker = workers[0]
    code = worker["code"]
    if worker.get("bytes") != 538 or sum(map(len, code)) != 538:
        raise ValueError("LANG1-C1 operator byte count changed")
    if worker.get("instructions") != 63 or len(code) != 63:
        raise ValueError("LANG1-C1 operator instruction count changed")
    digest = hashlib.sha256(b"".join(code)).hexdigest()
    if digest != "a99d59057dfebac8539ac761c7b220b7f9bc9b60e219ab4357faf470cf272676":
        raise ValueError(f"LANG1-C1 operator machine-code hash changed: {digest}")
    if worker["captures"] != [
        "capture left slot 1",
        "capture right slot 1 word-offset 768",
        "capture output slot 0",
    ]:
        raise ValueError("LANG1-C1 operator capture/extents contract changed")
    begins = sum(inst == MASK_BEGIN for inst in code)
    elses = sum(inst == MASK_ELSE for inst in code)
    pops = sum(inst == MASK_POP for inst in code)
    compares = sum(decode_cmpsel(inst) is not None for inst in code)
    if (begins, elses, pops, compares) != (3, 2, 3, 3):
        raise ValueError("LANG1-C1 operator control shape changed")
    if code[-1] != STOP:
        raise ValueError("LANG1-C1 operator worker has no stop")


def check_c1_operator_output(path: pathlib.Path) -> None:
    modulus = 2013265921
    mask = (1 << 64) - 1
    state = 0x6A09E667F3BCC909
    values: list[int] = []
    for _ in range(768):
        state = (state * 6364136223846793005 + 1442695040888963407) & mask
        left = state % modulus
        state = (state * 6364136223846793005 + 1442695040888963407) & mask
        right = state % modulus
        values.append(max(left, right))
    if path.read_bytes() != struct.pack("<768I", *values):
        raise ValueError("LANG1-C1 trait/operator output differs from oracle")


def check_c1_closure_artifact(path: pathlib.Path) -> None:
    workers = parse_artifact(path)
    if len(workers) != 1:
        raise ValueError(f"expected one LANG1-C1 closure worker, saw {len(workers)}")
    worker = workers[0]
    code = worker["code"]
    if worker.get("bytes") != 278 or sum(map(len, code)) != 278:
        raise ValueError("LANG1-C1 closure byte count changed")
    if worker.get("instructions") != 32 or len(code) != 32:
        raise ValueError("LANG1-C1 closure instruction count changed")
    digest = hashlib.sha256(b"".join(code)).hexdigest()
    if digest != "10e52c3b4c7ff8498f5edbb770b69284cc596e8ad6319876f1b1aaf6f262e9ec":
        raise ValueError(f"LANG1-C1 closure machine-code hash changed: {digest}")
    if worker["captures"] != [
        "capture right slot 1",
        "capture left slot 1 word-offset 768",
        "capture output slot 0",
    ]:
        raise ValueError("LANG1-C1 closure capture/extents contract changed")
    begins = sum(inst == MASK_BEGIN for inst in code)
    elses = sum(inst == MASK_ELSE for inst in code)
    pops = sum(inst == MASK_POP for inst in code)
    compares = sum(decode_cmpsel(inst) is not None for inst in code)
    if (begins, elses, pops, compares) != (1, 1, 1, 1):
        raise ValueError("LANG1-C1 closure control shape changed")
    if code[-1] != STOP:
        raise ValueError("LANG1-C1 closure worker has no stop")


def check_c1_closure_output(path: pathlib.Path) -> None:
    check_c1_operator_output(path)


def check_c1_harden_artifact(path: pathlib.Path) -> None:
    workers = parse_artifact(path)
    if len(workers) != 1:
        raise ValueError(f"expected one LANG1-C1 hardening worker, saw {len(workers)}")
    worker = workers[0]
    code = worker["code"]
    if worker.get("bytes") != 1108 or sum(map(len, code)) != 1108:
        raise ValueError("LANG1-C1 hardening byte count changed")
    if worker.get("instructions") != 135 or len(code) != 135:
        raise ValueError("LANG1-C1 hardening instruction count changed")
    digest = hashlib.sha256(b"".join(code)).hexdigest()
    if digest != "8cdf11edc19ff989b4615cdb98c8958f7f1e1507da1f94a25fcd2198a85fd314":
        raise ValueError(f"LANG1-C1 hardening machine-code hash changed: {digest}")
    if worker["captures"] != [
        "capture left slot 1",
        "capture right slot 1 word-offset 768",
        "capture output slot 0",
    ]:
        raise ValueError("LANG1-C1 hardening capture/extents contract changed")
    if code[-1] != STOP:
        raise ValueError("LANG1-C1 hardening worker has no stop")


def check_c1_harden_output(path: pathlib.Path) -> None:
    modulus = 2013265921
    mask = (1 << 64) - 1
    state = 0x6A09E667F3BCC909
    values: list[int] = []
    for index in range(768):
        state = (state * 6364136223846793005 + 1442695040888963407) & mask
        left = state % modulus
        state = (state * 6364136223846793005 + 1442695040888963407) & mask
        right = state % modulus
        values.append(right if (index // 128) % 2 == 0 else left)
    if path.read_bytes() != struct.pack("<768I", *values):
        raise ValueError("LANG1-C1 order/type hardening output differs from oracle")


def write_c1_hardening_refusals(path: pathlib.Path) -> None:
    lines = [
        "// SPDX-License-Identifier: Apache-2.0",
        "field AgxC1LimitF = Field<2013265921>;",
        "",
    ]
    for index in range(34):
        lines.append(
            f"fn c1_limit_depth_{index}(value: AgxC1LimitF) -> AgxC1LimitF {{"
        )
        if index == 33:
            lines.append("    let copied: AgxC1LimitF = value;")
            lines.append("    return copied;")
        else:
            lines.append(f"    return c1_limit_depth_{index + 1}(value);")
        lines.extend(["}", ""])
    lines.extend([
        "fn agx_c1_refuse_call_depth(input: *AgxC1LimitF,",
        "                               output: *AgxC1LimitF) {",
        "    for i in 0..32 { output[i] = c1_limit_depth_0(input[i]); }",
        "}",
        "",
        "fn c1_limit_prelude(value: AgxC1LimitF) -> AgxC1LimitF {",
    ])
    prior = "value"
    for index in range(256):
        lines.append(f"    let value_{index}: AgxC1LimitF = {prior};")
        prior = f"value_{index}"
    lines.extend([
        f"    return {prior};",
        "}",
        "",
        "fn agx_c1_refuse_prelude_capacity(input: *AgxC1LimitF,",
        "                                    output: *AgxC1LimitF) {",
        "    for i in 0..32 { output[i] = c1_limit_prelude(input[i]); }",
        "}",
        "",
        "fn agx_dynamic_refuse_prelude_capacity(input: *AgxC1LimitF,",
        "                                        output: *AgxC1LimitF) {",
        "    for i in 0..32 {",
        "        var local: [i32; 4] = 0;",
        "        var index: i32 = 0;",
        "        if input[i] != 0 { index = 1; }",
        "        let total: i32 = " + " + ".join(["local[index]"] * 60) + ";",
        "        var chosen: AgxC1LimitF = input[i];",
        "        if total == 0 { chosen = input[i]; }",
        "        output[i] = chosen;",
        "    }",
        "}",
        "",
    ])
    path.write_text("\n".join(lines))


def check_c1_refusals(path: pathlib.Path) -> None:
    rows = [json.loads(line) for line in path.read_text().splitlines() if line]
    by_name = {row["fn"]: row for row in rows}
    shape_names = (
        "agx_c1_refuse_nonterminal",
        "agx_c1_refuse_missing_else",
        "agx_c1_refuse_nested_loop",
        "agx_c1_refuse_recursive",
        "agx_c1_refuse_mutual_recursion",
    )
    for name in shape_names:
        row = by_name.get(name)
        if row is None or row["c1_refusal"] != "callee-shape":
            raise ValueError(f"LANG1-C1 shape refusal changed for {name}")
        if row["lang1_candidate"] != 0:
            raise ValueError(f"LANG1-C1 shape refusal became a candidate: {name}")
    short = by_name.get("agx_c1_refuse_short_circuit")
    if short is None or short["c1_refusal"] != "short-circuit":
        raise ValueError("LANG1-C1 short-circuit refusal changed")
    depth = by_name.get("agx_c1_refuse_depth")
    if depth is None or depth["max_control_depth"] != 5:
        raise ValueError("LANG1-C1 excessive-depth observation changed")
    mutation_names = {
        "agx_c1_refuse_capture_mutation",
        "agx_c1_refuse_expr_capture_mutation",
    }
    if not mutation_names.issubset(by_name):
        raise ValueError("LANG1-C1 capture-mutation refusal disappeared")
    for name, row in by_name.items():
        if (name.startswith("agx_c1_refuse_")
                and name not in mutation_names
                and row["lang1_candidate"] != 0):
            raise ValueError(f"LANG1-C1 negative became a candidate: {name}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check-artifact", type=pathlib.Path)
    parser.add_argument("--check-output", type=pathlib.Path)
    parser.add_argument("--check-match-artifact", type=pathlib.Path)
    parser.add_argument("--check-match-output", type=pathlib.Path)
    parser.add_argument("--check-array-artifact", type=pathlib.Path)
    parser.add_argument("--check-array-output", type=pathlib.Path)
    parser.add_argument("--check-dynamic-array-artifact", type=pathlib.Path)
    parser.add_argument("--check-dynamic-array-output", type=pathlib.Path)
    parser.add_argument("--check-call-artifact", type=pathlib.Path)
    parser.add_argument("--check-call-output", type=pathlib.Path)
    parser.add_argument("--check-operator-artifact", type=pathlib.Path)
    parser.add_argument("--check-operator-output", type=pathlib.Path)
    parser.add_argument("--check-closure-artifact", type=pathlib.Path)
    parser.add_argument("--check-closure-output", type=pathlib.Path)
    parser.add_argument("--check-c1-artifact", type=pathlib.Path)
    parser.add_argument("--check-c1-output", type=pathlib.Path)
    parser.add_argument("--check-c1-operator-artifact", type=pathlib.Path)
    parser.add_argument("--check-c1-operator-output", type=pathlib.Path)
    parser.add_argument("--check-c1-closure-artifact", type=pathlib.Path)
    parser.add_argument("--check-c1-closure-output", type=pathlib.Path)
    parser.add_argument("--check-c1-harden-artifact", type=pathlib.Path)
    parser.add_argument("--check-c1-harden-output", type=pathlib.Path)
    parser.add_argument("--write-c1-hardening-refusals", type=pathlib.Path)
    parser.add_argument("--check-c1-refusals", type=pathlib.Path)
    args = parser.parse_args()
    check_encoder()
    if args.check_artifact is not None:
        check_artifact(args.check_artifact)
    if args.check_output is not None:
        check_output(args.check_output)
    if args.check_match_artifact is not None:
        check_match_artifact(args.check_match_artifact)
    if args.check_match_output is not None:
        check_match_output(args.check_match_output)
    if args.check_array_artifact is not None:
        check_array_artifact(args.check_array_artifact)
    if args.check_array_output is not None:
        check_array_output(args.check_array_output)
    if args.check_dynamic_array_artifact is not None:
        check_dynamic_array_artifact(args.check_dynamic_array_artifact)
    if args.check_dynamic_array_output is not None:
        check_dynamic_array_output(args.check_dynamic_array_output)
    if args.check_call_artifact is not None:
        check_call_artifact(args.check_call_artifact)
    if args.check_call_output is not None:
        check_call_output(args.check_call_output)
    if args.check_operator_artifact is not None:
        check_operator_artifact(args.check_operator_artifact)
    if args.check_operator_output is not None:
        check_operator_output(args.check_operator_output)
    if args.check_closure_artifact is not None:
        check_closure_artifact(args.check_closure_artifact)
    if args.check_closure_output is not None:
        check_closure_output(args.check_closure_output)
    if args.check_c1_artifact is not None:
        check_c1_artifact(args.check_c1_artifact)
    if args.check_c1_output is not None:
        check_c1_output(args.check_c1_output)
    if args.check_c1_operator_artifact is not None:
        check_c1_operator_artifact(args.check_c1_operator_artifact)
    if args.check_c1_operator_output is not None:
        check_c1_operator_output(args.check_c1_operator_output)
    if args.check_c1_closure_artifact is not None:
        check_c1_closure_artifact(args.check_c1_closure_artifact)
    if args.check_c1_closure_output is not None:
        check_c1_closure_output(args.check_c1_closure_output)
    if args.check_c1_harden_artifact is not None:
        check_c1_harden_artifact(args.check_c1_harden_artifact)
    if args.check_c1_harden_output is not None:
        check_c1_harden_output(args.check_c1_harden_output)
    if args.write_c1_hardening_refusals is not None:
        write_c1_hardening_refusals(args.write_c1_hardening_refusals)
    if args.check_c1_refusals is not None:
        check_c1_refusals(args.check_c1_refusals)
    print("AGX LANG1 PASS: aggregates and static abstractions execute exactly")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
