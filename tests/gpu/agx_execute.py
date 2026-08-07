#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Execute a Traveler-emitted AGX field-map kernel on the owned G16X queue."""

import argparse
import importlib
import pathlib
import random
import sys


def read_artifact(path):
    prime = None
    code = []
    in_code = False
    for raw in pathlib.Path(path).read_text().splitlines():
        line = raw.strip()
        if line.startswith("field "):
            prime = int(line.split()[1])
        elif line == "code":
            in_code = True
        elif line.startswith("end "):
            in_code = False
        elif in_code and line:
            code.append(line)
    if prime is None or not code:
        raise ValueError("artifact has no field or code block")
    return prime, bytes.fromhex("".join(code))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("artifact")
    parser.add_argument("harness")
    parser.add_argument("--runs", type=int, default=8)
    parser.add_argument("--formula",
                        choices=("square-plus-3", "add-sub", "add7", "sub5",
                                 "identity"),
                        default="square-plus-3")
    args = parser.parse_args()

    harness = pathlib.Path(args.harness).resolve()
    sys.path.insert(0, str(harness))
    probe_load = importlib.import_module("probe_load")

    prime, code = read_artifact(args.artifact)
    adversarial = [
        0,
        1,
        prime - 1,
        (1 << 30) % prime,
        12345 % prime,
        67890 % prime,
        (prime // 2) % prime,
        (prime // 2 + 1) % prime,
    ]
    rng = random.Random(20260804)
    rows = [adversarial]
    rows.extend([[rng.randrange(prime) for _ in range(8)]
                 for _ in range(args.runs)])

    checked = 0
    for values in rows:
        if prime > 0xFFFFFFFF:
            packed = []
            for value in values:
                packed.extend((value & 0xFFFFFFFF, value >> 32))
            got_words = probe_load.run_with_data(code, packed, grid=len(values),
                                                 words=len(packed))
            got = [got_words[i] | (got_words[i + 1] << 32)
                   for i in range(0, len(got_words), 2)]
        else:
            got = probe_load.run_with_data(code, values, grid=len(values),
                                           words=len(values))
        if args.formula == "add-sub":
            want = [((value - 5) % prime) * ((value + 7) % prime) % prime
                    for value in values]
        elif args.formula == "add7":
            want = [(value + 7) % prime for value in values]
        elif args.formula == "sub5":
            want = [(value - 5) % prime for value in values]
        elif args.formula == "identity":
            want = values
        else:
            want = [(value * value + 3) % prime for value in values]
        if got != want:
            print("AGX execution mismatch", file=sys.stderr)
            print("  input:", values, file=sys.stderr)
            print("  got:  ", got, file=sys.stderr)
            print("  want: ", want, file=sys.stderr)
            return 1
        checked += len(values)

    print("AGX execution PASS: Field<%d>, %d products, %d code bytes"
          % (prime, checked, len(code)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
