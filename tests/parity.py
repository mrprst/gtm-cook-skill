#!/usr/bin/env python3
"""Check that score.py --commit and the SQL from to_sql.py agree on tests/fixtures (deliberately tricky data, not the demo): ranking, scores,
routing, routing blocks, and the assignments table after the run. Also checks that a dry run
(no --commit) leaves the assignment memory untouched.

Usage: python tests/parity.py --policy <policy.json> --as-of YYYY-MM-DD --psql "psql -d testdb"
Needs a scratch Postgres database (tables accounts/signals/assignments are dropped and recreated).
"""
import argparse, csv, filecmp, os, shlex, shutil, subprocess, sys, tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
SAMPLE = os.path.join(HERE, "fixtures")
SCHEMA = os.path.join(ROOT, "assets", "schema.sql")

LOAD = """
DROP TABLE IF EXISTS accounts, signals, assignments;
\\i {schema}
CREATE TEMP TABLE a_raw(account_id text, account_name text, domain text, employees text, industry text,
  country text, is_customer text, is_blocked text, has_contact_email text, owned_by_ae text);
\\copy a_raw FROM '{acc}' CSV HEADER
INSERT INTO accounts SELECT account_id, account_name, NULLIF(domain,''), NULLIF(employees,'')::int,
  NULLIF(industry,''), NULLIF(country,''), is_customer::boolean, is_blocked::boolean,
  has_contact_email::boolean, owned_by_ae::boolean FROM a_raw;
CREATE TEMP TABLE s_raw(account_id text, signal_type text, signal_source text, signal_date text, signal_value text);
\\copy s_raw FROM '{sig}' CSV HEADER
INSERT INTO signals SELECT account_id, signal_type, signal_source, signal_date::date, NULLIF(signal_value,'')::numeric
  FROM s_raw;
CREATE TEMP TABLE h_raw(account_id text, assigned_to text, assigned_on text, policy_version text, outcome text);
\\copy h_raw FROM '{asg}' CSV HEADER
INSERT INTO assignments SELECT account_id, assigned_to, assigned_on::date, policy_version, NULLIF(outcome,'') FROM h_raw;
"""


def psql(cmd, sql_file):
    out = subprocess.run(shlex.split(cmd) + ["-X", "-q", "-t", "-A", "-F", "\t", "-v", "ON_ERROR_STOP=1",
                                             "-f", sql_file], capture_output=True, text=True)
    if out.returncode:
        sys.exit(out.stderr)
    return out.stdout


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--policy", required=True)
    ap.add_argument("--as-of", required=True)
    ap.add_argument("--psql", required=True)
    a = ap.parse_args()
    tmp = tempfile.mkdtemp()
    os.chmod(tmp, 0o755)
    files = {}
    for name in ("accounts.csv", "signals.csv", "assignments.csv"):
        files[name] = os.path.join(tmp, name)
        shutil.copy(os.path.join(SAMPLE, name), files[name])
        os.chmod(files[name], 0o644)
    shutil.copy(SCHEMA, os.path.join(tmp, "schema.sql"))
    os.chmod(os.path.join(tmp, "schema.sql"), 0o644)

    load = os.path.join(tmp, "load.sql")
    open(load, "w").write(LOAD.format(schema=os.path.join(tmp, "schema.sql"), acc=files["accounts.csv"],
                                      sig=files["signals.csv"], asg=files["assignments.csv"]))
    psql(a.psql, load)

    q = os.path.join(tmp, "q.sql")
    with open(q, "w") as f:
        subprocess.run([sys.executable, os.path.join(ROOT, "scripts", "to_sql.py"), "--policy", a.policy,
                        "--as-of", a.as_of], stdout=f, check=True)
    os.chmod(q, 0o644)
    sql_lines = [l.split("\t") for l in psql(a.psql, q).splitlines() if l and l[0].isdigit()]
    sql = [(l[1], f"{float(l[4]):.2f}", l[5], l[6]) for l in sql_lines]

    score = [sys.executable, os.path.join(ROOT, "scripts", "score.py"), "--accounts", files["accounts.csv"],
             "--signals", files["signals.csv"], "--policy", a.policy, "--as-of", a.as_of]
    ok = True

    # dry run must not touch the memory
    py_asg = os.path.join(tmp, "py_assignments.csv")
    shutil.copy(files["assignments.csv"], py_asg)
    subprocess.run(score + ["--out", os.path.join(tmp, "dry"), "--assignments", py_asg],
                   check=True, capture_output=True)
    if not filecmp.cmp(py_asg, files["assignments.csv"], shallow=False):
        ok = False
        print("DRY RUN MODIFIED THE ASSIGNMENT MEMORY")

    subprocess.run(score + ["--out", os.path.join(tmp, "out"), "--assignments", py_asg, "--commit"],
                   check=True, capture_output=True)
    py = [(r["account_id"], r["score"], r["assigned_to"], r["routing_block"])
          for r in csv.DictReader(open(os.path.join(tmp, "out", "ranked_accounts.csv")))]

    if py != sql:
        ok = False
        print("RANKING MISMATCH")
        for x, y in zip(py, sql):
            if x != y:
                print("  py ", x, "\n  sql", y)
        if len(py) != len(sql):
            print(f"  rows: py {len(py)} sql {len(sql)}")

    dump = os.path.join(tmp, "dump.sql")
    open(dump, "w").write("SELECT account_id, assigned_to, assigned_on, policy_version, COALESCE(outcome,'') "
                          "FROM assignments ORDER BY 1,3,2,4,5;")
    os.chmod(dump, 0o644)
    sql_asg = [tuple(l.split("\t")) for l in psql(a.psql, dump).splitlines() if l]
    py_rows = sorted((tuple(r[c] for c in ("account_id", "assigned_to", "assigned_on", "policy_version", "outcome"))
                      for r in csv.DictReader(open(py_asg))), key=lambda t: (t[0], t[2], t[1], t[3], t[4]))
    if py_rows != sql_asg:
        ok = False
        print("ASSIGNMENTS MISMATCH", len(py_rows), len(sql_asg))
        for x in set(py_rows) ^ set(sql_asg):
            print("  ", "py " if x in py_rows else "sql", x)

    # no recorded outcome may disappear
    before = {(r["account_id"], r["assigned_on"], r["outcome"]) for r in csv.DictReader(open(files["assignments.csv"]))
              if r["outcome"]}
    after = {(r[0], r[2], r[4]) for r in py_rows if r[4]}
    lost = before - after
    if lost:
        ok = False
        print("OUTCOMES LOST:", sorted(lost))

    blocks = sum(1 for r in py if r[3])
    print(f"{'PASS' if ok else 'FAIL'} [{a.policy.split('/')[-1]} @ {a.as_of}]: {len(py)} ranked, "
          f"{sum(1 for r in py if r[2])} routed, {blocks} blocked, {len(py_rows)} assignment rows")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
