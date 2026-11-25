"""Descriptive top-1 headroom on logged A/B development sets. No model fitting."""
from collections import defaultdict
from fractions import Fraction

from .selector import split_for_group
from .w4review import ACTIONS, SALT


def describe(original_index, expansion_index):
    """Accept Cartesian, proof-audited row indexes from w4review; never alter split."""
    grouped = defaultdict(dict)
    for source, indexed in (("original", original_index), ("new", expansion_index)):
        for row in indexed.values():
            group = row["group_id"]
            key = source, split_for_group(group, salt=SALT), group, row["task_id"], row["repeat"]
            action = row["action"]
            if action in grouped[key]:
                raise ValueError("repeated action for the same trial")
            result = row["outcome"]
            grouped[key][action] = (
                max(0, result["and_reduction"]) if result["equivalent"] and result["depth_ok"] else 0
            )
    if any(set(m) != set(ACTIONS) for m in grouped.values()):
        raise ValueError("incomplete action menu")

    def aggregate(keys, score):
        groups = defaultdict(list)
        for k in keys:
            groups[k[2]].append(score(grouped[k]))
        if not groups:
            raise ValueError("empty diagnostic partition")
        return sum((Fraction(sum(w), len(w)) for w in groups.values()), Fraction(0)) / len(groups)

    train = [k for k in grouped if k[1] == "train"]
    static = min(ACTIONS, key=lambda a: (-aggregate(train, lambda scores: scores[a]), a))
    out = {}
    for name, keys in (
        ("prior_dev", [k for k in grouped if k[:2] == ("original", "dev")]),
        ("new_dev", [k for k in grouped if k[:2] == ("new", "dev")]),
        ("combined_dev", [k for k in grouped if k[1] == "dev"]),
    ):
        fixed = aggregate(keys, lambda m: m[static])
        oracle = aggregate(keys, lambda m: max(m.values()))
        out[name] = {
            "groups": len({k[2] for k in keys}), "task_repeats": len(keys),
            "mean_AND_saved_static": str(fixed), "mean_AND_saved_oracle": str(oracle),
            "mean_maximum_top1_gain": str(oracle-fixed),
            "trials_oracle_strictly_beats_static": sum(max(grouped[k].values()) > grouped[k][static] for k in keys),
        }
    return {"schema":"w4-logged-top1-headroom-v1",
            "scope":"post-run descriptive diagnostic on development only; fixed ABC menu and observed rewards",
            "baseline_chosen_from_original_train_only":static, "partitions":out,
            "model_fit":False, "measures_runtime_or_generalization":False}


def main():
    import argparse
    import json
    from pathlib import Path

    from . import w4review as review

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--expansion-summary-sha256", required=True)
    args = parser.parse_args()
    dev = Path(__file__).resolve().parents[1]
    evidence = dev / "evidence"
    original = evidence / "week3/matched_dev_controls"
    freeze = evidence / "week4_validation_freeze/cohort.json"
    manifest = dev.parent / "records/week2/week2_smoke/dataset_manifest.jsonl"
    prev, new = review.cohort_records(
        review.jsonl(review.pinned(manifest, review.MANIFEST_SHA)),
        review.strict_json(review.pinned(original / "cohort.json", review.ORIGINAL_COHORT_SHA)),
        review.strict_json(review.pinned(freeze, review.EXPANSION_COHORT_SHA)))
    review.pinned(dev / "mdai/selector.py", review.SELECTOR_SHA)
    old_rows = review.jsonl(review.pinned(original / "abc_action_observations.jsonl",
                                        review.ORIGINAL_ROWS_SHA))
    new_rows, pins = review.verify_expansion(evidence / "week4_validation_expansion",
                                            args.expansion_summary_sha256,
                                            {r["task_id"]:r for r in new})
    result = describe(review.validate_rows(old_rows, prev), review.validate_rows(new_rows, new))
    result["provenance"] = {"original_rows_sha256":review.ORIGINAL_ROWS_SHA, **pins}
    print(json.dumps(result, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
