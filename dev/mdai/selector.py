"""Small offline action ranker. A recommendation is never a verification verdict.

Only A/B development-labelled rows enter this module. The caller must prove that
the frozen eligible input data are authorized before fitting; synthetic fixtures
are the only training data used by this module's tests.
"""
from collections import defaultdict
from dataclasses import dataclass
from hashlib import sha256
import math
import re

_VERSION = "w4-small-selector-v1"
_FEATURES = ("input_bits", "output_bits", "source_and_count", "source_depth")
_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z")
_LIMITS = ((1, 16), (1, 8), (0, 100000), (0, 100000))


def _identifier(value, label):
    if type(value) is not str or not _ID.fullmatch(value):
        raise ValueError(f"invalid {label}")
    return value


def _features(family, values):
    if family not in ("A", "B") or type(values) is not dict or set(values) != set(_FEATURES):
        raise ValueError("pre-action feature whitelist violated")
    numbers = tuple(values[k] for k in _FEATURES)
    if any(type(x) is not int or not lo <= x <= hi for x, (lo, hi) in zip(numbers, _LIMITS)):
        raise ValueError("invalid pre-action feature")
    if numbers[3] > numbers[2]:
        raise ValueError("source depth exceeds reachable AND count")
    return (*numbers, int(family == "B"))


def split_for_group(group_id, *, salt=_VERSION):
    """Outcome-blind deterministic 25% dev split; never alter salt after results."""
    _identifier(group_id, "group ID")
    _identifier(salt, "split salt")
    digest = sha256(f"{salt}:{group_id}".encode()).digest()
    return "dev" if digest[0] < 64 else "train"


def _action_set(actions):
    if isinstance(actions, (str, bytes)):
        raise ValueError("need an action menu, not a string")
    actions = tuple(_identifier(a, "action") for a in actions)
    if not 2 <= len(actions) <= 20 or len(set(actions)) != len(actions):
        raise ValueError("need 2..20 distinct fixed actions")
    return tuple(sorted(actions))


@dataclass(frozen=True)
class Observation:
    task_id: str
    group_id: str
    family: str
    split: str
    features: tuple[int, ...]
    action: str
    repeat: int
    reward: int
    accepted: bool


def _observations(rows, actions, salt):
    allowed = {"task_id", "group_id", "family", "features", "action", "outcome",
               "repeat", "partition"}
    out, seen, task_meta = [], set(), {}
    for row in rows:
        if type(row) is not dict or set(row) != allowed or row["partition"] != "development":
            raise ValueError("only complete explicitly development-labelled A/B rows accepted")
        task = _identifier(row["task_id"], "task ID")
        group = _identifier(row["group_id"], "group ID")
        family = row["family"]
        features = _features(family, row["features"])
        old = task_meta.setdefault(task, (group, family, features))
        if old != (group, family, features):
            raise ValueError("task provenance or pre-action features drifted")
        if row["action"] not in actions or type(row["repeat"]) is not int or not 0 <= row["repeat"] <= 100:
            raise ValueError("undeclared action or invalid repeat")
        key = (task, row["action"], row["repeat"])
        if key in seen:
            raise ValueError("duplicate task/action/repeat record")
        seen.add(key)
        outcome = row["outcome"]
        if type(outcome) is not dict or set(outcome) != {"and_reduction", "depth_ok", "equivalent"}:
            raise ValueError("outcome schema mismatch")
        reduction, depth, equal = outcome["and_reduction"], outcome["depth_ok"], outcome["equivalent"]
        if type(depth) is not bool or type(equal) is not bool or (reduction is not None and
                (type(reduction) is not int or not -100000 <= reduction <= features[2])):
            raise ValueError("invalid result label")
        accepted = depth and equal
        if accepted and reduction is None:
            raise ValueError("verified eligible row missing cost observation")
        reward = max(0, reduction) if accepted else 0
        out.append(Observation(task, group, family, split_for_group(group, salt=salt), features, row["action"], row["repeat"], reward, accepted))
    return sorted(out, key=lambda r: (r.split, r.group_id, r.task_id, r.action, r.repeat))


def _quality(parsed, actions, min_train, min_dev):
    if any(type(x) is not int or x < 2 for x in (min_train, min_dev)):
        raise ValueError("minimum independent groups must be >=2 per split")
    groups = {s: {r.group_id for r in parsed if r.split == s} for s in ("train", "dev")}
    assert not groups["train"] & groups["dev"]
    pools = defaultdict(set)
    for r in parsed:
        pools[(r.task_id, r.repeat)].add(r.action)
    reasons = []
    if len(groups["train"]) < min_train:
        reasons.append("too_few_train_groups")
    if len(groups["dev"]) < min_dev:
        reasons.append("too_few_dev_groups")
    if any(c != set(actions) for c in pools.values()):
        reasons.append("incomplete_logged_action_menu")
    coverage = {s: {a: len({r.group_id for r in parsed if r.split == s and r.action == a and r.accepted})
                    for a in actions} for s in ("train", "dev")}
    if any(n < 2 for n in coverage["train"].values()):
        reasons.append("too_few_verified_train_groups_per_action")
    if not any(r.split == "train" and r.reward > 0 for r in parsed):
        reasons.append("no_positive_train_signal")
    return {"ready": not reasons, "reasons": reasons, "rows": len(parsed),
            "independent_groups": {s: len(gs) for s, gs in groups.items()},
            "verified_groups_per_action": coverage, "timing_readiness": "not_assessed",
            "fit_requires_external_gate_authorization": True}


def check_adequacy(rows, actions, *, salt=_VERSION, min_train=12, min_dev=6):
    """Validate full logged menus and group support without fitting or ranking."""
    acts = _action_set(actions)
    parsed = _observations(rows, acts, salt)
    return _quality(parsed, acts, min_train, min_dev)


def _solve(a, b):
    r = [list(row) + [v] for row, v in zip(a, b)]
    n = len(b)
    for k in range(n):
        pivot = max(range(k, n), key=lambda j: abs(r[j][k]))
        r[k], r[pivot] = r[pivot], r[k]
        value = r[k][k]
        if abs(value) < 1e-15:
            raise ValueError("singular selector fit")
        r[k] = [v / value for v in r[k]]
        for j in range(n):
            if j != k:
                coeff = r[j][k]
                r[j] = [a - coeff * b for a, b in zip(r[j], r[k])]
    return tuple(row[-1] for row in r)


def _normalized(x, means, spread):
    return (1.0, *((v - a) / s for v, a, s in zip(x, means, spread)))


@dataclass(frozen=True)
class Selector:
    actions: tuple[str, ...]
    means: tuple[float, ...]
    spread: tuple[float, ...]
    weights: tuple[tuple[float, ...], ...]
    best_static: str
    split_salt: str

    def rank(self, family, features):
        """Rank the configured menu from original input/source data only."""
        x = _normalized(_features(family, features), self.means, self.spread)
        result = [(a, sum(v * w for v, w in zip(x, ws))) for a, ws in zip(self.actions, self.weights)]
        return sorted(result, key=lambda pair: (-pair[1], pair[0]))


def _mean(values):
    return sum(values) / len(values)


def _mean_group(entries):
    groups = defaultdict(list)
    for group, score in entries:
        groups[group].append(score)
    return _mean([_mean(vals) for vals in groups.values()])


def fit_selector(rows, actions, *, allow_fit=False, salt=_VERSION, min_train=12, min_dev=6, ridge=1.0):
    """Train only on outcome-blind train groups; report development replay.

    Label is conservative nonnegative AND reduction versus original source:
    ineligible or non-equivalent fallback is zero. This assumes the original
    circuit remains available, and says nothing about wall-clock efficiency.
    """
    if allow_fit is not True:
        raise PermissionError("selector fitting requires explicit gate authorization")
    if type(ridge) not in (int, float) or not math.isfinite(ridge) or not 1e-6 <= ridge <= 1e6:
        raise ValueError("ridge must be finite and positive")
    actions = _action_set(actions)
    rows = _observations(rows, actions, salt)
    report = _quality(rows, actions, min_train, min_dev)
    if not report["ready"]:
        raise ValueError("adequacy gate: " + ",".join(report["reasons"]))
    train = [r for r in rows if r.split == "train"]
    features = {(r.group_id, r.task_id): r.features for r in train}
    n = len(next(iter(features.values())))
    means = tuple(_mean_group((k[0], v[i]) for k, v in features.items()) for i in range(n))
    spread = tuple(max(1.0, math.sqrt(_mean_group((k[0], (v[i] - means[i]) ** 2)
                                                 for k, v in features.items()))) for i in range(n))
    coeffs, baselines = [], []
    for action in actions:
        obs = [r for r in train if r.action == action]
        sizes = defaultdict(int)
        for r in obs:
            sizes[r.group_id] += 1
        m = n + 1
        mat = [[0.0] * m for _ in range(m)]
        vec = [0.0] * m
        for r in obs:
            x = _normalized(r.features, means, spread)
            weight = 1.0 / sizes[r.group_id]
            for i in range(m):
                vec[i] += weight * r.reward * x[i]
                for j in range(m):
                    mat[i][j] += weight * x[i] * x[j]
        for i in range(1, m):
            mat[i][i] += ridge
        coeffs.append(_solve(mat, vec))
        baselines.append((action, _mean_group((r.group_id, r.reward) for r in obs)))
    static = min(baselines, key=lambda pair: (-pair[1], pair[0]))[0]
    model = Selector(actions, means, spread, tuple(coeffs), static, salt)
    dev_trials = defaultdict(dict)
    for r in rows:
        if r.split == "dev":
            dev_trials[(r.group_id, r.task_id, r.repeat, r.family, r.features)][r.action] = r.reward
    ranked, constant = [], []
    for (group, _, _, family, x), rewards in dev_trials.items():
        first = model.rank(family, dict(zip(_FEATURES, x[:len(_FEATURES)])))[0][0]
        ranked.append((group, rewards[first]))
        constant.append((group, rewards[static]))
    report.update({"ranked_dev_mean_AND_saved": _mean_group(ranked),
                   "constant_dev_mean_AND_saved": _mean_group(constant),
                   "static_action_selected_on_train": static,
                   "evidence_kind": "logged_full_menu_development_only_not_holdout",
                   "claims_measured_runtime_or_generalization": False})
    return model, report
