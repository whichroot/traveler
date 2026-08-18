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
    if len(rows) != 410:
        raise ValueError(f"expected 410 corpus summaries, saw {len(rows)}")
    if any(row.get("line", 0) <= 0 or row.get("col", 0) <= 0 for row in rows):
        raise ValueError("PROOF1 emitted a missing/invalid source location")
    dispatched = sum(row["legacy_dispatched"] for row in rows)
    cpu = sum(row["cpu_effects"] for row in rows)
    agx = sum(row["agx_effects"] for row in rows)
    cpu_candidate = sum(row["cpu_candidate"] for row in rows)
    cpu_dispatched = sum(row["cpu_dispatched"] for row in rows)
    agx_candidate = sum(row["agx_candidate"] for row in rows)
    lang1_candidate = sum(row["lang1_candidate"] for row in rows)
    independent = sum(row["legacy_independent"] for row in rows)
    if dispatched != 207:
        raise ValueError(
            f"legacy dispatch baseline changed: dispatched={dispatched}, "
            f"cpu={cpu}, agx={agx}"
        )
    incomplete = [row for row in rows if row["complete"] == 0]
    if incomplete:
        raise ValueError("unexpected whole-corpus summary incompleteness")
    if (cpu, agx) != (263, 180):
        raise ValueError(
            f"PROOF0 effect baseline changed: cpu={cpu} agx={agx}"
        )
    call_totals = tuple(
        sum(row[key] for row in rows)
        for key in ("calls", "unknown_calls", "effectful_calls",
                    "read_calls", "addresses")
    )
    if call_totals != (148, 32, 68, 1, 14):
        raise ValueError(f"LANG1 call-effect totals changed: {call_totals}")
    widths = tuple(
        sum(row["iterator_width"] == width for row in rows)
        for width in (0, 32, 64)
    )
    if widths != (1, 403, 6):
        raise ValueError(f"LANG1 iterator-width census changed: {widths}")
    cpu_added = sum(
        row["cpu_candidate"] == 1 and row["legacy_independent"] == 0
        for row in rows
    )
    cpu_removed = sum(
        row["cpu_candidate"] == 0 and row["legacy_independent"] == 1
        for row in rows
    )
    census = (independent, cpu_candidate, agx_candidate, cpu_added, cpu_removed)
    if census != (207, 217, 164, 10, 0):
        raise ValueError(f"PROOF1 candidate census changed: {census}")
    c1_inlineable = sum(row["c1_inlineable_calls"] for row in rows)
    c1_refusals = {
        reason: sum(row["c1_refusal"] == reason for row in rows)
        for reason in ("", "callee-shape", "closure-shape", "short-circuit")
    }
    if (lang1_candidate, c1_inlineable, c1_refusals) != (
        179,
        38,
        {"": 400, "callee-shape": 9, "closure-shape": 1,
         "short-circuit": 0},
    ):
        raise ValueError(
            "LANG1-C1 candidate/refusal census changed: "
            f"{lang1_candidate}, {c1_inlineable}, {c1_refusals}"
        )
    print(
        "LANG1-C1 census: "
        f"candidates={lang1_candidate} inlineable={c1_inlineable} "
        f"refusals={c1_refusals}"
    )
    operationally_blocked = [
        row for row in rows
        if row["cpu_candidate"] == 1 and row["cpu_dispatched"] == 0
    ]
    if cpu_dispatched != 217 or operationally_blocked:
        raise ValueError(
            "PROOF1 operational census changed: "
            f"dispatched={cpu_dispatched} blocked={operationally_blocked}"
        )
    prior_workers = [row for row in rows if row["legacy_dispatched"] == 1]
    parity_keys = (
        "capture_set_parity", "capture_order_parity",
        "write_set_parity", "write_order_parity",
    )
    for key in parity_keys:
        mismatches = [row for row in prior_workers if row[key] != 1]
        if mismatches:
            raise ValueError(
                f"PROOF1 {key} failed for {len(mismatches)} prior workers"
            )
    print(
        "PROOF1 candidate census: "
        f"legacy-independent={independent} cpu={cpu_candidate} "
        f"agx={agx_candidate} cpu-added={cpu_added} cpu-removed={cpu_removed}"
    )
    seen: dict[tuple[str, str, str], int] = {}
    added_ids: list[str] = []
    removed_ids: list[str] = []
    width_excluded_ids: list[str] = []
    for row in rows:
        base = (row["source"], row["fn"], row["var"])
        ordinal = seen.get(base, 0) + 1
        seen[base] = ordinal
        stable_id = f"{base[0]}:{base[1]}:{base[2]}#{ordinal}"
        if row["cpu_candidate"] == 1 and row["legacy_independent"] == 0:
            added_ids.append(stable_id)
        if row["cpu_candidate"] == 0 and row["legacy_independent"] == 1:
            removed_ids.append(stable_id)
        if (row["cpu_candidate"] == 1 and row["agx_candidate"] == 0
                and row["iterator_width"] != 32):
            width_excluded_ids.append(stable_id)
    expected_added = [
        "examples/genus_alias_test.tv:onset_field:i#1",
        "examples/genus_probe_test.tv:main:i#2",
        "examples/genus_probe_test.tv:main:i#3",
        "examples/poly_core_generic_test.tv:forward_sum_F251:ki#1",
        "examples/poly_core_generic_test.tv:forward_sum_F65521:ki#1",
        "examples/poly_core_generic_test.tv:regime_detect_F251:k#1",
        "src/lib/crypto/mds_check.tv:mds_build_matrix_dyn:j#1",
        "src/lib/crypto/poseidon2_wide.tv:mds_external_w_dyn:k#1",
        "src/lib/features/relational.tv:mat_trace_pow_dyn:j#1",
        "src/lib/genus/genus_alias.tv:genus_onset_modp:i#1",
    ]
    expected_removed: list[str] = []
    if added_ids != expected_added or removed_ids != expected_removed:
        raise ValueError(
            f"LANG1 exact candidate delta changed: "
            f"added={added_ids} removed={removed_ids}"
        )
    operational_added_ids: list[str] = []
    operational_added_caps: dict[str, list[str]] = {}
    seen.clear()
    for row in rows:
        base = (row["source"], row["fn"], row["var"])
        ordinal = seen.get(base, 0) + 1
        seen[base] = ordinal
        if row["cpu_dispatched"] == 1 and row["legacy_dispatched"] == 0:
            stable_id = f"{base[0]}:{base[1]}:{base[2]}#{ordinal}"
            operational_added_ids.append(stable_id)
            operational_added_caps[stable_id] = row["proof_captures"]
    if operational_added_ids != expected_added:
        raise ValueError(
            f"PROOF1 exact operational delta changed: {operational_added_ids}"
        )
    expected_added_caps = {
        expected_added[0]: ["data", "p", "red"],
        expected_added[1]: ["P", "data"],
        expected_added[2]: ["P", "data"],
        expected_added[3]: ["order", "coeffs", "reg"],
        expected_added[4]: ["order", "coeffs", "reg"],
        expected_added[5]: ["order", "data", "reg"],
        expected_added[6]: ["i", "t", "v", "m", "__field"],
        expected_added[7]: ["state", "nchunks", "sums", "__field"],
        expected_added[8]: ["m", "i", "P", "M", "Q", "__field"],
        expected_added[9]: ["data", "p", "red"],
    }
    if operational_added_caps != expected_added_caps:
        raise ValueError(
            f"PROOF1 addition capture order changed: {operational_added_caps}"
        )
    expected_width_excluded = [
        "examples/for_i64_bounds.tv:main:j#1",
        "tests/pfor/pfor_i64_bounds.tv:main:j#1",
    ]
    if width_excluded_ids != expected_width_excluded:
        raise ValueError(
            f"LANG1 AGX iterator-width exclusions changed: {width_excluded_ids}"
        )
    restored = [
        row for row in rows
        if row["source"] == "examples/closure_prove_through.tv"
        and row["fn"] == "closure_map"
    ]
    if len(restored) != 1:
        raise ValueError("LANG1 closure_map corpus identity changed")
    require(restored[0], legacy_independent=1, unknown_calls=0,
            cpu_effects=1, affine_safe=1, cpu_candidate=1)
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
        "2065dd9d905d8f59f46537e51f2a1fc31adeb8017c533789855f400dbdcddb77"
    )
    if digest != expected_digest:
        raise ValueError(f"PROOF0 per-record baseline changed: sha256={digest}")
    print(
        "PROOF1 corpus PASS: 410 records, "
        f"effects cpu={cpu} agx={agx}, dispatch 207->217, "
        "prior capture/write parity 207/207"
    )


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


def check_closures(rows: list[dict]) -> None:
    if len(rows) != 8:
        raise ValueError(f"expected 8 closure summaries, saw {len(rows)}")
    if any(row["complete"] != 1 or row["reason"] != "" for row in rows):
        raise ValueError("focused LANG1 closure summary is incomplete")

    require(rows[0], legacy_independent=1, legacy_reason="", calls=1,
            unknown_calls=0, effectful_calls=0, read_calls=0,
            cpu_effects=1, affine_safe=1, cpu_candidate=1)
    require(rows[1], legacy_independent=1, legacy_reason="", calls=1,
            unknown_calls=0, effectful_calls=0, read_calls=0,
            cpu_effects=1, affine_safe=1, cpu_candidate=1)
    require(rows[2], legacy_independent=0, legacy_reason="mutating-call",
            calls=1, unknown_calls=0, effectful_calls=1, read_calls=0,
            cpu_effects=0, affine_safe=1, cpu_candidate=0)
    require(rows[3], legacy_independent=0, legacy_reason="indirect-call",
            calls=1, unknown_calls=1, effectful_calls=0, read_calls=0,
            cpu_effects=0, affine_safe=1, cpu_candidate=0)
    require(rows[4], legacy_independent=0, legacy_reason="mutating-call",
            calls=1, unknown_calls=0, effectful_calls=1, read_calls=0,
            cpu_effects=0, affine_safe=1, cpu_candidate=0)
    require(rows[5], legacy_independent=0, legacy_reason="call-read",
            calls=1, unknown_calls=0, effectful_calls=0, read_calls=1,
            cpu_effects=1, affine_safe=0, affine_reason="call-read",
            cpu_candidate=0)
    require(rows[6], legacy_independent=0, legacy_reason="call-read",
            calls=1, unknown_calls=0, effectful_calls=0, read_calls=1,
            cpu_effects=1, affine_safe=0, affine_reason="call-read",
            cpu_candidate=0)
    require(rows[7], legacy_independent=1, legacy_reason="", calls=1,
            unknown_calls=0, effectful_calls=0, read_calls=0,
            cpu_effects=1, affine_safe=1, cpu_candidate=1,
            proof_elem_ok=1, cpu_dispatched=1)
    print("LANG1 closure PASS: direct identities cross and erased/effectful calls refuse")


def check_static_calls(rows: list[dict]) -> None:
    if len(rows) != 10:
        raise ValueError(f"expected 10 static-call summaries, saw {len(rows)}")
    if any(row["complete"] != 1 or row["reason"] != "" for row in rows):
        raise ValueError("focused LANG1 static-call summary is incomplete")

    require(rows[0], legacy_independent=1, legacy_reason="", calls=1,
            overload_ops=0, unknown_calls=0, effectful_calls=0,
            addresses=0, cpu_effects=1, affine_safe=1, cpu_candidate=1)
    require(rows[1], legacy_independent=1, legacy_reason="", calls=1,
            overload_ops=0, unknown_calls=0, effectful_calls=0,
            addresses=0, cpu_effects=1, affine_safe=1, cpu_candidate=1)
    require(rows[2], legacy_independent=0, legacy_reason="extern-call", calls=1,
            overload_ops=0, unknown_calls=0, effectful_calls=0,
            addresses=0, cpu_effects=1, affine_safe=1, cpu_candidate=1,
            proof_elem_ok=0, cpu_dispatched=0, cpu_reason="cap-elem")
    require(rows[3], legacy_independent=0, legacy_reason="mutating-call", calls=1,
            overload_ops=1, unknown_calls=0, effectful_calls=0,
            addresses=0, cpu_effects=1, affine_safe=1, cpu_candidate=1,
            proof_elem_ok=0, cpu_dispatched=0, cpu_reason="cap-elem")
    require(rows[4], legacy_independent=0, legacy_reason="extern-call", calls=1,
            overload_ops=0, unknown_calls=0, effectful_calls=0,
            addresses=1, cpu_effects=0, affine_safe=1, cpu_candidate=0)
    require(rows[5], legacy_independent=0, legacy_reason="unsupported-expr",
            shadows=1, calls=1, overload_ops=0, unknown_calls=0,
            effectful_calls=1, addresses=0, cpu_effects=0, cpu_candidate=0)
    require(rows[6], legacy_independent=0, legacy_reason="unsupported-expr",
            shadows=1, calls=1, overload_ops=1, unknown_calls=0,
            effectful_calls=1, addresses=0, cpu_effects=0, cpu_candidate=0)
    require(rows[7], legacy_independent=1, legacy_reason="", calls=0,
            overload_ops=0, unknown_calls=0, effectful_calls=0,
            addresses=1, cpu_effects=0, affine_safe=1, cpu_candidate=0,
            cpu_dispatched=0, cpu_reason="private-escape")
    require(rows[8], iterator_width=64, legacy_independent=1,
            legacy_reason="", calls=0, overload_ops=0, unknown_calls=0,
            effectful_calls=0, addresses=0, cpu_effects=1, agx_effects=0,
            affine_safe=1, cpu_candidate=1, agx_candidate=0)
    require(rows[9], legacy_independent=0, legacy_reason="extern-call", calls=1,
            overload_ops=0, unknown_calls=0, effectful_calls=0,
            addresses=0, cpu_effects=1, affine_safe=1, cpu_candidate=1,
            proof_elem_ok=0, cpu_dispatched=0, cpu_reason="cap-elem")
    print("LANG1 static-call PASS: direct, generic, trait, and operator targets resolve")


def check_lang1_hardware(rows: list[dict]) -> None:
    if len(rows) != 5:
        raise ValueError(f"expected 5 LANG1 hardware summaries, saw {len(rows)}")
    require(rows[0], complete=1, cpu_candidate=0, member_reads=3,
            private_member_reads=3, member_writes=1,
            private_member_writes=1, lang1_complete=1,
            lang1_effects=1, lang1_candidate=1)
    require(rows[1], complete=1, cpu_candidate=0, member_reads=1,
            private_member_reads=0, lang1_complete=1,
            lang1_effects=0, lang1_candidate=0)
    require(rows[2], complete=0, reason="match-bindings", matches=1,
            lang1_complete=1, lang1_effects=1, lang1_candidate=1)
    require(rows[3], complete=0, reason="match-bindings", matches=1,
            bindings=4, lang1_complete=1, lang1_effects=1,
            lang1_candidate=1)
    require(rows[4], complete=1, calls=1, inlineable_calls=1,
            lang1_complete=1, lang1_effects=1, lang1_candidate=1)
    print(
        "LANG1 hardware shadow PASS: private aggregates, match, and "
        "static inlining candidates are distinguished"
    )


def check_adversarial(rows: list[dict]) -> None:
    if len(rows) != 12:
        raise ValueError(f"expected 12 adversarial summaries, saw {len(rows)}")
    rows.sort(key=lambda row: (row["line"], row["col"]))
    if any(row["complete"] != 1 or row["reason"] != "" for row in rows):
        raise ValueError("adversarial PROOF0 summary is incomplete")
    require(rows[0], legacy_reason="mutating-call", calls=1,
            effectful_calls=1, cpu_effects=0, agx_effects=0)
    require(rows[1], alias_writes=1, cpu_effects=0, agx_effects=0)
    require(rows[2], legacy_reason="mutating-call", calls=1,
            overload_ops=1, unknown_calls=0, effectful_calls=1,
            cpu_effects=0, agx_effects=0)
    require(rows[3], short_circuits=4, max_control_depth=5,
            cpu_effects=1, agx_effects=0)
    require(rows[4], fors=1, continues=1, max_private_depth=1,
            max_control_depth=4, cpu_effects=1, agx_effects=1)
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
    require(rows[9], legacy_reason="call-read", calls=1, read_calls=1,
            cpu_effects=1, agx_effects=0, affine_safe=0,
            affine_reason="call-read", affine_call_reads=0,
            cpu_candidate=0, agx_candidate=0)
    require(rows[10], legacy_reason="mutating-call", calls=1,
            effectful_calls=1, cpu_effects=0, agx_effects=0,
            cpu_candidate=0, agx_candidate=0)
    require(rows[11], legacy_reason="mutating-call", calls=1,
            effectful_calls=1, cpu_effects=0, agx_effects=0)
    print("PROOF0-b adversarial PASS: false-safe counterexamples refused")


def check_dyn_capture(rows: list[dict]) -> None:
    if len(rows) != 1:
        raise ValueError(f"expected 1 dyn-capture summary, saw {len(rows)}")
    require(rows[0], complete=1, cpu_candidate=1, needs_dyn_field=1,
            proof_captures=["out", "__field"], proof_elem_ok=1,
            capture_set_parity=1, capture_order_parity=1,
            cpu_dispatched=1, cpu_reason="")
    print("PROOF1 dyn capture PASS: primitive map carries implicit __field")


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
    if len(sys.argv) == 3 and sys.argv[1] == "--closures":
        check_closures(read_rows(sys.argv[2]))
        return 0
    if len(sys.argv) == 3 and sys.argv[1] == "--static-calls":
        check_static_calls(read_rows(sys.argv[2]))
        return 0
    if len(sys.argv) == 3 and sys.argv[1] == "--lang1-hardware":
        check_lang1_hardware(read_rows(sys.argv[2]))
        return 0
    if len(sys.argv) == 3 and sys.argv[1] == "--dyn-capture":
        check_dyn_capture(read_rows(sys.argv[2]))
        return 0
    if len(sys.argv) != 2:
        raise SystemExit(
            "usage: proof0_probe.py REPORT.jsonl | "
            "--adversarial REPORT.jsonl | --affine REPORT.jsonl | "
            "--closures REPORT.jsonl | --static-calls REPORT.jsonl | "
            "--lang1-hardware REPORT.jsonl | --dyn-capture REPORT.jsonl | "
            "--corpus REPORT.jsonl"
        )
    rows = read_rows(sys.argv[1])
    if len(rows) != 17:
        raise ValueError(f"expected 17 loop summaries, saw {len(rows)}")
    rows.sort(key=lambda row: (row["line"], row["col"]))
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
            cpu_effects=0, agx_effects=0)
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
