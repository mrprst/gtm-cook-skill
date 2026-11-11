#!/usr/bin/env python3
"""Step gate for a GTM Cook project. A step can only be approved when its checks pass and every
earlier step is approved and unchanged since approval. Changing an approved step's files makes it
stale: it and every later step must be approved again.

Usage:
  python check.py --project DIR status        # where are we, what is the next step
  python check.py --project DIR check N       # run step N's checks
  python check.py --project DIR approve N     # record the user's approval of step N (checks must pass)

Steps: 1 context & ICP, 2 signal catalog, 3 data, 4 scoring, 5 routing, 6 production, 7 calibration.
"""
import argparse, glob, hashlib, json, os, re, sys
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import ACCOUNT_REQUIRED, SIGNAL_REQUIRED, ASSIGN_COLS, IDENT, read_csv, scoring_hash

STEPS = {1: "Context & ICP", 2: "Signal catalog", 3: "Data (signal engine intake)", 4: "Scoring policy",
         5: "Routing (attribution)", 6: "Production", 7: "Calibration"}
LAST_GATED = 6


class Fail(Exception):
    pass


def jload(path):
    if not os.path.exists(path):
        raise Fail(f"missing {os.path.relpath(path)}")
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except json.JSONDecodeError as e:
        raise Fail(f"{os.path.relpath(path)} is not valid JSON: {e}")


def h(obj):
    return hashlib.sha256(json.dumps(obj, sort_keys=True).encode()).hexdigest()[:16]


def fhash(paths):
    m = hashlib.sha256()
    for p in sorted(paths):
        m.update(p.encode())
        with open(p, "rb") as f:
            m.update(f.read())
    return m.hexdigest()[:16]


class Project:
    def __init__(self, root):
        self.root = os.path.abspath(root)

    def p(self, *parts):
        return os.path.join(self.root, *parts)

    def project(self):
        return jload(self.p("project.json"))

    def catalog(self):
        cat = jload(self.p("signal_catalog.json"))
        return {s.get("signal_type"): s for s in cat.get("signals", [])}

    def latest_policy(self):
        files = glob.glob(self.p("policies", "policy_v*.json"))
        if not files:
            raise Fail("no policies/policy_vN.json yet")
        num = lambda f: int(re.search(r"policy_v(\d+)\.json$", f).group(1)) if re.search(r"policy_v(\d+)\.json$", f) else -1
        f = max(files, key=num)
        return f, jload(f)

    def runs_for(self, version):
        out = []
        for rj in glob.glob(self.p("runs", "*", "run.json")):
            r = jload(rj)
            if str(r.get("policy_version")) == str(version):
                out.append(r)
        return out


# ---------- checks: each returns (hash_of_what_gets_approved, notes) or raises Fail ----------
def check1(P):
    pj = P.project()
    errs = []
    if pj.get("mode") not in ("own", "demo"):
        errs.append("project.json 'mode' must be 'own' (the user's data) or 'demo' (fictional data): ask the user first")
    for k in ("product", "buying_moment"):
        if len(str(pj.get(k) or "").strip()) < 10:
            errs.append(f"project.json '{k}' must describe it in a sentence (got: {pj.get(k)!r})")
    icp = pj.get("icp")
    if not isinstance(icp, dict):
        errs.append("project.json needs an 'icp' object")
    else:
        has = any(icp.get(k) not in (None, [], "") for k in ("employees_min", "employees_max", "industries", "countries"))
        if not has and not str(icp.get("no_filter_reason") or "").strip():
            errs.append("icp has no criterion: set one, or write the user's reason in icp.no_filter_reason")
    if not isinstance(pj.get("exclusions"), dict):
        errs.append("project.json needs an 'exclusions' object")
    if errs:
        raise Fail("; ".join(errs))
    return h({k: pj.get(k) for k in ("mode", "product", "buying_moment", "icp", "exclusions")}), []


def check2(P):
    cat = P.catalog()
    errs, notes = [], []
    if not cat:
        raise Fail("signal_catalog.json has no signals")
    for t, s in cat.items():
        if not t or not IDENT.match(t):
            errs.append(f"signal_type {t!r} must be lowercase letters, digits, _")
            continue
        if s.get("direction") not in ("positive", "negative"):
            errs.append(f"{t}: direction must be positive or negative")
        if s.get("kind") not in ("binary", "valued"):
            errs.append(f"{t}: kind must be binary or valued")
        if len(str(s.get("why") or "").strip()) < 10:
            errs.append(f"{t}: 'why' must say in a sentence how it relates to the buying moment")
        if not str(s.get("source") or "").strip():
            errs.append(f"{t}: source must name where it comes from, or 'not_sourced'")
        if not str(s.get("family") or "").strip():
            errs.append(f"{t}: family is required")
        if s.get("kind") == "valued" and len(str(s.get("value_unit") or "").strip()) < 3:
            errs.append(f"{t}: valued signals need 'value_unit' (e.g. 'open roles in the last 30 days'), "
                        "identical for every source feeding this type")
        if not isinstance(s.get("refresh_days"), int) or s["refresh_days"] < 1:
            errs.append(f"{t}: refresh_days (how often the source is re-pulled) must be a whole number of days >= 1")
    sourced_pos = [t for t, s in cat.items() if s.get("direction") == "positive" and s.get("source") != "not_sourced"]
    if not sourced_pos:
        errs.append("at least one positive signal must have a real source")
    unsourced = [t for t, s in cat.items() if s.get("source") == "not_sourced"]
    if unsourced:
        notes.append(f"not sourced yet (kept in the catalog, ignored downstream): {', '.join(unsourced)}")
    if errs:
        raise Fail("; ".join(errs))
    return fhash([P.p("signal_catalog.json")]), notes


def check3(P):
    cat = P.catalog()
    acc_path, sig_path = P.p("data", "accounts.csv"), P.p("data", "signals.csv")
    for f in (acc_path, sig_path):
        if not os.path.exists(f):
            raise Fail(f"missing {os.path.relpath(f, P.root)}: run scripts/ingest.py")
    try:
        _, accs = read_csv(acc_path, ACCOUNT_REQUIRED, "accounts file")
        _, sigs = read_csv(sig_path, SIGNAL_REQUIRED, "signals file")
    except SystemExit as e:
        raise Fail(str(e))
    errs, notes = [], []
    ids = [a["account_id"] for a in accs]
    if not ids:
        errs.append("accounts.csv is empty")
    if len(ids) != len(set(ids)):
        errs.append("accounts.csv has duplicate account_id")
    known = set(ids)
    demo_rows = sum(1 for a in accs if (a.get("domain") or "").endswith(".example"))
    mode = P.project().get("mode")
    if mode == "own" and demo_rows:
        errs.append(f"{demo_rows} accounts have demo domains (.example): this project uses the user's own data, "
                    "remove the demo files from data/ and ingest the user's exports")
    if mode == "demo":
        notes.append("DEMO DATA: fictional accounts and signals (assets/demo/README.md lists the planted anomalies)")
    orphans = sum(1 for s in sigs if s["account_id"] not in known)
    if orphans:
        errs.append(f"{orphans} signal rows point to unknown account_id: re-run ingest after the accounts")
    types = {s["signal_type"] for s in sigs}
    extra = types - set(cat)
    if extra:
        errs.append(f"signal types in the data but not in the catalog: {', '.join(sorted(extra))}")
    for t, c in cat.items():
        if c.get("source") == "not_sourced":
            continue
        rows = [s for s in sigs if s["signal_type"] == t]
        if not rows:
            errs.append(f"catalog signal '{t}' (source {c.get('source')}) has no rows: ingest it or mark it not_sourced")
        elif c.get("kind") == "valued" and not any(s.get("signal_value") for s in rows):
            errs.append(f"'{t}' is valued but no row has a signal_value")
        else:
            notes.append(f"{t}: {len(rows)} rows, {len({s['account_id'] for s in rows})} accounts")
    for um in glob.glob(P.p("data", "unmatched_*.csv")):
        n = len(read_csv(um, (), "unmatched file")[1])
        notes.append(f"{os.path.basename(um)}: {n} rows matched no account (review with the user)")
    if errs:
        raise Fail("; ".join(errs))
    return fhash([acc_path, sig_path]), notes


def check4(P):
    pj, cat = P.project(), P.catalog()
    f, pol = P.latest_policy()
    errs, notes = [], [f"latest policy: {os.path.basename(f)} ({pol.get('version')})"]
    for k in ("version", "notes"):
        if not str(pol.get(k) or "").strip():
            errs.append(f"policy needs a non-empty '{k}'")
    for k in ("icp", "exclusions"):
        if pol.get(k) != pj.get(k):
            errs.append(f"policy '{k}' differs from project.json (step 1 is the source: change it there first)")
    for t, c in (pol.get("signals") or {}).items():
        if t not in cat:
            errs.append(f"policy signal '{t}' is not in the catalog")
            continue
        w = c.get("weight")
        if not isinstance(w, (int, float)):
            errs.append(f"{t}: weight must be a number")
        elif (w < 0) != (cat[t].get("direction") == "negative"):
            errs.append(f"{t}: weight sign does not match catalog direction ({cat[t].get('direction')})")
        if bool(c.get("value_scale")) != (cat[t].get("kind") == "valued"):
            errs.append(f"{t}: value_scale must be set if and only if the catalog kind is valued")
    if not pol.get("signals"):
        errs.append("policy has no signals")
    runs = P.runs_for(pol.get("version"))
    if not runs:
        errs.append(f"no run of {pol.get('version')} in runs/: run score.py (dry run) with --out runs/<as_of>_{pol.get('version')}")
    elif any(x.get("policy_scoring_hash") != scoring_hash(pol) for x in runs):
        errs.append(f"{os.path.basename(f)} was edited after it was run: save the change as the next policy_vN.json "
                    "(versions are never edited once run), or delete the outdated runs/ folders if it never left review")
    fb = P.p("feedback.csv")
    if not os.path.exists(fb):
        errs.append("no feedback.csv: review the top accounts with the user and record the answers")
    else:
        _, rows = read_csv(fb, ["date", "account_id", "policy_version", "feedback", "note"], "feedback file")
        if not any(r["policy_version"] == str(pol.get("version")) for r in rows):
            errs.append(f"no feedback row for {pol.get('version')}: the user must review this version "
                        "(feedback 'ok' with account_id '-' if they accept it as is)")
    if errs:
        raise Fail("; ".join(errs))
    return h({k: v for k, v in pol.items() if k != "routing"}), notes


def check5(P):
    f, pol = P.latest_policy()
    r = pol.get("routing") or {}
    errs, notes = [], []
    for k in ("sdrs", "accounts_per_sdr", "cooldown_days", "min_score", "require_true", "exclude_if_true",
              "skip_weekdays"):
        if k not in r:
            errs.append(f"routing.{k} must be set explicitly" + (" (an empty list is a valid answer)"
                        if k in ("require_true", "exclude_if_true", "skip_weekdays") else ""))
    if not r.get("sdrs"):
        errs.append("routing.sdrs is empty")
    acc_path = P.p("data", "accounts.csv")
    if os.path.exists(acc_path):
        cols, _ = read_csv(acc_path, (), "accounts file")
        for col in (r.get("require_true") or []) + (r.get("exclude_if_true") or []):
            if col not in cols:
                errs.append(f"routing column '{col}' is not in data/accounts.csv: add it at intake (step 3)")
    runs = P.runs_for(pol.get("version"))
    if not any(x.get("routed", 0) > 0 for x in runs):
        errs.append(f"no run of {pol.get('version')} routed any account: check 'Scored but not routed' in the report")
    else:
        best = max(runs, key=lambda x: x["as_of"])
        notes.append(f"last run {best['as_of']}: {best['routed']} routed, committed={best.get('committed')}")
    if errs:
        raise Fail("; ".join(errs))
    return h(r), notes


def check6(P):
    pj = P.project()
    f, pol = P.latest_policy()
    errs = []
    sql = P.p("scoring.sql")
    if not os.path.exists(sql):
        errs.append("no scoring.sql: run to_sql.py on the latest policy")
    else:
        first = open(sql, encoding="utf-8").readline()
        if f"policy {pol.get('version')}." not in first:
            errs.append(f"scoring.sql was not generated from {pol.get('version')}: regenerate it")
    prod = pj.get("production") or {}
    for k, what in (("schedule", "when the nightly job runs and on what (e.g. pg_cron 06:00 UTC)"),
                    ("signal_refresh", "how each sourced signal gets refreshed in the signals table"),
                    ("outcome_recording", "who writes outcomes into assignments.outcome, where and when")):
        if len(str(prod.get(k) or "").strip()) < 10:
            errs.append(f"project.json production.{k}: {what}")
    if errs:
        raise Fail("; ".join(errs))
    return fhash([sql]) + h(prod), []


CHECKS = {1: check1, 2: check2, 3: check3, 4: check4, 5: check5, 6: check6}


def calibration_status(P):
    path = P.p("assignments.csv")
    if not os.path.exists(path):
        return "no assignment memory yet"
    _, rows = read_csv(path, ASSIGN_COLS, "assignments file")
    with_out = [r for r in rows if r["outcome"]]
    return (f"{len(rows)} assignments, {len(with_out)} with an outcome, "
            f"{len({r['account_id'] for r in with_out})} accounts: run calibration.py")


def run_check(P, n):
    try:
        digest, notes = CHECKS[n](P)
        return True, digest, notes
    except Fail as e:
        return False, None, [str(e)]


def state(P):
    """Per step: approved & fresh / stale / not approved. Stops trusting approvals after the first gap."""
    try:
        approvals = P.project().get("approvals") or {}
    except Fail:
        approvals = {}
    out, chain_ok = {}, True
    for n in range(1, LAST_GATED + 1):
        ok, digest, notes = run_check(P, n)
        ap = approvals.get(str(n))
        if not chain_ok:
            status = "blocked"
        elif ap and ok and ap.get("hash") == digest:
            status = "approved"
        elif ap:
            status = "stale"
            chain_ok = False
        else:
            status = "next"
            chain_ok = False
        out[n] = (status, ok, notes)
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--project", default=".")
    ap.add_argument("cmd", choices=["status", "check", "approve"])
    ap.add_argument("step", nargs="?", type=int)
    a = ap.parse_args()
    P = Project(a.project)

    if a.cmd == "status":
        try:
            mode = P.project().get("mode")
        except Fail:
            mode = None
        print({"demo": "Mode: DEMO (fictional data, nothing here is real)\n",
               "own": "Mode: own data\n"}.get(mode, "Mode: not chosen yet (ask: own data or demo?)\n"))
        st = state(P)
        nxt = next((n for n, (s, _, _) in st.items() if s in ("next", "stale")), None)
        for n, (s, ok, notes) in st.items():
            label = {"approved": "APPROVED", "stale": "CHANGED SINCE APPROVAL", "next": "TO DO", "blocked": "LOCKED"}[s]
            print(f"{n}. {STEPS[n]}: {label}" + ("" if s in ("blocked",) else f" (checks {'pass' if ok else 'fail'})"))
            if s != "blocked":
                for x in notes:
                    print(f"     {x}")
        print(f"7. {STEPS[7]}: {'available' if nxt is None else 'LOCKED'}"
              + (f" ({calibration_status(P)})" if nxt is None else ""))
        print(f"\nWork only on step {nxt}: {STEPS[nxt]}." if nxt else "\nAll gated steps approved.")
        return

    if a.step not in CHECKS:
        sys.exit(f"step must be 1..{LAST_GATED}")
    if a.cmd == "check":
        ok, _, notes = run_check(P, a.step)
        print(f"Step {a.step} {STEPS[a.step]}: {'PASS' if ok else 'FAIL'}")
        for x in notes:
            print(f"  {x}")
        sys.exit(0 if ok else 1)

    st = state(P)
    for n in range(1, a.step):
        if st[n][0] != "approved":
            sys.exit(f"REFUSED: step {n} ({STEPS[n]}) is not approved and unchanged. Finish it first.")
    ok, digest, notes = run_check(P, a.step)
    if not ok:
        sys.exit(f"REFUSED: step {a.step} checks fail: {notes[0]}")
    path = P.p("project.json")
    pj = P.project()
    appr = {k: v for k, v in (pj.get("approvals") or {}).items() if int(k) < a.step}
    appr[str(a.step)] = {"hash": digest, "on": date.today().isoformat()}
    pj["approvals"] = appr
    with open(path, "w", encoding="utf-8") as f:
        json.dump(pj, f, indent=2)
    print(f"Step {a.step} {STEPS[a.step]} approved. Later approvals cleared." if a.step < LAST_GATED
          else f"Step {a.step} {STEPS[a.step]} approved.")


if __name__ == "__main__":
    main()
