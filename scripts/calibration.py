#!/usr/bin/env python3
"""Is there enough outcome volume to calibrate weights? Per signal type.

Usage:
  python calibration.py --assignments assignments.csv --signals signals.csv --policy policy_vN.json
                        [--success converted] [--min-gap 0.05]

Unit = one account, not one assignment: an account assigned 5 times is one observation, otherwise
repeats inflate n. An account is a success if any of its outcomes is in --success (comma-separated);
otherwise, if it has at least one outcome, a failure. Signals "present" are judged at the account's
latest assignment that has an outcome: a policy signal dated on or before it, within max_age_days.

Sample size: to detect a conversion gap d at 80% power and alpha 0.05 (two-sided), you need about
n = 16 * p * (1 - p) / d^2 accounts per group, p = baseline conversion rate.
"""
import argparse, math, os, sys
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import ASSIGN_COLS, SIGNAL_REQUIRED, parse_date, read_csv, load_policy


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--assignments", required=True)
    ap.add_argument("--signals", required=True)
    ap.add_argument("--policy", required=True)
    ap.add_argument("--success", default="converted")
    ap.add_argument("--min-gap", type=float, help="smallest conversion gap worth detecting, e.g. 0.05")
    a = ap.parse_args()

    conf = load_policy(a.policy)["signals"]
    ok = {s.strip().lower() for s in a.success.split(",") if s.strip()}

    sigs = defaultdict(list)
    for r in read_csv(a.signals, SIGNAL_REQUIRED, "signals file")[1]:
        dt = parse_date(r["signal_date"])
        if r["signal_type"] in conf and dt:
            sigs[r["account_id"]].append((r["signal_type"], dt))

    per_acc, n_assign = {}, 0
    for r in read_csv(a.assignments, ASSIGN_COLS, "assignments file")[1]:
        out, on = r["outcome"].lower(), parse_date(r["assigned_on"])
        if not out or not on:
            continue
        n_assign += 1
        cur = per_acc.setdefault(r["account_id"], {"win": False, "on": on})
        cur["win"] |= out in ok
        cur["on"] = max(cur["on"], on)

    obs = []
    for aid, v in per_acc.items():
        present = {t for t, dt in sigs.get(aid, []) if dt <= v["on"] and
                   (conf[t].get("max_age_days") is None or (v["on"] - dt).days <= conf[t]["max_age_days"])}
        obs.append((v["win"], present))

    N = len(obs)
    if N == 0:
        sys.exit("No assignment with an outcome yet. Record outcomes first: nothing can be calibrated.")
    wins = sum(1 for w, _ in obs if w)
    p = wins / N
    print(f"Accounts with an outcome: {N} (from {n_assign} assignments with an outcome), successes: {wins}, "
          f"baseline conversion p = {p:.3f}")
    if p in (0, 1):
        print("p is 0 or 1: no contrast at all yet, the sample size cannot be estimated. Keep collecting.")
        return
    need = 16 * p * (1 - p) / a.min_gap ** 2 if a.min_gap else None
    if need:
        print(f"Needed per group to detect a gap of {a.min_gap:.3f}: n = 16*p*(1-p)/d^2 = {math.ceil(need)} accounts")
    print()
    print("signal_type | n with | conv with | n without | conv without | smallest detectable gap | volume reached")
    tested = 0
    for t in conf:
        w = [x for x, pr in obs if t in pr]
        wo = [x for x, pr in obs if t not in pr]
        nmin = min(len(w), len(wo))
        tested += 1 if nmin else 0
        gap = math.sqrt(16 * p * (1 - p) / nmin) if nmin else float("inf")
        cw = f"{sum(w) / len(w):.3f}" if w else "-"
        cwo = f"{sum(wo) / len(wo):.3f}" if wo else "-"
        if need:
            reached = "yes" if nmin >= need else f"no (need {math.ceil(need) - nmin} more in the smaller group)"
        else:
            reached = "set --min-gap"
        gap_s = "-" if not math.isfinite(gap) else (">1" if gap > 1 else f"{gap:.3f}")
        print(f"{t} | {len(w)} | {cw} | {len(wo)} | {cwo} | {gap_s} | {reached}")
    print()
    print("How to read this:")
    print("- 'Smallest detectable gap': below this difference between 'with' and 'without', the data cannot")
    print("  tell signal from noise. Weight changes inside that gap are hunches. It uses the smaller group: conservative.")
    if tested > 1:
        print(f"- {tested} signal types tested at once: even with no real effect, the chance that at least one shows")
        print(f"  a 'significant' gap is {100 * (1 - 0.95 ** tested):.0f}%. One standout type is not proof.")
    print("- Observational and limited to accounts that were routed (already high scorers): a gap is a lead")
    print("  for a weight change, not a measured effect of the signal.")


if __name__ == "__main__":
    main()
