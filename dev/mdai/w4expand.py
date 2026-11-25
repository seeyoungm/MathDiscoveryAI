"""Fixed ABC verification trials on the hash-pinned, fresh development cohort."""
import argparse
from collections import Counter
from hashlib import sha256
import json
from pathlib import Path
import stat
import subprocess
import sys
from time import monotonic

from .abc_runner import RECIPES
from .aig import AIG
from .candidates import seed_graph
from .w3verify import verify_z3
from .w3runner import run_match

FROZEN_SHA = "485956ccc0dd7bbcef9a5c4f5971108989236f04f2925c883e58458a3fa4849a"
ROOT = Path(__file__).resolve().parents[1]
FREEZE = ROOT / "evidence" / "week4_validation_freeze" / "cohort.json"
DEST = ROOT / "evidence" / "week4_validation_expansion"
ACTIONS = ("strash_only", "balance_rewrite", "rewrite_refactor")
REPEATS = (0, 1, 2)
TOTAL_SECONDS = 60.0
PER_ACTION_SECONDS = 20.0
MAX_INPUT = 2_000_000
W3_COHORT = ROOT / "evidence" / "week3" / "matched_dev_controls" / "cohort.json"
REFERENCE_ABC = {
    "expected_package": "Debian trixie yosys-abc 0.52-2",
    "expected_version_line": "UC Berkeley, ABC 1.01 (compiled May  4 2025 16:37:33)",
}


def canon(obj):
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), allow_nan=False)


def sha(data):
    return sha256(data).hexdigest()


def bounded(path, limit=MAX_INPUT):
    if not stat.S_ISREG(path.stat().st_mode):
        raise ValueError(f"not a regular file: {path.name}")
    with path.open("rb") as f:
        data = f.read(limit + 1)
    if len(data) > limit:
        raise ValueError(f"oversized: {path.name}")
    return data


def pin(path):
    h = sha256()
    with path.open("rb") as f:
        for block in iter(lambda:f.read(128*1024), b""):
            h.update(block)
    return h.hexdigest()


def put(path, obj):
    with path.open("x", encoding="utf-8") as f:
        f.write(json.dumps(obj, indent=2, sort_keys=True, allow_nan=False) + "\n")


def read_cohort(source_path):
    freeze_bytes = bounded(FREEZE, 64_000)
    if sha(freeze_bytes) != FROZEN_SHA:
        raise ValueError("validation cohort digest mismatch")
    spec = json.loads(freeze_bytes)
    if spec.get("version") != "w4-validation-cohort-v1" or spec.get("outcome_blind") is not True:
        raise ValueError("invalid cohort contract")
    manifest = bounded(source_path)
    if sha(manifest) != spec["source_manifest_sha256"]:
        raise ValueError("development source manifest digest mismatch")
    rows = {}
    for line in manifest.splitlines():
        row = json.loads(line)
        task_id = row["task_id"]
        if task_id in rows or row["family"] not in ("A", "B") or row["split"] != "development":
            raise ValueError("duplicate or not permitted development row")
        rows[task_id] = row
    prior_ids = set(json.loads(bounded(W3_COHORT, 200_000))["task_ids"])
    chosen = []
    seen_groups = set()
    seen_roots = set()
    for group in spec["groups"]:
        task_id = group["task_id"]
        row = rows.get(task_id)
        if row is None or group["group_id"] != row["canonical_id"] or group["root_id"] != row["root_id"] or group["family"] != row["family"]:
            raise ValueError("validation group does not match source manifest")
        if task_id in prior_ids or row["canonical_id"] in seen_groups or row["root_id"] in seen_roots:
            raise ValueError("validation cohort overlap")
        seen_groups.add(row["canonical_id"])
        seen_roots.add(row["root_id"])
        chosen.append(row)
    if [r["task_id"] for r in chosen] != spec["task_ids"] or len(chosen) != 11:
        raise ValueError("cohort membership mismatch")
    if dict(Counter(r["family"] for r in chosen)) != {"A":4,"B":7}:
        raise ValueError("unexpected family count")
    return spec, chosen


def run(source, abc, abc_sha):
    if tuple(RECIPES) != ACTIONS or not abc.is_absolute():
        raise ValueError("changed action menu or unqualified ABC path")
    spec, chosen = read_cohort(source)
    verified_abc_sha = pin(abc)
    if abc_sha != verified_abc_sha:
        raise ValueError("ABC executable digest mismatch")
    version = subprocess.run([str(abc), "-s", "-c", "version"], text=True, capture_output=True,
                             check=True, timeout=5)
    if REFERENCE_ABC["expected_version_line"] not in version.stdout.splitlines():
        raise ValueError("ABC version drift")
    z3 = ROOT/"tools"/"z3_lib_cli.py"
    z3_version = subprocess.check_output([str(z3),"-version"],text=True,timeout=5).strip()
    if z3_version != "Z3 4.13.3.0":
        raise ValueError("Z3 version drift")
    DEST.mkdir(exist_ok=False)
    put(DEST/"preregistered.json",{
        "scope":"fixed genuine ABC actions on frozen development subset only",
        "cohort_sha256":FROZEN_SHA,"source_manifest_sha256":spec["source_manifest_sha256"],
        "abc":{**REFERENCE_ABC,"path":str(abc),"executable_sha256":verified_abc_sha,"version":version.stdout.strip()},
        "z3":{"path":str(z3),"version":z3_version,"bridge_sha256":pin(z3)},
        "python":sys.version.split()[0],"actions":{a:list(RECIPES[a]) for a in ACTIONS},
        "repeats":list(REPEATS),"total_per_task_repeat_s":TOTAL_SECONDS,
        "max_per_action_s":PER_ACTION_SECONDS,"max_verification_s":5.0,"no_retry":True,
        "runner":"unchanged mdai.w3runner.run_match","runner_sha256":pin(ROOT/"mdai"/"w3runner.py"),
        "label_rule":"raw exact-proof receipts retain equivalent no-ops even if the runner structurally deduplicates them",
        "end_to_end_live_model":False,"heldout_accessed":False,
    })
    index=[]
    statuses=Counter()
    n=0
    with (DEST/"abc_action_observations.jsonl").open("x", encoding="utf-8") as out:
        for item_index,item in enumerate(chosen):
            task_folder=DEST/f"case-{item_index:02d}"
            task_folder.mkdir()
            seedfile=task_folder/"seed.aag"
            seedfile.write_text(seed_graph(item).to_aag(symbols=True),encoding="ascii")
            seed_sha=pin(seedfile)
            for repeat in REPEATS:
                repeat_start=monotonic()
                base=AIG.from_aag(seedfile.read_text(encoding="ascii"))
                base_m=base.metrics()
                from_disk_sha=pin(seedfile)
                if from_disk_sha != seed_sha:
                    raise RuntimeError("frozen common seed mutated")
                match_dir=task_folder/f"repeat-{repeat}"
                match=run_match(base,task_id=item["task_id"],arm="abc",repeat=repeat,
                                seed=1000+repeat,abc_path=abc,timeout_s=TOTAL_SECONDS,
                                candidate_limit=20,verify=verify_z3,evidence_dir=match_dir,
                                started_monotonic=repeat_start)
                attempts=match.get("attempts",[])
                if any(a.get("action") != wanted for a,wanted in zip(attempts,ACTIONS)):
                    raise RuntimeError("ABC runner action order drift")
                for action_position,action in enumerate(ACTIONS):
                    trial=match_dir/f"abc-{action_position+1:02d}"
                    rawfile=trial/"receipt.json"
                    attempt=attempts[action_position] if action_position<len(attempts) else {}
                    runner_status=attempt.get("status","NOT_RUN")
                    raw=json.loads(bounded(rawfile)) if rawfile.is_file() else {}
                    metrics=raw.get("output_metrics")
                    valid = bool(runner_status in {"VERIFIED","DUPLICATE","DEPTH_CAP"}
                                 and raw.get("status") == "VERIFIED"
                                 and raw.get("equivalent") is True
                                 and raw.get("recipe") == action
                                 and raw.get("input_metrics") == vars(base_m)
                                 and isinstance(metrics,dict)
                                 and type(metrics.get("and_count")) is int and metrics["and_count"] >= 0
                                 and type(metrics.get("depth")) is int and metrics["depth"] >= 0
                                 and (trial/"input.aig").is_file() and (trial/"output.aig").is_file())
                    status="VERIFIED" if valid else runner_status if runner_status not in {"VERIFIED","DUPLICATE","DEPTH_CAP"} else "PROOF_INCONSISTENT"
                    labels={"equivalent":valid,
                            "depth_ok":valid and metrics["depth"] <= base_m.depth,
                            "and_reduction":base_m.and_count-metrics["and_count"] if valid else None}
                    row={"task_id":item["task_id"],"group_id":item["canonical_id"],"family":item["family"],
                         "repeat":repeat,"partition":"development","action":action,
                         "features":{"input_bits":len(base.input_literals),"output_bits":len(base.outputs),
                                     "source_and_count":base_m.and_count,"source_depth":base_m.depth},
                         "outcome":labels}
                    line=canon(row)
                    out.write(line+"\n")
                    out.flush()
                    rec={
                        "row_index":n,"row_sha256":sha(line.encode()),"task_id":item["task_id"],"repeat":repeat,
                        "action":action,"status":status,"runner_attempt_status":runner_status,
                        "seed_sha256":seed_sha,"seed_path":str(seedfile.relative_to(DEST)),
                        "receipt_path":str(rawfile.relative_to(DEST)) if rawfile.is_file() else None,
                        "receipt_sha256":pin(rawfile) if rawfile.is_file() else None,
                        "runner_receipt_path":str((match_dir/"receipt.json").relative_to(DEST)),
                        "runner_receipt_sha256":pin(match_dir/"receipt.json"),
                        "input_sha256":pin(trial/"input.aig") if (trial/"input.aig").exists() else None,
                        "output_sha256":pin(trial/"output.aig") if (trial/"output.aig").exists() else None,
                    }
                    index.append(rec)
                    statuses[status]+=1
                    n+=1
    put(DEST/"receipt_index.json",{"schema":"w4-expansion-index-v1","rows":index})
    counts={"expected":len(chosen)*len(ACTIONS)*len(REPEATS),"observed":n}
    terminal="VERIFIED_DEVELOPMENT_ONLY" if statuses=={"VERIFIED":counts["expected"]} else "PARTIAL_FAIL_CLOSED"
    summary={
        "terminal_status":terminal,"cohort_sha256":FROZEN_SHA,
        "source_manifest_sha256":spec["source_manifest_sha256"],
        "counts":counts,"statuses":dict(statuses),
        "rows_sha256":pin(DEST/"abc_action_observations.jsonl"),
        "receipt_index_sha256":pin(DEST/"receipt_index.json"),
        "preregistered_sha256":pin(DEST/"preregistered.json"),
        "separate_from_week3":True,
        "final_holdout":False,
        "complete_action_labels":terminal=="VERIFIED_DEVELOPMENT_ONLY",
    }
    put(DEST/"summary.json",summary)
    print(canon(summary))
    return terminal=="VERIFIED_DEVELOPMENT_ONLY"


def main():
    ap=argparse.ArgumentParser()
    ap.add_argument("--source",type=Path,required=True)
    ap.add_argument("--abc",type=Path)
    ap.add_argument("--abc-sha256")
    ap.add_argument("--check-only",action="store_true")
    args=ap.parse_args()
    if args.check_only:
        _, rows=read_cohort(args.source)
        print(canon({"rows":len(rows),"cohort_sha256":FROZEN_SHA,"executed":False}))
        return
    if args.abc is None or args.abc_sha256 is None:
        ap.error("requires pinned executable and SHA-256")
    raise SystemExit(0 if run(args.source,args.abc,args.abc_sha256) else 1)


if __name__=="__main__":
    main()
