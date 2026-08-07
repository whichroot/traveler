#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Assemble and execute the fixed-shape G16X counted Montgomery dot."""

import pathlib
import sys


MASK32 = 0xFFFFFFFF
R = 1 << 32
PRIMES = (2013265921, 1811939329, 2130706433)

COUNTER = 0
BOUND = 2
ACC = 5
SLOT_X = 6
P_REG = 7
PINV_REG = 8
R2_REG = 9
SLOT_W = 12
SCRATCH = tuple([4, 10, 11] + list(range(13, 32)))

LOOP_TOP = bytes.fromhex("0f05541a")
LOOP_EXIT = bytes.fromhex("0f06040200000f0604010000")
BRANCH_LEAD = bytes.fromhex("8f0454020f0054")
KINC = bytes.fromhex("9f015400030200881504")
LOOP_CMP = bytes.fromhex("0a0122050500")
STOP = bytes.fromhex("0e000000")


def mov_imm(reg: int, value: int) -> bytes:
    if not 0 <= reg <= 15 or not 0 <= value <= 127:
        raise ValueError("small immediate outside the measured form")
    return bytes([0x0C | (reg << 4), value])


def mov_wide(value: int) -> bytes:
    if not 0 <= value <= MASK32:
        raise ValueError("wide immediate is not u32")
    return bytes([
        0x0C, 0x80 | (value & 0x7F), 0x02,
        ((value >> 25) & 0x7F) << 1,
        ((value >> 7) & 0x0F) << 1,
        ((value >> 11) & 0x03) << 2,
        (value >> 13) & 0xFF,
        (value >> 21) & 0x0F,
    ])


def prologue(reg: int) -> bytes:
    return bytes([0x0C | (reg << 4), 0xA0, 0x10, 0x06])


def sl_imm(dst: int, src: int, imm: int) -> bytes:
    return bytes([
        0x9F, 0x11, 0x54, dst * 2, 0x02, (imm << 1) & 0xFF,
        (src << 3) & 0xFF, 0x88, 0x11, 0x04,
    ])


def sl_add(dst: int, a: int, b: int) -> bytes:
    return bytes([
        0x9F, 0x11, 0x54, dst * 2, 0x02, (b << 2) & 0xFF,
        (a << 3) & 0xFF, 0xA8, 0x17, 0x05,
    ])


def sl_sub(dst: int, a: int, b: int) -> bytes:
    return bytes([
        0x1F, 0x11, 0x54, dst * 2, 0x02, (a << 2) & 0xFF,
        (b << 3) & 0xFF, 0xA8, 0x17, 0x05,
    ])


def sl_shift(dst: int, src: int, amount: int, left: bool) -> bytes:
    return bytes([
        0x27 if left else 0xA7, 0x00, 0x54, dst * 2,
        0x03 if left else 0x02, (src << 2) & 0xFF,
        (amount << 2) & 0xFF, 0x00, 0xF0, 0x11, 0x01, 0x00,
    ])


def sl_extract(dst: int, src: int, shift: int, width: int) -> bytes:
    return bytes([
        0xA7, 0x00, 0x54, dst * 2, 0x02, (src << 2) & 0xFF,
        (shift << 2) & 0xFF, 0x00, 0xF0, 0x11,
        (((width & 15) << 4) | 1) & 0xFF, 0x00,
    ])


def sl_mul(dst: int, a: int, b: int) -> bytes:
    return bytes([
        0x9F, 0x00, 0x54, dst * 2, 0x02, (a << 2) & 0xFF,
        (b << 3) & 0xFF, 0x00, 0xE0, 0x26, 0x0A, 0x00,
    ])


def loop_store(value: int) -> bytes:
    return bytes([
        0xE7, 0x10, 0x54, value * 2, 0x00, 0x00, 0x21, 0x00,
        0x11, 0x00, 0x00, 0x90, 0x11, 0x00,
    ])


def il_load(slot: int, index: int) -> bytes:
    return bytes([
        0x67, 0x00, 0x04, slot, 0x01, index, 0x20, 0x00,
        0x51, 0x01, 0x00, 0x40, 0x46, 0x00,
    ])


def il_add_load(dst: int, slot: int, greg: int) -> bytes:
    return bytes([
        0x9F, 0x01, 0x56, dst * 2, 0x02, slot * 2,
        (greg << 3) & 0xFF, 0xA8, 0x17, 0x05,
    ])


def il_imm(dst: int, src: int, imm: int) -> bytes:
    if not 0 <= imm <= 127:
        raise ValueError("in-loop immediate outside the measured form")
    return bytes([
        0x9F, 0x01, 0x54, dst * 2, 0x03, (imm << 1) & 0xFF,
        (src << 3) & 0xFF, 0x88, 0x15, 0x04,
    ])


def loop_bound(dst: int, src: int, trip_count: int) -> bytes:
    return bytes([
        0x9F, 0x01, 0x54, dst * 2, 0x03, (trip_count << 1) & 0xFF,
        (src << 3) & 0xFF, 0x88, 0x11, 0x04,
    ])


def loop_setup() -> bytearray:
    return bytearray.fromhex(
        "0a81220905221b0000000f0554010f015400000000000000"
    )


def branch_back(target: int, branch: int) -> bytes:
    offset = target - branch
    return BRANCH_LEAD + (offset & 0xFFFFFFFFFFFF).to_bytes(6, "little") + b"\x00"


class Body:
    def __init__(self) -> None:
        self.code = bytearray()
        self.free = list(SCRATCH)
        self.live = set()

    def take(self) -> int:
        if not self.free:
            raise ValueError("fixed counted-dot register map exhausted")
        reg = self.free.pop(0)
        self.live.add(reg)
        return reg

    def take_pair(self) -> tuple[int, int]:
        for reg in self.free:
            if reg + 1 in self.free:
                self.free.remove(reg)
                self.free.remove(reg + 1)
                self.live.add(reg)
                self.live.add(reg + 1)
                return reg, reg + 1
        raise ValueError("fixed counted-dot pair register map exhausted")

    def take_small(self) -> int:
        for reg in self.free:
            if reg <= 15:
                self.free.remove(reg)
                self.live.add(reg)
                return reg
        raise ValueError("fixed counted-dot small register map exhausted")

    def release(self, reg: int) -> None:
        if reg not in self.live:
            raise ValueError(f"release of non-live r{reg}")
        self.live.remove(reg)
        self.free.append(reg)
        self.free.sort()

    def copy(self, src: int) -> int:
        dst = self.take()
        self.code += sl_imm(dst, src, 0)
        return dst

    def load(self, slot: int, index: int, consume_index: bool = False) -> int:
        self.code += il_load(slot, index)
        zero = self.take_small()
        self.code += mov_imm(zero, 0)
        dst = self.take()
        self.code += il_add_load(dst, slot, zero)
        self.release(zero)
        if consume_index:
            self.release(index)
        return dst

    def binary(self, op: str, a: int, b: int) -> int:
        dst = self.take()
        self.code += sl_add(dst, a, b) if op == "add" else sl_sub(dst, a, b)
        self.release(a)
        self.release(b)
        return dst

    def shift(self, src: int, amount: int, left: bool = False) -> int:
        dst = self.take()
        self.code += sl_shift(dst, src, amount, left)
        self.release(src)
        return dst

    def extract(self, src: int, shift: int, width: int) -> int:
        dst = self.take()
        self.code += sl_extract(dst, src, shift, width)
        self.release(src)
        return dst

    def mul_pair(self, a: int, b: int) -> tuple[int, int]:
        low, high = self.take_pair()
        self.code += sl_mul(low, a, b)
        self.release(a)
        self.release(b)
        return low, high

    def redc(self, high: int, low: int) -> int:
        low_m = self.copy(low)
        low_half_src = self.copy(low)
        m, unused = self.mul_pair(low_m, self.copy(PINV_REG))
        self.release(unused)
        mp_low, mp_high = self.mul_pair(m, self.copy(P_REG))

        mp_half_src = self.copy(mp_low)
        low_half = self.shift(low_half_src, 1)
        mp_half = self.shift(mp_half_src, 1)
        low_bit = self.extract(low, 0, 1)
        mp_bit = self.extract(mp_low, 0, 1)
        halves = self.binary("add", low_half, mp_half)
        bottoms = self.binary("add", low_bit, mp_bit)
        bottom_carry = self.shift(bottoms, 1)
        carry_sum = self.binary("add", halves, bottom_carry)
        carry = self.shift(carry_sum, 31)

        high_sum = self.binary("add", high, mp_high)
        total = self.binary("add", high_sum, carry)
        diff = self.binary("sub", total, self.copy(P_REG))
        negative = self.shift(self.copy(diff), 31)
        correction, unused = self.mul_pair(negative, self.copy(P_REG))
        self.release(unused)
        return self.binary("add", diff, correction)

    def montgomery_mul(self, a: int, b: int) -> int:
        low, high = self.mul_pair(a, b)
        first = self.redc(high, low)
        low, high = self.mul_pair(first, self.copy(R2_REG))
        return self.redc(high, low)

    def field_add(self, a: int, b: int) -> int:
        total = self.binary("add", a, b)
        diff = self.binary("sub", total, self.copy(P_REG))
        negative = self.shift(self.copy(diff), 31)
        correction, unused = self.mul_pair(negative, self.copy(P_REG))
        self.release(unused)
        return self.binary("add", diff, correction)


def montgomery_constants(prime: int) -> tuple[int, int]:
    inverse = pow(prime, -1, R)
    return (-inverse) & MASK32, (R * R) % prime


def build_kernel(prime: int, trip_count: int) -> tuple[bytes, dict[str, int]]:
    if prime not in PRIMES or not 1 <= trip_count <= 8:
        raise ValueError("probe is confined to the three RNS primes and K<=8")
    pinv, r2 = montgomery_constants(prime)
    code = bytearray()
    for reg, value in ((P_REG, prime), (PINV_REG, pinv), (R2_REG, r2)):
        code += mov_wide(value)
        code += sl_imm(reg, 0, 0)
    code += mov_imm(ACC, 0)
    code += prologue(COUNTER)
    code += sl_imm(3, COUNTER, 0)
    code += sl_shift(COUNTER, 3, 4, True)

    setup_at = len(code)
    code += loop_setup()
    code += loop_bound(BOUND, COUNTER, trip_count * 2)
    loop_top = len(code)
    code += LOOP_TOP
    body_start = len(code)

    body = Body()
    x = body.load(SLOT_X, COUNTER)
    body.code += KINC
    w = body.load(SLOT_W, COUNTER)
    body.code += KINC
    product = body.montgomery_mul(x, w)
    result = body.field_add(body.copy(ACC), product)
    body.code += il_imm(ACC, result, 0)
    body.release(result)
    if body.live:
        raise ValueError(f"iteration leaves scratch live: {sorted(body.live)}")
    body.code += LOOP_CMP
    code += body.code

    branch = len(code)
    code += branch_back(loop_top, branch)
    code += LOOP_EXIT
    code += prologue(COUNTER)
    code += loop_store(ACC)
    stop = len(code)
    code += STOP

    body_size = stop - body_start
    size_at = setup_at + 17
    if body_size > 0xFFFF:
        raise ValueError("counted body exceeds measured size field")
    code[size_at] = body_size & 0xFF
    code[size_at + 1] = body_size >> 8
    return bytes(code), {
        "setup": setup_at,
        "size_at": size_at,
        "body_start": body_start,
        "loop_top": loop_top,
        "branch": branch,
        "stop": stop,
        "body_size": body_size,
    }


def input_words(prime: int) -> tuple[list[int], list[int], list[int]]:
    signed_x = [((i * 37 + 11) % 127) - 63 for i in range(32)]
    signed_w = [((i * 53 + 7) % 131) - 65 for i in range(256)]
    packed = []
    for q in range(128):
        row = q // 32
        col = q % 32
        for k in range(8):
            packed.append(signed_x[row * 8 + k] % prime)
            packed.append(signed_w[k * 32 + col] % prime)
    return signed_x, signed_w, packed


def expected(prime: int, trip_count: int) -> list[int]:
    signed_x, signed_w, _ = input_words(prime)
    result = []
    for q in range(128):
        row = q // 32
        col = q % 32
        total = 0
        for k in range(trip_count):
            total += signed_x[row * 8 + k] * signed_w[k * 32 + col]
        result.append(total % prime)
    return result


def check_kernel(code: bytes, meta: dict[str, int], trip_count: int) -> None:
    if len(code) >= 6652:
        raise ValueError(f"counted kernel did not shrink: {len(code)} bytes")
    if code.count(LOOP_TOP) != 1 or code.count(BRANCH_LEAD) != 1:
        raise ValueError("counted kernel does not have exactly one backedge")
    offset = int.from_bytes(code[meta["branch"] + 7:meta["branch"] + 13],
                            "little", signed=True)
    if offset != meta["loop_top"] - meta["branch"]:
        raise ValueError("counted-dot backward branch base changed")
    declared = int.from_bytes(code[meta["size_at"]:meta["size_at"] + 2],
                              "little")
    if declared != meta["stop"] - meta["body_start"]:
        raise ValueError("counted-dot structured size field changed")
    bound = loop_bound(BOUND, COUNTER, trip_count * 2)
    if code[meta["loop_top"] - len(bound):meta["loop_top"]] != bound:
        raise ValueError("counted-dot trip bound changed")
    if code[-4:] != STOP:
        raise ValueError("counted-dot stop absent")


def check_only() -> None:
    lengths = set()
    for prime in PRIMES:
        for trip_count in (1, 2, 8):
            code, meta = build_kernel(prime, trip_count)
            check_kernel(code, meta, trip_count)
            lengths.add(len(code))
    if len(lengths) != 1:
        raise ValueError(f"counted kernel size depends on constants: {lengths}")


def check_artifact(path: pathlib.Path) -> None:
    records = []
    current = None
    in_code = False
    for line in path.read_text().splitlines():
        if line.startswith("worker "):
            current = {"code": bytearray()}
            in_code = False
        elif current is not None and line.startswith("field "):
            current["field"] = int(line.split()[1])
        elif current is not None and line.startswith("output-words "):
            current["output"] = int(line.split()[1])
        elif current is not None and line.startswith("input0-words "):
            current["input0"] = int(line.split()[1])
        elif current is not None and line.startswith("input1-words "):
            current["input1"] = int(line.split()[1])
        elif current is not None and line.startswith("input-layout "):
            current["layout"] = int(line.split()[1])
        elif current is not None and line.startswith("bytes "):
            current["bytes"] = int(line.split()[1])
        elif current is not None and line == "code":
            in_code = True
        elif current is not None and line.startswith("end "):
            records.append(current)
            current = None
            in_code = False
        elif current is not None and in_code and line.startswith("  "):
            current["code"] += bytes.fromhex(line.strip())
    if len(records) != 3 or {record["field"] for record in records} != set(PRIMES):
        raise ValueError("compiler artifact does not contain the three RNS workers")
    for record in records:
        if (record.get("output"), record.get("input0"), record.get("input1"),
                record.get("layout")) != (128, 32, 256, 1):
            raise ValueError("compiler counted-dot ABI metadata changed")
        code = bytes(record["code"])
        expected_code, meta = build_kernel(record["field"], 8)
        check_kernel(code, meta, 8)
        if record.get("bytes") != len(code) or code != expected_code:
            raise ValueError(
                f"compiler counted-dot bytes differ for Field<{record['field']}>"
            )


def execute(harness: pathlib.Path) -> None:
    sys.path.insert(0, str(harness.resolve()))
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
    import agx_control_probe
    import probe_load

    a = [0, 1, 2, 0xFFFFFFFF, 100, 200, 42, 42]
    b = [0, 0, 0xFFFFFFFF, 0xFFFFFFFF, 101, 199, 42, 43]
    canary = [1 if x < y else 0 for x, y in zip(a, b)]
    for prime in PRIMES:
        _, _, words = input_words(prime)
        for trip_count in (1, 2, 8):
            before = probe_load.run_with_data(
                agx_control_probe.COMPARE, a + b, grid=8, words=8,
            )[:8]
            if before != canary:
                raise ValueError("counted-dot pre-canary failed")
            code, meta = build_kernel(prime, trip_count)
            check_kernel(code, meta, trip_count)
            got = probe_load.run_with_data(code, words, grid=64, words=64)[:64]
            want = expected(prime, trip_count)[:64]
            if got != want:
                first = next(i for i in range(64) if got[i] != want[i])
                raise ValueError(
                    f"counted dot mismatch p={prime} K={trip_count} "
                    f"at {first}: {got[first]} != {want[first]}"
                )
            after = probe_load.run_with_data(
                agx_control_probe.COMPARE, a + b, grid=8, words=8,
            )[:8]
            if after != canary:
                raise ValueError("counted-dot post-canary failed")


def main() -> int:
    check_only()
    if len(sys.argv) == 3 and sys.argv[1] == "--check-artifact":
        check_artifact(pathlib.Path(sys.argv[2]))
        print("AGX counted-dot compiler artifact PASS: three workers byte-exact")
        return 0
    if len(sys.argv) == 2 and sys.argv[1] == "--check-only":
        code, _ = build_kernel(PRIMES[0], 8)
        print(f"AGX counted-dot structure PASS: {len(code)} bytes, one backedge")
        return 0
    if len(sys.argv) != 2:
        raise SystemExit(
            "usage: agx_counted_dot_probe.py HARNESS|--check-only|"
            "--check-artifact PATH"
        )
    execute(pathlib.Path(sys.argv[1]))
    print("AGX counted-dot hardware PASS: K=1/2/8 exact over three RNS primes")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
