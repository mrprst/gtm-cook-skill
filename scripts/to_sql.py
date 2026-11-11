#!/usr/bin/env python3
"""Turn a GTM Cook policy into a Postgres script that reproduces score.py --commit.

Usage: python to_sql.py --policy policy_vN.json [--as-of YYYY-MM-DD] > scoring.sql

Expected tables (assets/schema.sql creates them; rename in the output if yours differ):
  accounts(account_id, account_name, domain, employees, industry, country, is_customer, is_blocked,
           + every boolean column named in routing.require_true / routing.exclude_if_true)
  signals(account_id, signal_type, signal_source, signal_date, signal_value)
  assignments(account_id, assigned_to, assigned_on, policy_version, outcome)  -- created if missing

One transaction: score and route into a temp table, return the ranking, then replace the as-of day's
batch in assignments. A same-day row that already has an outcome is never deleted.
"""
import argparse, os, sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import load_policy


def q(v):
    if v is None:
        return "NULL"
    if isinstance(v, bool):
        return "TRUE" if v else "FALSE"
    if isinstance(v, (int, float)):
        return repr(v)
    return "'" + str(v).replace("'", "''") + "'"


def values(rows, types):
    if not rows:
        return "SELECT " + ", ".join(f"NULL::{t}" for t in types) + " WHERE FALSE"
    body = ",\n    ".join("(" + ", ".join(f"{q(v)}::{t}" for v, t in zip(r, types)) + ")" for r in rows)
    return f"VALUES\n    {body}"


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--policy", required=True)
    p.add_argument("--as-of", help="YYYY-MM-DD literal instead of CURRENT_DATE")
    a = p.parse_args()
    pol = load_policy(a.policy)
    version = " ".join(str(pol["version"]).split())  # one line: it sits in a SQL comment
    as_of = f"DATE {q(a.as_of)}" if a.as_of else "CURRENT_DATE"
    C = 'COLLATE "C"'

    sig = []
    for t, c in pol["signals"].items():
        vs = c.get("value_scale") or {}
        sig.append((t, c.get("family", t), c["weight"], c.get("half_life_days"), c.get("max_age_days"),
                    vs.get("min"), vs.get("max")))
    valued = any(c.get("value_scale") for c in pol["signals"].values())
    val = "s.signal_value::numeric" if valued else "NULL::numeric"
    caps = pol.get("caps", {})
    src = list(caps.get("per_source", {}).items())
    fam = list(caps.get("per_family", {}).items())
    dsrc, dfam = caps.get("default_source_cap"), caps.get("default_family_cap")

    ex = pol.get("exclusions", {})
    where = ["TRUE"]
    if ex.get("exclude_customers", True):
        where.append("NOT COALESCE(a.is_customer, FALSE)")
    if ex.get("exclude_blocked", True):
        where.append("NOT COALESCE(a.is_blocked, FALSE)")
    if ex.get("require_domain", True):
        where.append("NULLIF(TRIM(a.domain), '') IS NOT NULL")
    icp = pol.get("icp") or {}
    if icp.get("employees_min") is not None:
        where.append(f"a.employees >= {q(icp['employees_min'])}")
    if icp.get("employees_max") is not None:
        where.append(f"a.employees <= {q(icp['employees_max'])}")
    for field, col in (("industries", "industry"), ("countries", "country")):
        if icp.get(field):
            lst = ", ".join(q(x.lower()) for x in icp[field])
            where.append(f"LOWER(a.{col}) IN ({lst})")

    r = pol.get("routing", {})
    sdrs = r.get("sdrs") or ["SDR 1"]
    n = len(sdrs)
    slots = n * int(r.get("accounts_per_sdr", 10))
    cooldown = int(r.get("cooldown_days", 30))
    min_score = float(r.get("min_score", 0) or 0)
    skip_days = r.get("skip_weekdays") or []
    skip = f"EXTRACT(ISODOW FROM {as_of}) IN ({', '.join(str(int(d)) for d in skip_days)})" if skip_days else "FALSE"
    arr = "ARRAY[" + ", ".join(q(s) for s in sdrs) + "]::text[]"
    block = ["WHEN skip.is_skipped THEN 'weekday_skipped'",
             "WHEN c.account_id IS NOT NULL THEN 'cooldown'",
             f"WHEN s.score < {q(min_score)} THEN 'below_min_score'"]
    block += [f"WHEN NOT COALESCE(e.{col}, FALSE) THEN {q('missing:' + col)}" for col in r.get("require_true") or []]
    block += [f"WHEN COALESCE(e.{col}, FALSE) THEN {q('excluded:' + col)}" for col in r.get("exclude_if_true") or []]
    block_sql = "CASE " + "\n              ".join(block) + " ELSE '' END"

    print(f"""-- GTM Cook scoring, policy {version}. Generated from the policy file: edit the policy, not this script.
-- Policy notes: {" ".join(str(pol['notes']).split())}
BEGIN;

CREATE TABLE IF NOT EXISTS assignments (
  account_id     text NOT NULL,
  assigned_to    text NOT NULL,
  assigned_on    date NOT NULL,
  policy_version text NOT NULL,
  outcome        text
);

CREATE TEMP TABLE gtm_cook_run ON COMMIT DROP AS
WITH policy(signal_type, family, weight, half_life_days, max_age_days, vmin, vmax) AS (
  {values(sig, ['text', 'text', 'numeric', 'numeric', 'int', 'numeric', 'numeric'])}
),
source_caps(signal_source, cap) AS (
  {values(src, ['text', 'numeric'])}
),
family_caps(family, cap) AS (
  {values(fam, ['text', 'numeric'])}
),
skip(is_skipped) AS (SELECT {skip}),
eligible AS (
  SELECT a.* FROM accounts a
  WHERE {(chr(10) + '    AND ').join(where)}
),
cooldown AS (  -- assigned on a previous day within the cooldown window
  SELECT DISTINCT account_id FROM assignments
  WHERE assigned_on > {as_of} - {cooldown} AND assigned_on < {as_of}
),
latest AS (  -- latest signal per account and type; same date: first source in byte order, then highest value
  SELECT DISTINCT ON (s.account_id, s.signal_type)
         s.account_id, s.signal_type, COALESCE(NULLIF(s.signal_source, ''), 'unknown') AS signal_source,
         s.signal_date, {val} AS signal_value
  FROM signals s
  JOIN policy p USING (signal_type)
  JOIN eligible e USING (account_id)
  WHERE s.signal_date <= {as_of}
  ORDER BY s.account_id, s.signal_type, s.signal_date DESC,
           COALESCE(NULLIF(s.signal_source, ''), 'unknown') {C}, {val} DESC NULLS LAST
),
raw AS (  -- weight x exponential decay by age x value scale (valued signals only)
  SELECT l.account_id, l.signal_type, l.signal_source, p.family,
         ({as_of} - l.signal_date) AS age_days,
         p.weight
           * CASE WHEN p.half_life_days IS NULL THEN 1
                  ELSE POWER(0.5::numeric, ({as_of} - l.signal_date) / p.half_life_days) END
           * CASE WHEN p.vmin IS NULL THEN 1
                  ELSE LEAST(1, GREATEST(0, (l.signal_value - p.vmin) / (p.vmax - p.vmin))) END AS pts
  FROM latest l JOIN policy p USING (signal_type)
  WHERE (p.max_age_days IS NULL OR ({as_of} - l.signal_date) <= p.max_age_days)
    AND (p.vmin IS NULL OR l.signal_value IS NOT NULL)
),
src_capped AS (  -- caps scale positive points only
  SELECT r.account_id, r.signal_type, r.family, r.age_days,
         CASE WHEN r.pts > 0 THEN r.pts * LEAST(1, COALESCE(sc.cap, {q(dsrc)}::numeric) /
             NULLIF(SUM(GREATEST(r.pts, 0)) OVER (PARTITION BY r.account_id, r.signal_source), 0))
              ELSE r.pts END AS pts
  FROM raw r LEFT JOIN source_caps sc USING (signal_source)
),
fam_capped AS (
  SELECT s.account_id, s.signal_type, s.age_days,
         CASE WHEN s.pts > 0 THEN s.pts * LEAST(1, COALESCE(fc.cap, {q(dfam)}::numeric) /
             NULLIF(SUM(GREATEST(s.pts, 0)) OVER (PARTITION BY s.account_id, s.family), 0))
              ELSE s.pts END AS pts
  FROM src_capped s LEFT JOIN family_caps fc USING (family)
),
scores AS (
  SELECT account_id,
         ROUND(SUM(pts), 2) AS score,
         STRING_AGG(signal_type || ' (' || age_days || 'd, ' || ROUND(pts, 1) || ' pts)', '; '
                    ORDER BY pts DESC, signal_type {C}) AS breakdown
  FROM fam_capped GROUP BY account_id
),
ranked AS (
  SELECT e.account_id, e.account_name, e.domain, s.score, s.breakdown,
         {block_sql} AS routing_block,
         ROW_NUMBER() OVER (ORDER BY s.score DESC, e.account_name {C}, e.account_id {C}) AS rank
  FROM scores s JOIN eligible e USING (account_id)
  CROSS JOIN skip
  LEFT JOIN cooldown c USING (account_id)
  WHERE s.score > 0
),
routed AS (  -- snake order among routable accounts: A,B,B,A,A,B...
  SELECT account_id, ROW_NUMBER() OVER (ORDER BY rank) - 1 AS i
  FROM ranked WHERE routing_block = ''
)
SELECT k.rank, k.account_id, k.account_name, k.domain, k.score,
       CASE WHEN ro.i < {slots} THEN ({arr})[
         CASE WHEN (ro.i / {n}) % 2 = 0 THEN ro.i % {n} ELSE {n} - 1 - ro.i % {n} END + 1] END AS assigned_to,
       k.routing_block, k.breakdown
FROM ranked k LEFT JOIN routed ro USING (account_id);

SELECT rank, account_id, account_name, domain, score, assigned_to, routing_block, breakdown
FROM gtm_cook_run ORDER BY rank;

-- assignment memory: replace the day's batch; never delete a same-day row that has an outcome
-- unless the account is routed again (then it is re-inserted with that outcome). Skipped weekday: untouched.
WITH old AS (
  DELETE FROM assignments a
  WHERE a.assigned_on = {as_of}
    AND NOT {skip}
    AND (NULLIF(a.outcome, '') IS NULL
         OR a.account_id IN (SELECT account_id FROM gtm_cook_run WHERE assigned_to IS NOT NULL))
  RETURNING account_id, outcome
)
INSERT INTO assignments (account_id, assigned_to, assigned_on, policy_version, outcome)
SELECT r.account_id, r.assigned_to, {as_of}, {q(version)}, old.outcome
FROM gtm_cook_run r LEFT JOIN old USING (account_id)
WHERE r.assigned_to IS NOT NULL;

COMMIT;""")


if __name__ == "__main__":
    main()
