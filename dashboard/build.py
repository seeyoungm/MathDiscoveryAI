"""Build a single portable HTML file from local development evidence only."""
from collections import defaultdict
from pathlib import Path
from hashlib import sha256
import json
import sys

DASH = Path(__file__).resolve().parent
ROOT = DASH.parent
sys.path.insert(0, str(ROOT / "dev"))
from mdai.selector import split_for_group
from mdai.anytime import replay

E = ROOT / "dev/evidence"
W3 = E / "week3/matched_dev_controls"
EXP = E / "week4_validation_expansion"
READINESS = E / "week4_rankpilot_readiness"
MENU = ("strash_only", "balance_rewrite", "rewrite_refactor")
MILESTONE_MENU_OLD = (*MENU, "resub_balance")
LATEST_RECEIPT = Path("milestone_review/dashboard_receipt.json")
SUBPROCESS_RECEIPT = Path("milestone_subprocess_review/dashboard_receipt.json")
SUBPROCESS_SUMMARY = Path("pilot_local_transport/summary.json")


def read(p):
    return json.loads(p.read_text())


def w4_data():
    groups = defaultdict(dict)
    for src, tag, pat in ((W3, "기존", "abc-*"), (EXP, "추가", "repeat-*")):
        tags = {}
        for line in (src / "abc_action_observations.jsonl").read_text().splitlines():
            row = json.loads(line)
            tags[(row["task_id"], row["action"], row["repeat"])] = row
        for p in sorted(src.glob(f"case-*/{pat}/receipt.json")):
            data = read(p)
            key = data["key"]
            for action in data["attempts"]:
                aid = action["action"]
                row = tags.pop((key["task_id"],aid,key["repeat"]))
                ands, depth = action["candidate_and_count"], action["candidate_depth"]
                yes = row["outcome"]
                if yes["equivalent"] is not True or yes["and_reduction"] != row["features"]["source_and_count"]-ands:
                    raise ValueError("recorded label does not match the trial")
                task = groups[row["task_id"]]
                task.update(id=row["task_id"], family=row["family"], group=row["group_id"],
                            split=split_for_group(row["group_id"]), source=tag,
                            original=row["features"]["source_and_count"], inputDepth=row["features"]["source_depth"])
                vals = (ands, depth, action["candidate_fingerprint"], yes["depth_ok"])
                prev = task.setdefault("trials", {}).setdefault(aid, [])
                prev.append(vals)
        if tags:
            raise ValueError("unmatched recorded observations")
    for t in groups.values():
        all_actions = t.pop("trials")
        if set(all_actions) != set(MENU) or any(len(rows)!=3 or len(set(rows)) != 1 for rows in all_actions.values()):
            raise ValueError("recipe/repeat mismatch")
        t["actions"] = {a:all_actions[a][0] for a in MENU}
    summary=read(READINESS/"report.json")
    heads=read(READINESS/"headroom.json")
    return dict(groups=list(groups.values()), readiness=summary, headroom=heads, menu=MENU)


def _checked_json(path, claimed):
    raw = path.read_bytes()
    actual = sha256(raw).hexdigest()
    if type(claimed) is not str or actual != claimed:
        raise ValueError(f"independent digest mismatch: {path.name}")
    return json.loads(raw)


def _assert_equal(got, expected, where):
    if type(got) is not type(expected) or got != expected:
        raise ValueError(f"inconsistent {where}")


def _normal_count(value):
    if type(value) is not int or value < 0:
        raise ValueError("invalid nonnegative count")
    return value


def _display_synthesis(directory, review, receipt_hash):
    summary = _checked_json(directory/"summary.json", review.get("summary_sha256"))
    raw = _checked_json(directory/"group_metrics.json", review.get("group_metrics_sha256"))
    _assert_equal(summary.get("group_metrics_sha256"), review["group_metrics_sha256"], "group metrics cross-pin")
    for key, expected in (("state","VERIFIED_DEVELOPMENT_ONLY"),("model_calls",0),
                          ("ranker_fit",False),("holdout_accessed",False),("runtime_claim",False)):
        _assert_equal(summary.get(key),expected,key)
    groups = raw["groups"]
    if not groups or len({g["group_id"] for g in groups}) != len(groups):
        raise ValueError("duplicate or empty milestone group list")
    menu = summary["menu"]
    if len(menu) != len(set(menu)) or set(menu) <= set(MILESTONE_MENU_OLD) or not set(MILESTONE_MENU_OLD) <= set(menu):
        raise ValueError("complementary menu must extend historic menu")
    mode_counts = defaultdict(int)
    family_eval = defaultdict(lambda: dict(groups=0,oldOracle=0,newOracle=0,better=0))
    for g in groups:
        if g["family"] not in ("A","B") or g["partition"] not in ("train","eval"):
            raise ValueError("invalid milestone development scope")
        actions = g["actions"]
        _assert_equal(set(actions),set(menu),"milestone action set")
        depth_ceiling = _normal_count(g["source"]["depth"])
        initial = _normal_count(g["source"]["and_count"])
        for d in actions.values():
            _assert_equal(d.get("status"),"VERIFIED","proof status")
            _assert_equal(d.get("repeat_stable"),True,"repeat stability")
            words = d["trials"]
            if not words:
                raise ValueError("missing milestone trials")
            mode_counts["trials"] += len(words)
            ands = _normal_count(words[0]["output_metrics"]["and_count"])
            depth = _normal_count(words[0]["output_metrics"]["depth"])
            if any(w["output_metrics"] != words[0]["output_metrics"] for w in words):
                raise ValueError("inconsistent repeated metrics")
            _assert_equal(d.get("depth_ok"),depth <= depth_ceiling,"depth admissibility")
            if depth <= depth_ceiling:
                mode_counts["depth_ok"] += len(words)
            else:
                mode_counts[g["partition"]+"_depth_excluded"] += len(words)
            _assert_equal(d.get("strict_gain"),max(0,initial - ands) if depth <= depth_ceiling else 0,"strict gain")
        if g["partition"] == "eval":
            old = max(actions[a]["strict_gain"] for a in MILESTONE_MENU_OLD)
            new = max(d["strict_gain"] for d in actions.values())
            stats = family_eval[g["family"]]
            stats["groups"] += 1
            stats["oldOracle"] += old
            stats["newOracle"] += new
            stats["better"] += old < new
    if set(family_eval) != {"A","B"}:
        raise ValueError("missing milestone evaluation family")
    families = [dict(family=family, **family_eval[family]) for family in ("A","B")]
    totals = {k:sum(f[k] for f in families) for k in ("groups","oldOracle","newOracle","better")}
    count_total = summary["counts"]
    _assert_equal(count_total.get("groups"),len(groups),"group count")
    _assert_equal(count_total.get("trials"),mode_counts["trials"],"trial count")
    _assert_equal(count_total.get("statuses"),{"VERIFIED":mode_counts["trials"]},"trial statuses")
    for projection, summary_key in (("groups","groups"),("oldOracle","prior_menu_oracle_gain"),
                                    ("newOracle","oracle_gain"),("better","groups_new_oracle_beats_old")):
        _assert_equal(summary["eval_totals"].get(summary_key),totals[projection],summary_key)
        for f in families:
            _assert_equal(summary["eval_by_family"][f["family"]].get(summary_key),f[projection],
                          f"{f['family']} {summary_key}")
    added = [a for a in menu if a not in MILESTONE_MENU_OLD]
    stop = summary.get("stop_decision") == "STOP_MENU_EXPANSION_NO_PREDEFINED_SIGNAL"
    return dict(**totals, trials=mode_counts["trials"], families=families, added=added,
                depthOK=mode_counts["depth_ok"],trainDepthExcluded=mode_counts["train_depth_excluded"],
                devDepthExcluded=mode_counts["eval_depth_excluded"],stop=stop,receiptHash=receipt_hash)


def _display_rankpilot(directory, review, receipt_hash):
    if review.get("summary_path","pilot_contract_repair/summary.json") != "pilot_contract_repair/summary.json":
        raise ValueError("unexpected pilot summary binding")
    summary = _checked_json(directory/"summary.json", review.get("summary_sha256"))
    for key, expected in (
        ("schema","mdai-ab-ordered-pilot-repair-result-v1"),
        ("state","OFFLINE_CONTRACT_VERIFIED"),("formal_gate3_pass",False),
        ("holdout_accessed",False),("ranker_fit",False),("model_calls",0),
        ("actual_cost_or_latency_claimed",False),("real_live_model_comparison",False),
        ("historical_b0_b1_action","neutral_double_not"),
    ):
        _assert_equal(summary.get(key),expected,key)
    fixtures = ("A0","A1","B0","B1")
    _assert_equal(summary.get("historical_bound_fixtures"),list(fixtures),"pilot fixtures")
    expected_keys = {(key,repeat,arm) for key in fixtures for repeat in range(3)
                     for arm in ("no-model","offline-synthetic")}
    status_counts = defaultdict(int)
    trials = summary["trials"]
    if len(trials) != len(expected_keys):
        raise ValueError("incompatible repair trial count")
    arms = defaultdict(int)
    for item in trials:
        row = (item["key"],item["repeat"],item["arm"])
        if row not in expected_keys:
            raise ValueError("unexpected or repeated pilot trial")
        expected_keys.remove(row)
        arms[item["arm"]] += 1
        labels = item["verified_statuses"]
        if not labels or not set(labels) <= {"VERIFIED","DUPLICATE"}:
            raise ValueError("unreviewed pilot outcomes")
        for label,count in labels.items():
            status_counts[label] += _normal_count(count)
        _assert_equal(item.get("attempts"),sum(labels.values()),"per-trial attempt count")
    _assert_equal(summary.get("fixtures"),len(fixtures),"pilot fixture count")
    _assert_equal(summary.get("no_model_runs"),arms["no-model"],"no-model arm count")
    _assert_equal(summary.get("offline_synthetic_runs"),arms["offline-synthetic"],"offline arm count")
    _assert_equal(summary.get("attempt_statuses"),dict(status_counts),"pilot outcome counts")
    rows = [
        ["개발 예제",len(fixtures)],
        ["미리 정한 오프라인 응답",arms["offline-synthetic"]],
        ["모델 없이 실행",arms["no-model"]],
        ["시도 합계",sum(status_counts.values())],
        ["동치 확인 / 중복",f"{status_counts['VERIFIED']} / {status_counts['DUPLICATE']}"],
        ["과거 B 예제의 인덱스 해석","원래 메뉴의 이중 부정 (neutral_double_not)"],
        ["라이브 모델 호출",0],
    ]
    return dict(rows=rows, receiptHash=receipt_hash)


def _display_local_contract(evidence, review, receipt_hash):
    _assert_equal(review.get("summary_path"),str(SUBPROCESS_SUMMARY),"local summary binding")
    summary = _checked_json(evidence/SUBPROCESS_SUMMARY, review.get("summary_sha256"))
    for key, expected in (
        ("schema","mdai-pilot-local-transport-v1"),
        ("state","LOCAL_SUBPROCESS_CONTRACT_VERIFIED"),
        ("model_calls",0),
        ("formal_gate3_pass",False),
    ):
        _assert_equal(summary.get(key),expected,key)
    expected_cases = {
        "canonical":"COMMITTED_LOCAL_FIXTURE",
        "sleeping":"DEADLINE_EXCEEDED",
        "malformed":"INVALID_RESPONSE",
        "oversized_response":"RESPONSE_TOO_LARGE",
        "oversized_request":"REQUEST_TOO_LARGE",
    }
    cases = summary["cases"]
    _assert_equal(set(cases),set(expected_cases),"local case set")
    for name, status in expected_cases.items():
        row = cases[name]
        spawned = name != "oversized_request"
        _assert_equal(row.get("status"),status,name+" status")
        _assert_equal(row.get("committed"),name=="canonical",name+" commit")
        _assert_equal(row.get("spawned"),spawned,name+" launch")
        if spawned:
            _assert_equal(row.get("reaped"),True,name+" reap")
    _assert_equal(cases["sleeping"].get("killed"),True,"deadline kill")
    rows = [
        ["로컬 검사 사례",len(cases)],
        ["정상 응답만 채택",sum(row["committed"] for row in cases.values())],
        ["시간 초과", "자식 프로세스 중단·회수 확인"],
        ["잘못된 응답·크기 초과", "채택 0건"],
        ["라이브 모델 호출",0],
    ]
    return dict(rows=rows, receiptHash=receipt_hash)


def _independent_dashboard_receipt(path):
    raw = path.read_bytes()
    data = json.loads(raw)
    _assert_equal(data.get("schema"),1,"dashboard receipt schema")
    return data, sha256(raw).hexdigest()


def milestone_data(evidence):
    latest = {}
    path = evidence/LATEST_RECEIPT
    if path.exists():
        independent,receipt_hash = _independent_dashboard_receipt(path)
        for branch, subdir, project in (("synthesis","milestone_synthesis",_display_synthesis),
                                        ("rankpilot","pilot_contract_repair",_display_rankpilot)):
            review = independent.get(branch)
            if review is None or review.get("status") != "PASS":
                continue
            latest[branch] = project(evidence/subdir,review,receipt_hash)
    path = evidence/SUBPROCESS_RECEIPT
    if path.exists():
        independent,receipt_hash = _independent_dashboard_receipt(path)
        review = independent.get("subprocess_contract")
        if review is not None and review.get("status") == "PASS":
            latest["subprocess_contract"] = _display_local_contract(evidence,review,receipt_hash)
    return latest or None

def anytime_data():
    folder=E/"anytime"
    path=folder/"audit.json"
    if not path.exists():
        return None
    audit=read(path)
    _assert_equal(audit["schema"],"mdai-anytime-reproof-v1","timing audit schema")
    _assert_equal(audit["status"],"PASS","timing audit verdict")
    raw=_checked_json(folder/"trace.json",audit["trace_sha256"])
    result=replay(raw)
    n=sum(e["smt"] is True for c in result["cases"] for r in c["runs"] for e in r["events"])
    _assert_equal(audit["reproved"],n,"timing audit proof count")
    _assert_equal(audit["unique_reproved"],len({e["fingerprint"] for c in result["cases"] for r in c["runs"] for e in r["events"] if e["smt"] is True}),"timing audit distinct graph count")
    for f,want in raw["pins"].items():
        if f not in ("docs/ANYTIME_PROTOCOL.md","dev/mdai/anytime.py","dev/mdai/candidates.py","dev/mdai/aig.py",
                      "dev/mdai/logic.py","dev/mdai/w3verify.py","dev/tools/z3_lib_cli.py"):
            raise ValueError("unexpected timing pin path")
        _assert_equal(sha256((ROOT/f).read_bytes()).hexdigest(),want,"timing protocol input")
    _assert_equal(len(raw["pins"]),7,"timing pin count")
    return dict(**result, traceHash=audit["trace_sha256"])


def main():
    data=dict(w4=w4_data(), next=None, latest=milestone_data(E), anytime=anytime_data())
    ns = E / "nextstage_synthesis"
    if (ns/"summary.json").exists():
        summary = read(ns/"summary.json")
        data["next"] = {"summary":summary}
        if summary.get("state") == "VERIFIED_DEVELOPMENT_ONLY":
            raw = (ns/"group_metrics.json").read_bytes()
            if sha256(raw).hexdigest() != summary["group_metrics_sha256"]:
                raise ValueError("next-stage summary does not match recorded group metrics")
            groups = json.loads(raw)["groups"]
            selected = [g for g in groups if g["partition"] == "eval"]
            menu = summary["menu"]
            for g in groups:
                if set(g["actions"]) != set(menu):
                    raise ValueError("next-stage group action menu mismatch")
                for a in menu:
                    d = g["actions"][a]
                    if d["status"] != "VERIFIED" or d["repeat_stable"] is not True:
                        raise ValueError("incomplete next-stage proof label")
            expected = dict(groups=len(selected),
                            prior_fixed_gain=sum(g["actions"][summary["prior_fixed_action"]]["strict_gain"] for g in selected),
                            train_selected_gain=sum(g["actions"][summary["train_selected_action"]]["strict_gain"] for g in selected),
                            oracle_gain=sum(max(d["strict_gain"] for d in g["actions"].values()) for g in selected))
            if any(summary["eval_totals"][k] != v for k,v in expected.items()):
                raise ValueError("inconsistent next-stage totals")
            data["next"]["groups"] = [dict(group_id=g["group_id"], family=g["family"], partition=g["partition"],
                source=g["source"], actions={a:dict(gain=d["strict_gain"],depthOK=d["depth_ok"],
                  ands=d["trials"][0]["output_metrics"]["and_count"],depth=d["trials"][0]["output_metrics"]["depth"])
                  for a,d in g["actions"].items()}) for g in selected]
    literal = json.dumps(data, ensure_ascii=False, separators=(",", ":")).replace("<", "\\u003c")
    tpl = (DASH / "template.html").read_text()
    if tpl.count("__EMBEDDED_DATA__")!=1:
        raise ValueError("template must have exactly one data slot")
    syn=(data["latest"] or {}).get("synthesis")
    summary="독립 검토된 절감 기록이 아직 없습니다."
    if syn:
        summary=(f"내부 개발 {syn['groups']}개에서 AND 절감 합계 {syn['oldOracle']} → {syn['newOracle']}, "
                 f"추가 개선 {syn['better']}/{syn['groups']}개. "
                 f"동치 {syn['trials']}/{syn['trials']}회, 깊이 제한 {syn['depthOK']}/{syn['trials']}회. "
                 f"깊이 제외 {syn['trainDepthExcluded']}회는 학습 사례에만 해당합니다."
                 if syn["devDepthExcluded"]==0 else "깊이 제외 내역은 실험 상세를 참조하세요.")
    time=data["anytime"]
    rehearsal="검토된 시간 기록 없음."
    if time:
        runs=[r for c in time["cases"] for r in c["runs"]]
        hits=[sum(r["target_ns"] is not None for r in runs if r["arm"]==arm) for arm in ("canonical","structure")]
        trials=len(runs)//2
        rehearsal=f"합성 회로 {len(time['cases'])}개로 기록 방식을 검증했습니다. 목표 도달: 기본 순서 {hits[0]}/{trials}회, 구조 우선 {hits[1]}/{trials}회. 라이브 AI·ABC 비교 아님."
    out = tpl.replace("__EMBEDDED_DATA__", literal).replace("__SYNTHESIS_STATIC__",summary).replace("__ANYTIME_STATIC__",rehearsal)
    (DASH / "index.html").write_text(out)
    print(f"index.html: {len(out.encode())} bytes; {len(data['w4']['groups'])} W4 groups; next={bool(data['next'])}; milestone={bool(data['latest'])}")


if __name__=="__main__":
    main()
