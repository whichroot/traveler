#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Execute definition-point taps from Traveler's 2^64-59 AGX kernel."""

import argparse
import importlib
import pathlib
import random
import re
import sys

B = 1 << 32
MASK = B - 1
Q = 1 << 64
P = Q - 59
TAP = re.compile(r"^tap (\w+) reg (\d+) mark (\d+)$")


def carry32(x, y):
    value = ((x >> 1) + (y >> 1)
             + (((x & 1) + (y & 1)) >> 1))
    assert value <= MASK
    return value >> 31


def addc32(x, y):
    return (x + y) & MASK, carry32(x, y)


def mulw32(x, y):
    value = x * y
    return value & MASK, value >> 32


def model(a, b):
    a0, a1 = a & MASK, a >> 32
    b0, b1 = b & MASK, b >> 32
    l00, h00 = mulw32(a0, b0)
    l01, h01 = mulw32(a0, b1)
    l10, h10 = mulw32(a1, b0)
    l11, h11 = mulw32(a1, b1)

    s, c10 = addc32(h00, l01)
    t1, c11 = addc32(s, l10)
    c1 = c10 + c11
    s, c20 = addc32(h01, h10)
    u, c21 = addc32(s, l11)
    t2, c22 = addc32(u, c1)
    c2 = c20 + c21 + c22
    t0, t3 = l00, h11 + c2
    assert a * b == t0 + B * t1 + B**2 * t2 + B**3 * t3

    f20, f21 = mulw32(t2, 59)
    f30, f31 = mulw32(t3, 59)
    s0, k0 = addc32(t0, f20)
    z1, k1 = addc32(t1, f30)
    h = f21 + k0
    s1, k2 = addc32(z1, h)
    c = f31 + k1 + k2

    k = 59 * c
    x0, d0 = addc32(s0, k)
    x1, d = addc32(s1, d0)
    v0 = x0 + 59 * d
    v1 = x1
    g0 = carry32(v0, 59)
    e = carry32(v1, g0)
    r0 = (v0 + 59 * e) & MASK
    r1 = (v1 + e) & MASK
    assert r0 | (r1 << 32) == (a * b) % P

    return {
        "t0": t0, "t1": t1, "t2": t2, "t3": t3,
        "s0": s0, "s1": s1, "c": c,
        "x0": x0, "x1": x1, "d": d,
        "v0": v0, "v1": v1, "g0": g0, "e": e,
        "r0": r0, "r1": r1,
    }


def read_artifact(path):
    taps = {}
    instructions = []
    in_code = False
    for raw in pathlib.Path(path).read_text().splitlines():
        line = raw.strip()
        match = TAP.match(line)
        if match:
            taps[match.group(1)] = (int(match.group(2)), int(match.group(3)))
        elif line == "code":
            in_code = True
        elif line.startswith("end "):
            in_code = False
        elif in_code and line:
            instructions.append(bytes.fromhex(line))
    if not taps or not instructions:
        raise ValueError("artifact has no taps or code")
    return taps, instructions


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("artifact")
    parser.add_argument("harness")
    parser.add_argument("--formula", choices=("square", "add-sub"),
                        default="square")
    args = parser.parse_args()

    harness = pathlib.Path(args.harness).resolve()
    sys.path.insert(0, str(harness))
    kernel = importlib.import_module("agx_kernel")
    probe_load = importlib.import_module("probe_load")
    taps, instructions = read_artifact(args.artifact)

    rng = random.Random(20260804)
    values = [0, 1, P - 1, P - 59, P - 4,
              0xC4440054DD3F4006 % P,
              1783284043205960718]
    values.append(rng.randrange(P))
    packed = []
    for value in values:
        packed.extend((value & MASK, value >> 32))
    if args.formula == "add-sub":
        expected = [model((value - 5) % P, (value + 7) % P)
                    for value in values]
    else:
        expected = [model(value, value) for value in values]

    failures = 0
    for name in ("t0", "t1", "t2", "t3", "s0", "s1", "c",
                 "x0", "x1", "d", "v0", "v1", "g0", "e", "r0", "r1"):
        reg, mark = taps[name]
        code = b"".join(instructions[:mark])
        code += kernel.store_g(reg, 0, 1) + kernel.STOP
        got = probe_load.run_with_data(code, packed, grid=len(values),
                                       words=len(values))
        want = [row[name] for row in expected]
        if got != want:
            failures += 1
            print("  %-3s FAILED\n    got  %s\n    want %s" %
                  (name, got, want), file=sys.stderr)
        else:
            print("  %-3s PASS" % name)

    print("AGX64 taps: %d stages, %d failure(s)" % (len(taps), failures))
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
