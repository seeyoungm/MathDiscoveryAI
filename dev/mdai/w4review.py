"""Readiness-only audit of the pinned, A/B development fixed ABC action labels."""
from __future__ import annotations

from collections import Counter
import argparse
from hashlib import sha256
import json
from pathlib import Path
import re

from .abc_runner import RECIPES, decode_binary, encode_binary
from .aig import AIG, verify_aig_exact
from .candidates import seed_graph
from .devgen import partition_groups
from .selector import check_adequacy, split_for_group

MANIFEST_SHA = "34951a26fdf0a6067558931484915ea027a797cdc9f9d51f71eee01c0ece4077"
ORIGINAL_COHORT_SHA = "4675f029da7da723795a0e1bc52ab0f2d2bca9025c338f659554eb8758647e84"
ORIGINAL_ROWS_SHA = "246d28ac3b138dc7236ea876459b9506c8e151b7f0b9b67f0204ec15d05de95e"
EXPANSION_COHORT_SHA = "485956ccc0dd7bbcef9a5c4f5971108989236f04f2925c883e58458a3fa4849a"
SELECTOR_SHA = "05d35fb4883506dbfbea52b60b45ff7ca724094e8a3704566e4a24eddd5c1293"
SALT = "w4-small-selector-v1"
REPEATS = (0, 1, 2)
ACTIONS = tuple(RECIPES)


def strict_json(data):
    def pairs(xs):
        result = {}
        for k, v in xs:
            if k in result:
                raise ValueError("duplicate JSON key")
            result[k] = v
        return result

    def invalid(x):
        raise ValueError(f"invalid non-finite JSON value: {x}")
    return json.loads(data, object_pairs_hook=pairs, parse_constant=invalid)


def pinned(path, expected):
    if type(expected) is not str or not re.fullmatch("[0-9a-f]{64}", expected):
        raise ValueError("an explicit lowercase SHA-256 pin is required")
    data = path.read_bytes()
    if sha256(data).hexdigest() != expected:
        raise ValueError(f"pin mismatch: {path.name}")
    return data


def jsonl(data):
    return [strict_json(line) for line in data.splitlines()]


def cohort_records(manifest, original, expanded):
    components = partition_groups(manifest)
    if set(components) != {"development"}:
        raise ValueError("manifest is not development only")
    by_id = {c[0]["task_id"]: c[0] for c in components["development"] if len(c) == 1}
    oids, nids = original["task_ids"], expanded["task_ids"]
    if (type(oids) is not list or type(nids) is not list or not oids or not nids or
            len(set(oids + nids)) != len(oids + nids) or not set(oids + nids) <= set(by_id)):
        raise ValueError("cohort overlap, duplicate, or non-independent development task")
    previous = [by_id[t] for t in oids]
    new = [by_id[t] for t in nids]
    for records, spec in ((previous, original), (new, expanded)):
        if Counter(r["family"] for r in records) != spec["family_counts"]:
            raise ValueError("cohort family counts differ from manifest")
        if any(r["family"] not in ("A", "B") or r["task_id"] != r["root_id"] or
               r.get("parent_id") is not None for r in records):
            raise ValueError("not an independent A/B development root")
    expected_groups = [dict(task_id=r["task_id"], group_id=r["canonical_id"],
                            family=r["family"], root_id=r["root_id"]) for r in new]
    if (expanded.get("outcome_blind") is not True or expanded.get("selector_split_salt") != SALT or
            expanded.get("groups") != expected_groups or
            any(split_for_group(r["canonical_id"], salt=SALT) != "dev" for r in new)):
        raise ValueError("expansion violates frozen validation group identities")
    return previous, new


def validate_rows(rows, records):
    records_by_id = {r["task_id"]: r for r in records}
    expected = {(t, a, repeat) for t in records_by_id for a in ACTIONS for repeat in REPEATS}
    features = {}
    for task, record in records_by_id.items():
        graph = seed_graph(record)
        m = graph.metrics()
        features[task] = dict(input_bits=len(graph.input_literals), output_bits=len(graph.outputs),
                              source_and_count=m.and_count, source_depth=m.depth)
    indexed = {}
    for row in rows:
        if type(row) is not dict or set(row) != {"task_id", "group_id", "family", "features",
                                                   "action", "repeat", "partition", "outcome"}:
            raise ValueError("unexpected label schema")
        t, a, rep = row["task_id"], row["action"], row["repeat"]
        if type(t) is not str or type(a) is not str or type(rep) is not int:
            raise ValueError("invalid task/action/repeat key")
        key = (t, a, rep)
        if key not in expected or key in indexed:
            raise ValueError("out-of-cohort, unknown-action, or duplicate label")
        record = records_by_id[t]
        if (row["partition"] != "development" or row["family"] != record["family"] or
                row["group_id"] != record["canonical_id"] or type(row["features"]) is not dict or
                row["features"] != features[t] or any(type(v) is not int for v in row["features"].values())):
            raise ValueError("label provenance or seed feature mismatch")
        values = row["outcome"]
        if (type(values) is not dict or set(values) != {"equivalent", "depth_ok", "and_reduction"} or
                values["equivalent"] is not True or type(values["depth_ok"]) is not bool or
                type(values["and_reduction"]) is not int or values["and_reduction"] > features[t]["source_and_count"]):
            raise ValueError("row lacks a verified equivalence/cost/depth label")
        indexed[key] = row
    if set(indexed) != expected:
        raise ValueError("incomplete task/action/repeat cross product")
    return indexed


def readiness(original_rows, expansion_rows, previous, new):
    first, second = validate_rows(original_rows, previous), validate_rows(expansion_rows, new)
    if first.keys() & second.keys():
        raise ValueError("joined rows overlap")
    rows = [*first.values(), *second.values()]
    report = check_adequacy(rows, ACTIONS, salt=SALT)
    partitions = {s: {f: {row["group_id"] for row in rows if row["family"] == f and
                              split_for_group(row["group_id"], salt=SALT) == s}
                      for f in ("A", "B")} for s in ("train", "dev")}
    validation_ok = all(n >= 2 for n in report["verified_groups_per_action"]["dev"].values())
    return dict(schema="w4-review-readiness-v1", scope="fixed ABC recipe development readiness only",
                action_labels_verified=True, formal_gate3_pass=False, actual_fit_run=False,
                fit_released=False, repeat_schedule=list(REPEATS), original_rows=len(first),
                expanded_rows=len(second), group_split_salt=SALT, assessment=report,
                independent_groups_by_family={s: {f: len(g) for f, g in d.items()}
                                              for s, d in partitions.items()},
                dev_two_verified_groups_per_action=validation_ok,
                offline_data_adequate=report["ready"] and validation_ok)


def verify_recipe_artifacts(row, record, receipt, input_data, output_data):
    """Require an on-time true ABC receipt and independently recheck the actual circuit."""
    source = seed_graph(record)
    ms = source.metrics()
    if input_data != encode_binary(source):
        raise ValueError("ABC artifact does not encode the frozen source graph")
    recipe = row["action"]
    expected_command = "; ".join(("read_aiger input.aig", *RECIPES[recipe], "write_aiger -s output.aig"))
    if (receipt.get("recipe") != recipe or receipt.get("literal_commands") != expected_command or
            receipt.get("status") != "VERIFIED" or receipt.get("equivalent") is not True or
            receipt.get("input_metrics") != vars(ms) or
            type(receipt.get("elapsed_s")) not in (int, float) or
            type(receipt.get("budget_s")) not in (int, float) or
            not 0 <= receipt["elapsed_s"] < receipt["budget_s"] <= 20):
        raise ValueError("missing valid bounded ABC receipt")
    raw = decode_binary(output_data, require_labels=True)
    if (len(raw.input_literals), len(raw.outputs)) != (len(source.input_literals), len(source.outputs)):
        raise ValueError("ABC output interface changed")
    ports = "".join(f"i{j} {name}\n" for j, name in enumerate(source.input_names))
    checked = AIG.from_aag(raw.to_aag(symbols=False) + ports)
    out = checked.metrics()
    depth_ok = out.depth <= ms.depth
    if (receipt.get("output_metrics") != vars(out) or receipt.get("eligible") is not depth_ok or
            row["outcome"] != dict(equivalent=True, depth_ok=depth_ok,
                                   and_reduction=ms.and_count - out.and_count)):
        raise ValueError("ABC receipt or label contradicts measured output costs")
    exact = verify_aig_exact(source, checked)
    if exact.status != "EQUIVALENT":
        raise ValueError("replayed exact check did not prove equivalence")


def _contained(root, relative):
    p = Path(relative)
    if not p.parts or p.is_absolute() or ".." in p.parts:
        raise ValueError("untrusted expansion artifact path")
    dest = root / p
    if dest.resolve() != dest:
        raise ValueError("expansion artifact path escapes or aliases expected location")
    return dest


def verify_expansion(root, expected_summary_sha, rows_by_task_record):
    """Load every expected expansion row, verify pinned evidence, and recheck equivalence."""
    root = root.resolve()
    s = strict_json(pinned(_contained(root, "summary.json"), expected_summary_sha))
    if (s.get("terminal_status") != "VERIFIED_DEVELOPMENT_ONLY" or
            s.get("cohort_sha256") != EXPANSION_COHORT_SHA or s.get("source_manifest_sha256") != MANIFEST_SHA or
            s.get("complete_action_labels") is not True or s.get("final_holdout") is not False):
        raise ValueError("missing verified expansion completion on the frozen cohort")
    prereg = strict_json(pinned(_contained(root, "preregistered.json"), s["preregistered_sha256"]))
    if (prereg.get("cohort_sha256") != EXPANSION_COHORT_SHA or
            prereg.get("source_manifest_sha256") != MANIFEST_SHA or
            prereg.get("actions") != {a: list(RECIPES[a]) for a in ACTIONS} or
            prereg.get("repeats") != list(REPEATS) or prereg.get("total_per_task_repeat_s") != 60 or
            prereg.get("max_per_action_s") != 20 or prereg.get("max_verification_s") != 5 or
            prereg.get("no_retry") is not True or prereg.get("heldout_accessed") is not False or
            prereg.get("end_to_end_live_model") is not False):
        raise ValueError("expansion preregistration violates the fixed development protocol")
    n = len(rows_by_task_record) * len(ACTIONS) * len(REPEATS)
    if any(s["counts"].get(k) != n for k in ("expected", "observed")) or s.get("statuses") != {"VERIFIED": n}:
        raise ValueError("expansion did not prove all expected rows")
    index = strict_json(pinned(_contained(root, "receipt_index.json"), s["receipt_index_sha256"]))
    rows = jsonl(pinned(_contained(root, "abc_action_observations.jsonl"), s["rows_sha256"]))
    if (index.get("schema") != "w4-expansion-index-v1" or
            type(index.get("rows")) is not list or len(rows) != n or len(index["rows"]) != n):
        raise ValueError("incomplete expansion evidence index")
    validate_rows(rows, rows_by_task_record.values())
    seen_receipts, runner_cache = set(), {}
    task_positions = {t: i for i, t in enumerate(rows_by_task_record)}
    for i, (row, evidence) in enumerate(zip(rows, index["rows"])):
        task, repeat, action = (row[k] for k in ("task_id", "repeat", "action"))
        canonical = json.dumps(row, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()
        if (type(evidence.get("row_index")) is not int or evidence["row_index"] != i or
                evidence.get("row_sha256") != sha256(canonical).hexdigest() or
                any(evidence.get(k) != row[k] for k in ("task_id", "repeat", "action")) or
                evidence.get("status") != "VERIFIED" or
                evidence.get("runner_attempt_status") not in {"VERIFIED", "DUPLICATE", "DEPTH_CAP"}):
            raise ValueError("source evidence does not match indexed label")
        case = f"case-{task_positions[task]:02d}"
        repeat_folder = f"{case}/repeat-{repeat}"
        raw_path = f"{repeat_folder}/abc-{ACTIONS.index(action) + 1:02d}/receipt.json"
        if (evidence.get("seed_path") != f"{case}/seed.aag" or evidence.get("receipt_path") != raw_path or
                evidence.get("runner_receipt_path") != f"{repeat_folder}/receipt.json"):
            raise ValueError("evidence path differs from fixed task/repeat/action mapping")
        receipt_path = _contained(root, evidence["receipt_path"])
        if receipt_path in seen_receipts:
            raise ValueError("ABC evidence reused across different action trials")
        seen_receipts.add(receipt_path)
        receipt = strict_json(pinned(receipt_path, evidence["receipt_sha256"]))
        seed_path = _contained(root, evidence["seed_path"])
        record = rows_by_task_record[task]
        source = seed_graph(record)
        if pinned(seed_path, evidence["seed_sha256"]) != source.to_aag(symbols=True).encode():
            raise ValueError("persisted source does not match frozen seed")
        rp = _contained(root, evidence["runner_receipt_path"])
        rk = (rp, evidence["runner_receipt_sha256"])
        if rk not in runner_cache:
            runner_cache[rk] = strict_json(pinned(*rk))
        runner = runner_cache[rk]
        key = runner.get("key", {})
        matched = [a for a in runner.get("attempts", []) if a.get("id") == action]
        if (runner.get("terminal_status") != "PORTFOLIO_EXHAUSTED" or key.get("arm") != "abc" or
                key.get("task_id") != task or key.get("repeat") != repeat or key.get("seed") != 1000 + repeat or
                key.get("budget_seconds") != 60 or key.get("max_candidates") != 20 or
                key.get("abc_recipes") != list(ACTIONS) or key.get("abc_path") != prereg["abc"]["path"] or
                receipt.get("abc_path") != prereg["abc"]["path"] or
                [a.get("id") for a in runner.get("attempts", [])] != list(ACTIONS) or len(matched) != 1 or
                matched[0].get("status") != evidence["runner_attempt_status"]):
            raise ValueError("runner receipt does not prove this trial was completed")
        inp = pinned(_contained(root, str((receipt_path.parent / "input.aig").relative_to(root))), evidence["input_sha256"])
        out = pinned(_contained(root, str((receipt_path.parent / "output.aig").relative_to(root))), evidence["output_sha256"])
        verify_recipe_artifacts(row, record, receipt, inp, out)
    return rows, dict(expansion_summary_sha256=expected_summary_sha, expansion_rows_sha256=s["rows_sha256"],
                      expansion_receipt_index_sha256=s["receipt_index_sha256"],
                      expansion_preregistration_sha256=s["preregistered_sha256"])


def main():
    arg = argparse.ArgumentParser(description=__doc__)
    arg.add_argument("--expansion-summary-sha256", required=True)
    ns = arg.parse_args()
    dev = Path(__file__).resolve().parents[1]
    evidence = dev / "evidence"
    prior = evidence / "week3" / "matched_dev_controls"
    expansion = evidence / "week4_validation_expansion"
    manifest_path = dev.parent / "records/week2/week2_smoke/dataset_manifest.jsonl"
    manifest = jsonl(pinned(manifest_path, MANIFEST_SHA))
    original = strict_json(pinned(prior / "cohort.json", ORIGINAL_COHORT_SHA))
    frozen = strict_json(pinned(evidence / "week4_validation_freeze/cohort.json", EXPANSION_COHORT_SHA))
    pinned(dev / "mdai/selector.py", SELECTOR_SHA)
    if original.get("source_hash") != MANIFEST_SHA or frozen.get("source_manifest_sha256") != MANIFEST_SHA:
        raise ValueError("cohort and source manifest pins disagree")
    previous, new = cohort_records(manifest, original, frozen)
    original_rows = jsonl(pinned(prior / "abc_action_observations.jsonl", ORIGINAL_ROWS_SHA))
    new_rows, pins = verify_expansion(expansion, ns.expansion_summary_sha256, {r["task_id"]: r for r in new})
    report = readiness(original_rows, new_rows, previous, new)
    report["provenance"] = dict(manifest_sha256=MANIFEST_SHA, original_cohort_sha256=ORIGINAL_COHORT_SHA,
                                original_rows_sha256=ORIGINAL_ROWS_SHA, expansion_cohort_sha256=EXPANSION_COHORT_SHA,
                                selector_sha256=SELECTOR_SHA, **pins)
    print(json.dumps(report, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
