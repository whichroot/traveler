#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Pin PROOF0-a lexical identity and recursive effect summaries."""

import hashlib
import json
import pathlib
import sys


def require(row: dict, **expected: object) -> None:
    for key, value in expected.items():
        if row.get(key) != value:
            raise ValueError(
                f"line {row.get('line')}: {key}={row.get(key)!r}, expected {value!r}"
            )


def read_rows(path: str) -> list[dict]:
    return [
        json.loads(line)
        for line in pathlib.Path(path).read_text().splitlines()
        if line
    ]


def check_corpus(rows: list[dict]) -> None:
    if len(rows) != 346:
        raise ValueError(f"expected 346 corpus summaries, saw {len(rows)}")
    dispatched = sum(row["legacy_dispatched"] for row in rows)
    cpu = sum(row["cpu_effects"] for row in rows)
    agx = sum(row["agx_effects"] for row in rows)
    if dispatched != 171:
        raise ValueError(
            f"legacy dispatch baseline changed: dispatched={dispatched}, "
            f"cpu={cpu}, agx={agx}"
        )
    incomplete = [row for row in rows if row["complete"] == 0]
    if len(incomplete) != 1 or incomplete[0]["reason"] != "unsupported-node":
        raise ValueError("unexpected whole-corpus summary incompleteness")
    if (cpu, agx) != (213, 146):
        raise ValueError(
            f"PROOF0 effect baseline changed: cpu={cpu} agx={agx}"
        )
    canonical = "\n".join(
        json.dumps(
            {key: value for key, value in row.items()
             if key not in {"line", "col"}},
            sort_keys=True,
            separators=(",", ":"),
        )
        for row in rows
    )
    digest = hashlib.sha256(canonical.encode()).hexdigest()
    expected_digest = (
        "26336997f4ea6b1cb489be881949220028cfc5a874e5eae040e1dd5d5a80f1e6"
    )
    if digest != expected_digest:
        raise ValueError(f"PROOF0 per-record baseline changed: sha256={digest}")
    print(f"PROOF0-a corpus PASS: 346 records, effects cpu={cpu} agx={agx}")


def check_adversarial(rows: list[dict]) -> None:
    if len(rows) != 8:
        raise ValueError(f"expected 8 adversarial summaries, saw {len(rows)}")
    if any(row["complete"] != 1 or row["reason"] != "" for row in rows):
        raise ValueError("adversarial PROOF0 summary is incomplete")
    require(rows[0], legacy_reason="mutating-call", calls=1,
            effectful_calls=1, cpu_effects=0, agx_effects=0)
    require(rows[1], alias_writes=1, cpu_effects=0, agx_effects=0)
    require(rows[2], legacy_reason="mutating-call", calls=1,
            overload_ops=1, unknown_calls=1,
            cpu_effects=0, agx_effects=0)
    require(rows[3], short_circuits=4, max_control_depth=5,
            cpu_effects=1, agx_effects=0)
    require(rows[4], fors=1, continues=1, max_private_depth=1,
            max_control_depth=5, cpu_effects=1, agx_effects=0)
    require(rows[5], continues=1, outer_exits=1,
            cpu_effects=0, agx_effects=0)
    require(rows[6], legacy_reason="call-read", calls=1, read_calls=1,
            cpu_effects=0, agx_effects=0)
    require(rows[7], legacy_reason="mutating-call", calls=1,
            effectful_calls=1, cpu_effects=0, agx_effects=0)
    print("PROOF0-a adversarial PASS: false-safe counterexamples refused")


def main() -> int:
    if len(sys.argv) == 3 and sys.argv[1] == "--corpus":
        check_corpus(read_rows(sys.argv[2]))
        return 0
    if len(sys.argv) == 3 and sys.argv[1] == "--adversarial":
        check_adversarial(read_rows(sys.argv[2]))
        return 0
    if len(sys.argv) != 2:
        raise SystemExit(
            "usage: proof0_probe.py REPORT.jsonl | "
            "--adversarial REPORT.jsonl | --corpus REPORT.jsonl"
        )
    rows = read_rows(sys.argv[1])
    if len(rows) != 17:
        raise ValueError(f"expected 17 loop summaries, saw {len(rows)}")
    if any(row["complete"] != 1 or row["reason"] != "" for row in rows):
        raise ValueError("focused PROOF0 summary is incomplete")

    require(rows[0], bindings=4, shadows=1, shadow_reads=2,
            ifs=2, cpu_effects=1, agx_effects=1)
    require(rows[1], fors=1, breaks=1, continues=1,
            max_private_depth=1, cpu_effects=1, agx_effects=1)
    require(rows[2], capture_writes=1, outer_exits=2,
            cpu_effects=0, agx_effects=0)
    require(rows[3], legacy_reason="assign-carried", capture_writes=1,
            cpu_effects=0, agx_effects=0)
    require(rows[4], legacy_dispatched=1, calls=1, unknown_calls=0,
            cpu_effects=1, agx_effects=0)
    require(rows[5], legacy_reason="extern-call", calls=1, unknown_calls=1,
            cpu_effects=0, agx_effects=0)
    require(rows[6], conditional_stores=1,
            cpu_effects=1, agx_effects=0)
    require(rows[7], legacy_reason="private-base", local_index_bases=1,
            cpu_effects=1, agx_effects=0)
    require(rows[8], shadows=1, shadow_reads=2, conditional_stores=1,
            cpu_effects=1, agx_effects=0)
    require(rows[9], member_reads=1, index_reads=1,
            cpu_effects=1, agx_effects=0)
    require(rows[10], iterator_writes=1,
            cpu_effects=0, agx_effects=0)
    require(rows[11], member_writes=1, capture_member_writes=1,
            cpu_effects=0, agx_effects=0)
    require(rows[12], ifs=5, max_control_depth=5, conditional_stores=1,
            cpu_effects=1, agx_effects=0)
    require(rows[13], whiles=1, max_private_depth=1, max_control_depth=1,
            cpu_effects=1, agx_effects=0)
    require(rows[14], returns=1, cpu_effects=0, agx_effects=0)
    require(rows[15], shadows=1, shadow_reads=1,
            cpu_effects=1, agx_effects=1)
    require(rows[16], qmarks=1, calls=1, effectful_calls=1,
            cpu_effects=0, agx_effects=0)

    if any(row["affine_pending"] != (row["index_writes"] != 0) for row in rows):
        raise ValueError("affine-pending marker diverged from indexed writes")
    print("PROOF0-a PASS: scope identity and recursive effects pinned")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
