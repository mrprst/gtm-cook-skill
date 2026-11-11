#!/usr/bin/env python3
"""Deterministic signal scoring + SDR routing for GTM Cook.

Usage:
  python score.py --accounts accounts.csv --signals signals.csv --policy policy_vN.json \
                  --out out_dir --as-of YYYY-MM-DD [--assignments assignments.csv [--commit]]

Same inputs + same policy + same as-of date => same output. Stdlib only.

--assignments: assignment memory (account_id, assigned_to, assigned_on, policy_version, outcome).
  Read for the cooldown. Written ONLY with --commit (dry run otherwise): the as-of day's batch is
  replaced, and a same-day row that already has an outcome is never deleted.
"""
import argparse, json, os, sys
from collections import defaultdict, Counter
from datetime import date, timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import (ACCOUNT_REQUIRED, SIGNAL_REQUIRED, ASSIGN_COLS, die, parse_date, as_bool, as_num,
                    read_csv, write_csv, load_policy, scoring_hash)


# ---------- loading ----------
def load_accounts(path):
    _, rows = read_csv(path, ACCOUNT_REQUIRED, "accounts file")
    accounts = {}
    for r in rows:
        if not r["account_id"]:
            continue
        if r["account_id"] in accounts:
            die(f"duplicate account_id in accounts file: {r['account_id']}")
        accounts[r["account_id"]] = r
    return accounts


def load_signals(path, known):
    _, rows = read_csv(path, SIGNAL_REQUIRED, "signals file")
    signals, bad_dates, orphans = defaultdict(list), 0, 0
    for r in rows:
        if not r["account_id"] or not r["signal_type"]:
            continue
        if r["account_id"] not in known:
            orphans += 1
            continue
        d = parse_date(r["signal_date"])
        if d is None:
            bad_dates += 1
            continue
        signals[r["account_id"]].append({"type": r["signal_type"], "source": r["signal_source"] or "unknown",
                                         "date": d, "value": as_num(r.get("signal_value"))})
    return signals, bad_dates, orphans


def load_assignments(path):
    if not path or not os.path.exists(path):
        return []
    _, rows = read_csv(path, ASSIGN_COLS, "assignments file")
    return [{c: r.get(c, "") for c in ASSIGN_COLS} for r in rows]


# ---------- filters ----------
def exclusion_reason(a, ex):
    if ex.get("exclude_customers", True) and as_bool(a.get("is_customer")):
        return "customer"
    if ex.get("exclude_blocked", True) and as_bool(a.get("is_blocked")):
        return "blocked"
    if ex.get("require_domain", True) and not a.get("domain"):
        return "no_domain"
    return None


def icp_reason(a, icp):
    if not icp:
        return None
    lo, hi = icp.get("employees_min"), icp.get("employees_max")
    if lo is not None or hi is not None:
        emp = as_num(a.get("employees"))
        if emp is None:
            return "icp_unknown_size"
        if lo is not None and emp < lo:
            return "icp_too_small"
        if hi is not None and emp > hi:
            return "icp_too_large"
    for field, key in (("industries", "industry"), ("countries", "country")):
        allowed = icp.get(field)
        if allowed and (a.get(key) or "").lower() not in {x.lower() for x in allowed}:
            return f"icp_{key}"
    return None


# ---------- scoring ----------
def newer(s, cur):
    """Latest date wins; same date: first source in byte order; same source: higher value."""
    if cur is None or s["date"] > cur["date"]:
        return True
    if s["date"] < cur["date"]:
        return False
    if s["source"] != cur["source"]:
        return s["source"] < cur["source"]
    sv, cv = s["value"], cur["value"]
    return sv is not None and (cv is None or sv > cv)


def scale_positive(contribs, key, caps, default_cap):
    """Caps apply to positive points only: they limit how much good news one source/family can add."""
    groups = defaultdict(float)
    for c in contribs:
        if c["points"] > 0:
            groups[c[key]] += c["points"]
    for g, total in groups.items():
        cap = caps.get(g, default_cap)
        if cap is not None and total > cap:
            f = cap / total
            for c in contribs:
                if c[key] == g and c["points"] > 0:
                    c["points"] *= f


def score_account(sigs, policy, as_of, skip_source=None):
    conf = policy["signals"]
    latest, missing_value = {}, 0
    for s in sigs:
        if s["type"] not in conf or s["date"] > as_of or s["source"] == skip_source:
            continue
        if newer(s, latest.get(s["type"])):
            latest[s["type"]] = s
    contribs = []
    for t, s in latest.items():
        c = conf[t]
        age = (as_of - s["date"]).days
        if c.get("max_age_days") is not None and age > c["max_age_days"]:
            continue
        hl = c.get("half_life_days")
        decay = 0.5 ** (age / hl) if hl else 1.0
        scale = 1.0
        vs = c.get("value_scale")
        if vs is not None:
            if s["value"] is None:
                missing_value += 1
                continue
            lo, hi = float(vs["min"]), float(vs["max"])
            scale = min(1.0, max(0.0, (s["value"] - lo) / (hi - lo)))
        contribs.append({"type": t, "source": s["source"], "family": c.get("family", t), "age": age,
                         "points": float(c["weight"]) * decay * scale})
    caps = policy.get("caps", {})
    scale_positive(contribs, "source", caps.get("per_source", {}), caps.get("default_source_cap"))
    scale_positive(contribs, "family", caps.get("per_family", {}), caps.get("default_family_cap"))
    contribs.sort(key=lambda c: (-c["points"], c["type"].encode()))
    return round(sum(c["points"] for c in contribs), 2), contribs, missing_value


# ---------- routing ----------
def snake_index(i, n):
    """0-based position in routing order -> SDR index: A,B,B,A,A,B..."""
    pos, rnd = i % n, i // n
    return pos if rnd % 2 == 0 else n - 1 - pos


def block_reason(score, acc, cooldown, r):
    if acc["account_id"] in cooldown:
        return "cooldown"
    if score < float(r.get("min_score", 0) or 0):
        return "below_min_score"
    for col in r.get("require_true", []) or []:
        if not as_bool(acc.get(col)):
            return f"missing:{col}"
    for col in r.get("exclude_if_true", []) or []:
        if as_bool(acc.get(col)):
            return f"excluded:{col}"
    return ""


def run(eligible, signals, policy, as_of, cooldown, skip_source=None):
    scored, missing_value = [], 0
    for aid, acc in eligible.items():
        score, contribs, mv = score_account(signals.get(aid, []), policy, as_of, skip_source)
        missing_value += mv
        if score > 0:
            scored.append((score, acc, contribs))
        elif contribs:
            scored.append((score, acc, None))  # net zero or negative: dropped below
    dropped_net = sum(1 for s in scored if s[2] is None)
    scored = [s for s in scored if s[2] is not None]
    scored.sort(key=lambda x: (-x[0], x[1]["account_name"].encode(), x[1]["account_id"].encode()))
    r = policy.get("routing", {})
    sdrs = r.get("sdrs") or ["SDR 1"]
    slots = len(sdrs) * int(r.get("accounts_per_sdr", 10))
    weekday_skipped = as_of.isoweekday() in (r.get("skip_weekdays") or [])
    assigned, blocks, i = {}, {}, 0
    for score, acc, _ in scored:
        aid = acc["account_id"]
        if weekday_skipped:
            blocks[aid] = "weekday_skipped"
            continue
        b = block_reason(score, acc, cooldown, r)
        blocks[aid] = b
        if b or i >= slots:
            continue
        assigned[aid] = sdrs[snake_index(i, len(sdrs))]
        i += 1
    return scored, assigned, blocks, dropped_net, missing_value, weekday_skipped


# ---------- main ----------
def main():
    p = argparse.ArgumentParser()
    p.add_argument("--accounts", required=True)
    p.add_argument("--signals", required=True)
    p.add_argument("--policy", required=True)
    p.add_argument("--out", required=True)
    p.add_argument("--as-of", required=True, help="YYYY-MM-DD")
    p.add_argument("--assignments", help="assignment memory CSV, read for the cooldown")
    p.add_argument("--commit", action="store_true", help="write today's assignments to --assignments")
    a = p.parse_args()

    policy = load_policy(a.policy)
    as_of = parse_date(a.as_of)
    if as_of is None:
        die("--as-of must be YYYY-MM-DD")
    if a.commit and not a.assignments:
        die("--commit needs --assignments")
    version = str(policy["version"])
    r = policy.get("routing", {})
    sdrs = r.get("sdrs") or ["SDR 1"]
    per = int(r.get("accounts_per_sdr", 10))
    cooldown_days = int(r.get("cooldown_days", 30))

    accounts = load_accounts(a.accounts)
    signals, bad_dates, orphans = load_signals(a.signals, accounts)
    history = load_assignments(a.assignments)
    cutoff = as_of - timedelta(days=cooldown_days)
    cooldown = {h["account_id"] for h in history
                if (d := parse_date(h["assigned_on"])) and cutoff < d < as_of}

    dropped, eligible = Counter(), {}
    for aid, acc in accounts.items():
        reason = exclusion_reason(acc, policy.get("exclusions", {})) or icp_reason(acc, policy.get("icp"))
        if reason:
            dropped[reason] += 1
        else:
            eligible[aid] = acc

    scored, assigned, blocks, net_neg, missing_value, wk = run(eligible, signals, policy, as_of, cooldown)
    if net_neg:
        dropped["net_zero_or_negative"] += net_neg
    no_sig = len(eligible) - len(scored) - net_neg
    if no_sig:
        dropped["no_scoring_signal"] += no_sig

    os.makedirs(a.out, exist_ok=True)
    rows = []
    for i, (score, acc, cs) in enumerate(scored, 1):
        rows.append({"rank": i, "account_id": acc["account_id"], "account_name": acc["account_name"],
                     "domain": acc.get("domain", ""), "score": f"{score:.2f}",
                     "assigned_to": assigned.get(acc["account_id"], ""),
                     "routing_block": blocks.get(acc["account_id"], ""), "top_signal": cs[0]["type"],
                     "breakdown": "; ".join(f"{c['type']} ({c['age']}d, {c['points']:.1f} pts)" for c in cs)})
    write_csv(os.path.join(a.out, "ranked_accounts.csv"),
              ["rank", "account_id", "account_name", "domain", "score", "assigned_to", "routing_block",
               "top_signal", "breakdown"], rows, for_humans=True)

    # assignment memory, only on --commit
    committed = False
    if a.commit and not wk:
        today = as_of.isoformat()
        old_today = {h["account_id"]: h for h in history if h["assigned_on"] == today}
        kept = [h for h in history if h["assigned_on"] != today
                or (h["outcome"] and h["account_id"] not in assigned)]
        for _, acc, _ in scored:
            aid = acc["account_id"]
            if aid in assigned:
                kept.append({"account_id": aid, "assigned_to": assigned[aid], "assigned_on": today,
                             "policy_version": version, "outcome": old_today.get(aid, {}).get("outcome", "")})
        write_csv(a.assignments, ASSIGN_COLS, kept)
        committed = True

    # source influence
    src_points = Counter()
    for _, _, cs in scored:
        for c in cs:
            if c["points"] > 0:
                src_points[c["source"]] += c["points"]
    total_pts = sum(src_points.values()) or 1
    top = set(assigned)
    influence = {src: len(top - set(run(eligible, signals, policy, as_of, cooldown, src)[1]))
                 for src in src_points}

    # freshness: among eligible accounts holding a signal type, share whose latest one is past its half-life
    fresh = []
    for t, c in policy["signals"].items():
        hl = c.get("half_life_days")
        ages = []
        for aid in eligible:
            ds = [s["date"] for s in signals.get(aid, []) if s["type"] == t and s["date"] <= as_of]
            if ds:
                ages.append((as_of - max(ds)).days)
        if ages and hl:
            stale = sum(1 for x in ages if x > hl)
            fresh.append(f"- {t}: {len(ages)} accounts, {100 * stale / len(ages):.0f}% older than half-life ({hl}d)")

    ignored = Counter(s["type"] for ss in signals.values() for s in ss if s["type"] not in policy["signals"])
    future = sum(1 for ss in signals.values() for x in ss if x["date"] > as_of)
    block_counts = Counter(b for b in blocks.values() if b)

    L = [f"# GTM Cook run: policy {version}, as of {as_of}", "", f"Policy notes: {policy['notes']}", "",
         f"- Accounts in file: {len(accounts)}",
         f"- Scored: {len(scored)}",
         f"- Routed: {len(assigned)} ({len(sdrs)} SDRs x {per}, snake order)",
         f"- Assignment memory: {'updated (' + a.assignments + ')' if committed else 'not updated (dry run)'}",
         "", "## Dropped accounts"]
    L += [f"- {k}: {v}" for k, v in dropped.most_common()] or ["- none"]
    L += ["", "## Scored but not routed"]
    L += [f"- {k}: {v}" for k, v in block_counts.most_common()] or ["- none"]
    L += ["", f"## Source influence (routed top = {len(top)} accounts)",
          "share of positive points | accounts that leave the routed top if this source is removed"]
    L += [f"- {s}: {100 * v / total_pts:.0f}% | {influence[s]}/{len(top)}" for s, v in src_points.most_common()] \
         or ["- none"]
    L += ["", "## Freshness (latest signal per account vs half-life)"] + (fresh or ["- none"])
    if ignored:
        L += ["", "## Signal types in the file but not in the policy (ignored)"]
        L += [f"- {t}: {n} rows" for t, n in ignored.most_common()]
    warn = []
    if wk:
        warn.append(f"{as_of} is a skipped weekday (routing.skip_weekdays): nothing routed, memory untouched.")
    if future:
        warn.append(f"{future} signal rows are dated after {as_of} and were ignored.")
    if bad_dates:
        warn.append(f"{bad_dates} signal rows skipped because signal_date is not YYYY-MM-DD.")
    if orphans:
        warn.append(f"{orphans} signal rows point to an account_id absent from the accounts file.")
    if missing_value:
        warn.append(f"{missing_value} valued signals had no signal_value and counted zero.")
    if not wk and len(assigned) < len(sdrs) * per:
        warn.append(f"only {len(assigned)} accounts routed for {len(sdrs) * per} slots. See 'Scored but not routed'.")
    if warn:
        L += ["", "## Warnings"] + [f"- {w}" for w in warn]
    L += ["", "## Calibration", "Weights are assumptions until checked against outcomes. "
          "Run scripts/calibration.py once outcomes are recorded."]
    with open(os.path.join(a.out, "report.md"), "w", encoding="utf-8") as f:
        f.write("\n".join(L) + "\n")
    with open(os.path.join(a.out, "run.json"), "w", encoding="utf-8") as f:
        json.dump({"policy_version": version, "policy_scoring_hash": scoring_hash(policy), "policy_file": os.path.abspath(a.policy), "as_of": as_of.isoformat(),
                   "scored": len(scored), "routed": len(assigned), "committed": committed}, f, indent=2)
    print("\n".join(L))


if __name__ == "__main__":
    main()
