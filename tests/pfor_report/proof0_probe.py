#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Pin PROOF0 lexical effects, affine hazards, and candidate sets."""

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
    cpu_candidate = sum(row["cpu_candidate"] for row in rows)
    agx_candidate = sum(row["agx_candidate"] for row in rows)
    independent = sum(row["legacy_independent"] for row in rows)
    if dispatched != 171:
        raise ValueError(
            f"legacy dispatch baseline changed: dispatched={dispatched}, "
            f"cpu={cpu}, agx={agx}"
        )
    incomplete = [row for row in rows if row["complete"] == 0]
    if len(incomplete) != 1 or incomplete[0]["reason"] != "unsupported-node":
        raise ValueError("unexpected whole-corpus summary incompleteness")
    if (cpu, agx) != (214, 146):
        raise ValueError(
            f"PROOF0 effect baseline changed: cpu={cpu} agx={agx}"
        )
    cpu_added = sum(
        row["cpu_candidate"] == 1 and row["legacy_independent"] == 0
        for row in rows
    )
    cpu_removed = sum(
        row["cpu_candidate"] == 0 and row["legacy_independent"] == 1
        for row in rows
    )
    census = (independent, cpu_candidate, agx_candidate, cpu_added, cpu_removed)
    if census != (171, 176, 132, 6, 1):
        raise ValueError(f"PROOF0-b candidate census changed: {census}")
    print(
        "PROOF0-b candidate census: "
        f"legacy-independent={independent} cpu={cpu_candidate} "
        f"agx={agx_candidate} cpu-added={cpu_added} cpu-removed={cpu_removed}"
    )
    seen: dict[tuple[str, str, str], int] = {}
    added_ids: list[str] = []
    removed_ids: list[str] = []
    for row in rows:
        base = (row["source"], row["fn"], row["var"])
        ordinal = seen.get(base, 0) + 1
        seen[base] = ordinal
        stable_id = f"{base[0]}:{base[1]}:{base[2]}#{ordinal}"
        if row["cpu_candidate"] == 1 and row["legacy_independent"] == 0:
            added_ids.append(stable_id)
        if row["cpu_candidate"] == 0 and row["legacy_independent"] == 1:
            removed_ids.append(stable_id)
    expected_added = [
        "examples/genus_alias_test.tv:onset_field:i#1",
        "examples/genus_probe_test.tv:main:i#2",
        "examples/genus_probe_test.tv:main:i#3",
        "examples/poly_core_generic_test.tv:forward_sum_F251:ki#1",
        "examples/poly_core_generic_test.tv:forward_sum_F65521:ki#1",
        "examples/poly_core_generic_test.tv:regime_detect_F251:k#1",
    ]
    expected_removed = [
        "examples/closure_prove_through.tv:closure_map:i#1",
    ]
    if added_ids != expected_added or removed_ids != expected_removed:
        raise ValueError(
            f"PROOF0-b exact candidate delta changed: "
            f"added={added_ids} removed={removed_ids}"
        )
    for label, selected in (
        ("cpu-added", [row for row in rows
                       if row["cpu_candidate"] == 1
                       and row["legacy_independent"] == 0]),
        ("cpu-removed", [row for row in rows
                         if row["cpu_candidate"] == 0
                         and row["legacy_independent"] == 1]),
    ):
        for row in selected:
            print(
                f"  {label}: source={row.get('source', '?')} "
                f"fn={row['fn']} line={row['line']} var={row['var']} "
                f"legacy={row['legacy_reason']} affine={row['affine_reason']}"
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
        "489e7a59de6d15215e42506248a153eaade32539a2466d4cfc699e028234b78a"
    )
    if digest != expected_digest:
        raise ValueError(f"PROOF0 per-record baseline changed: sha256={digest}")
    print(f"PROOF0-b corpus PASS: 346 records, effects cpu={cpu} agx={agx}")


def check_affine(rows: list[dict]) -> None:
    if len(rows) != 17:
        raise ValueError(f"expected 17 affine summaries, saw {len(rows)}")
    if any(row["complete"] != 1 or row["reason"] != "" for row in rows):
        raise ValueError("focused PROOF0-b affine summary is incomplete")

    require(rows[0], affine_safe=1, affine_reason="", affine_reads=1,
            affine_writes=1, cpu_candidate=1, agx_candidate=1)
    require(rows[1], affine_safe=1, affine_reason="", affine_reads=1,
            affine_writes=1, cpu_candidate=1, agx_candidate=1)
    require(rows[2], legacy_independent=0, shadows=1, affine_safe=1,
            affine_reason="", affine_reads=1, affine_writes=1,
            cpu_candidate=1, agx_candidate=1)
    require(rows[3], affine_safe=1, affine_reason="", affine_reads=2,
            affine_writes=2, cpu_candidate=1, agx_candidate=0)
    require(rows[4], affine_safe=0, affine_reason="waw", affine_reads=2,
            affine_writes=2, cpu_candidate=0, agx_candidate=0)
    require(rows[5], affine_safe=0, affine_reason="raw-war", affine_reads=2,
            affine_writes=1, cpu_candidate=0, agx_candidate=0)
    require(rows[6], affine_safe=0, affine_reason="wild-read", affine_reads=2,
            affine_writes=1, cpu_candidate=0, agx_candidate=0)
    require(rows[7], affine_safe=0, affine_reason="noninjective",
            affine_reads=1, affine_writes=0, cpu_candidate=0, agx_candidate=0)
    require(rows[8], affine_safe=0, affine_reason="const-idx", affine_reads=1,
            affine_writes=0, cpu_candidate=0, agx_candidate=0)
    require(rows[9], shadows=1, affine_safe=0, affine_reason="let-hidden",
            affine_reads=2, affine_writes=0, cpu_candidate=0, agx_candidate=0)
    require(rows[10], affine_safe=0, affine_reason="private-base",
            affine_reads=1, affine_writes=0, cpu_candidate=0, agx_candidate=0)
    require(rows[11], conditional_stores=1, affine_safe=1, affine_reads=1,
            affine_writes=1, cpu_candidate=1, agx_candidate=0)
    require(rows[12], conditional_stores=2, affine_safe=1, affine_reads=1,
            affine_writes=1, cpu_candidate=1, agx_candidate=0)
    require(rows[13], affine_safe=1, affine_reason="", affine_reads=2,
            affine_writes=1, cpu_candidate=1, agx_candidate=1)
    require(rows[14], calls=1, read_calls=1, affine_safe=1, affine_reads=0,
            affine_writes=1, affine_call_reads=1,
            cpu_candidate=1, agx_candidate=0)
    require(rows[15], affine_safe=0, affine_reason="index-wrap",
            affine_reads=1, affine_writes=1,
            cpu_candidate=0, agx_candidate=0)
    require(rows[16], affine_safe=0, affine_reason="unsupported-base",
            affine_reads=1, affine_writes=0, cpu_candidate=0, agx_candidate=0)
    print("PROOF0-b affine PASS: injectivity and RAW/WAR/WAW hazards pinned")


def check_adversarial(rows: list[dict]) -> None:
    if len(rows) != 10:
        raise ValueError(f"expected 10 adversarial summaries, saw {len(rows)}")
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
            cpu_effects=1, agx_effects=0, affine_safe=0,
            affine_reason="call-read", affine_call_reads=1,
            cpu_candidate=0, agx_candidate=0)
    require(rows[7], legacy_reason="call-read", calls=1, read_calls=1,
            cpu_effects=1, agx_effects=0, affine_safe=0,
            affine_reason="call-read", affine_call_reads=0,
            cpu_candidate=0, agx_candidate=0)
    require(rows[8], legacy_reason="call-read", calls=1, read_calls=1,
            cpu_effects=1, agx_effects=0, affine_safe=0,
            affine_reason="call-read", affine_call_reads=0,
            cpu_candidate=0, agx_candidate=0)
    require(rows[9], legacy_reason="mutating-call", calls=1,
            effectful_calls=1, cpu_effects=0, agx_effects=0)
    print("PROOF0-b adversarial PASS: false-safe counterexamples refused")


def main() -> int:
    if len(sys.argv) == 3 and sys.argv[1] == "--corpus":
        check_corpus(read_rows(sys.argv[2]))
        return 0
    if len(sys.argv) == 3 and sys.argv[1] == "--adversarial":
        check_adversarial(read_rows(sys.argv[2]))
        return 0
    if len(sys.argv) == 3 and sys.argv[1] == "--affine":
        check_affine(read_rows(sys.argv[2]))
        return 0
    if len(sys.argv) != 2:
        raise SystemExit(
            "usage: proof0_probe.py REPORT.jsonl | "
            "--adversarial REPORT.jsonl | --affine REPORT.jsonl | "
            "--corpus REPORT.jsonl"
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

    if any(row["cpu_candidate"] != row["cpu_effects"] * row["affine_safe"]
           or row["agx_candidate"] != row["agx_effects"] * row["affine_safe"]
           for row in rows):
        raise ValueError("combined PROOF0 candidate diverged from its proofs")
    require(rows[0], affine_safe=1, cpu_candidate=1, agx_candidate=1)
    require(rows[7], affine_safe=0, affine_reason="private-base",
            cpu_candidate=0, agx_candidate=0)
    require(rows[8], affine_safe=0, affine_reason="let-hidden",
            cpu_candidate=0, agx_candidate=0)
    print("PROOF0-b PASS: scoped effects and affine candidates pinned")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
