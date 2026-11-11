#!/usr/bin/env python3
"""The signal engine's intake: turn raw exports (CRM, job boards, enrichment, web analytics...) into the
two files GTM Cook scores, using a mapping file Claude writes with the user. Stdlib only, deterministic.

Usage:
  python ingest.py accounts --input crm_export.csv --mapping mappings/accounts.json --out data/accounts.csv
  python ingest.py signals  --input jobs_export.csv --mapping mappings/jobs.json \
                            --accounts data/accounts.csv --out data/signals.csv

accounts: rebuilds data/accounts.csv from one export (one row per account).
signals:  replaces every row of this mapping's "source" in data/signals.csv, keeps other sources.
          Rows that match no account go to <out dir>/unmatched_<source>.csv, never guessed.
See references/mappings.md for the mapping format.
"""
import argparse, json, os, sys
from collections import Counter, defaultdict
from datetime import timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import (ACCOUNT_REQUIRED, IDENT, die, parse_date, as_num, read_csv, write_csv, normalize_domain)

SIGNAL_COLS = ["account_id", "signal_type", "signal_source", "signal_date", "signal_value"]
OPS = {"eq", "ne", "gt", "gte", "lt", "lte", "contains", "not_contains", "in", "not_in", "not_empty", "empty"}


def need_col(cols, c, where):
    if c not in cols:
        die(f"{where}: column '{c}' is not in the input file. Columns found: {', '.join(cols)}")


def check_conditions(conds, cols, where):
    for c in conds or []:
        need_col(cols, c.get("column"), where)
        if c.get("op") not in OPS:
            die(f"{where}: op '{c.get('op')}' unknown. Use one of: {', '.join(sorted(OPS))}")


def holds(row, cond):
    raw, op, val = row.get(cond["column"], ""), cond["op"], cond.get("value")
    if op == "not_empty":
        return raw != ""
    if op == "empty":
        return raw == ""
    if op in ("gt", "gte", "lt", "lte"):
        x, y = as_num(raw), as_num(val)
        if x is None or y is None:
            return False
        return {"gt": x > y, "gte": x >= y, "lt": x < y, "lte": x <= y}[op]
    s = raw.lower()
    if op in ("in", "not_in"):
        inside = s in {str(v).lower() for v in (val or [])}
        return inside if op == "in" else not inside
    v = str(val).lower()
    return {"eq": s == v, "ne": s != v, "contains": v in s, "not_contains": v not in s}[op]


def all_hold(row, conds):
    return all(holds(row, c) for c in conds or [])


def ingest_accounts(a, mp):
    cols, rows = read_csv(a.input, (), "input file")
    colmap = mp.get("columns") or {}
    for t in ACCOUNT_REQUIRED:
        if t not in colmap:
            die(f"mapping 'columns' must map '{t}'")
    for t, src in colmap.items():
        if not IDENT.match(t):
            die(f"mapping target column '{t}' must be lowercase letters, digits, _")
        need_col(cols, src, "columns")
    flags = mp.get("flags") or {}
    for t, conds in flags.items():
        if not IDENT.match(t):
            die(f"flag '{t}' must be lowercase letters, digits, _")
        check_conditions(conds, cols, f"flag {t}")
    out_cols = list(colmap) + [f for f in flags if f not in colmap]
    seen, out, dup, empty_id = set(), [], [], 0
    for r in rows:
        o = {t: r.get(src, "") for t, src in colmap.items()}
        if not o["account_id"]:
            empty_id += 1
            continue
        if "domain" in o:
            o["domain"] = normalize_domain(o["domain"])
        for f, conds in flags.items():
            o[f] = "true" if all_hold(r, conds) else "false"
        if o["account_id"] in seen:
            dup.append(o["account_id"])
            continue
        seen.add(o["account_id"])
        out.append(o)
    if dup:
        die(f"{len(dup)} duplicate account_id in the export (first: {', '.join(dup[:5])}). "
            "Deduplicate at the source, GTM Cook won't pick one for you.")
    write_csv(a.out, out_cols, out)
    print(f"accounts: {len(rows)} rows read, {len(out)} written to {a.out}, {empty_id} skipped (empty account_id)")
    for c in out_cols:
        miss = sum(1 for o in out if not o.get(c))
        if miss:
            print(f"  {c}: {miss} empty ({100 * miss / max(len(out), 1):.0f}%)")


def ingest_signals(a, mp):
    source = str(mp.get("source") or "").strip()
    if not source:
        die("mapping needs a 'source' name (it identifies these rows for later re-ingestion)")
    cols, rows = read_csv(a.input, (), "input file")
    m = mp.get("match") or {}
    need_col(cols, m.get("column"), "match")
    if m.get("on") not in ("account_id", "domain"):
        die("match.on must be 'account_id' or 'domain'")
    dt = mp.get("date") or {}
    need_col(cols, dt.get("column"), "date")
    fmts = tuple(dt.get("formats") or ["%Y-%m-%d"])
    rules = mp.get("rules") or []
    if not rules:
        die("mapping needs at least one rule")
    for i, rule in enumerate(rules):
        if not rule.get("signal_type") or not IDENT.match(rule["signal_type"]):
            die(f"rule {i}: signal_type must be lowercase letters, digits, _")
        check_conditions(rule.get("when"), cols, f"rule {rule['signal_type']}")
        if rule.get("value_column"):
            need_col(cols, rule["value_column"], f"rule {rule['signal_type']} value_column")
        ag = rule.get("aggregate")
        if ag and (int(ag.get("window_days", 0)) < 1 or int(ag.get("min_count", 0)) < 1):
            die(f"rule {rule['signal_type']}: aggregate needs window_days >= 1 and min_count >= 1")

    _, accounts = read_csv(a.accounts, ACCOUNT_REQUIRED, "accounts file")
    if m["on"] == "domain":
        index = defaultdict(list)
        for acc in accounts:
            d = normalize_domain(acc.get("domain"))
            if d:
                index[d].append(acc["account_id"])
        ambiguous = {d for d, ids in index.items() if len(ids) > 1}
        lookup = {d: ids[0] for d, ids in index.items() if len(ids) == 1}
    else:
        ambiguous, lookup = set(), {acc["account_id"]: acc["account_id"] for acc in accounts}

    stats, unmatched, out = Counter(), [], []
    agg_rows = defaultdict(list)
    for r in rows:
        key = normalize_domain(r[m["column"]]) if m["on"] == "domain" else r[m["column"]]
        aid = lookup.get(key)
        if aid is None:
            stats["ambiguous_domain" if key in ambiguous else "unmatched"] += 1
            unmatched.append(r)
            continue
        d = parse_date(r[dt["column"]], fmts)
        if d is None:
            stats["bad_date"] += 1
            continue
        stats["matched"] += 1
        for rule in rules:
            if not all_hold(r, rule.get("when")):
                continue
            if rule.get("aggregate"):
                agg_rows[(rule["signal_type"], aid)].append(d)
            else:
                v = as_num(r.get(rule["value_column"])) if rule.get("value_column") else None
                out.append({"account_id": aid, "signal_type": rule["signal_type"], "signal_source": source,
                            "signal_date": d.isoformat(), "signal_value": "" if v is None else f"{v:g}"})

    agg = {rule["signal_type"]: rule["aggregate"] for rule in rules if rule.get("aggregate")}
    for (t, aid), dates in agg_rows.items():
        w, k = int(agg[t]["window_days"]), int(agg[t]["min_count"])
        dates.sort()
        for d in sorted(set(dates)):
            n = sum(1 for x in dates if d - timedelta(days=w) < x <= d)
            if n >= k:
                out.append({"account_id": aid, "signal_type": t, "signal_source": source,
                            "signal_date": d.isoformat(), "signal_value": str(n)})

    uniq = {tuple(o[c] for c in SIGNAL_COLS): o for o in out}
    out = sorted(uniq.values(), key=lambda o: tuple(o[c] for c in SIGNAL_COLS))
    kept = []
    if os.path.exists(a.out):
        _, existing = read_csv(a.out, ["account_id", "signal_type", "signal_source", "signal_date"], "signals file")
        kept = [{c: e.get(c, "") for c in SIGNAL_COLS} for e in existing if e["signal_source"] != source]
    write_csv(a.out, SIGNAL_COLS, kept + out)

    base = os.path.dirname(os.path.abspath(a.out))
    um_path = os.path.join(base, f"unmatched_{source}.csv")
    if unmatched:
        write_csv(um_path, cols, unmatched, for_humans=True)
    elif os.path.exists(um_path):
        os.remove(um_path)
    print(f"signals [{source}]: {len(rows)} rows read, {stats['matched']} matched to an account, "
          f"{stats['unmatched']} unmatched, {stats['ambiguous_domain']} on a domain shared by several accounts, "
          f"{stats['bad_date']} with an unreadable date")
    if unmatched:
        print(f"  unmatched rows saved to {um_path}")
    by_type = Counter(o["signal_type"] for o in out)
    for t in [r["signal_type"] for r in rules]:
        print(f"  {t}: {by_type.get(t, 0)} signals, {len({o['account_id'] for o in out if o['signal_type'] == t})} accounts")
    print(f"  {a.out}: {len(kept)} rows from other sources kept, {len(out)} rows from {source} written")


def main():
    p = argparse.ArgumentParser()
    p.add_argument("kind", choices=["accounts", "signals"])
    p.add_argument("--input", required=True)
    p.add_argument("--mapping", required=True)
    p.add_argument("--out", required=True)
    p.add_argument("--accounts", help="data/accounts.csv (signals only)")
    a = p.parse_args()
    with open(a.mapping, encoding="utf-8") as f:
        mp = json.load(f)
    if a.kind == "accounts":
        ingest_accounts(a, mp)
    else:
        if not a.accounts:
            die("signals ingestion needs --accounts")
        ingest_signals(a, mp)


if __name__ == "__main__":
    main()
