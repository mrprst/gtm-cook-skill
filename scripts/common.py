"""Shared helpers for GTM Cook scripts. Stdlib only."""
import csv, json, os, re, sys
from datetime import datetime

TRUE = {"1", "true", "yes", "y", "t"}
ACCOUNT_REQUIRED = ["account_id", "account_name"]
SIGNAL_REQUIRED = ["account_id", "signal_type", "signal_source", "signal_date"]
ASSIGN_COLS = ["account_id", "assigned_to", "assigned_on", "policy_version", "outcome"]
IDENT = re.compile(r"^[a-z_][a-z0-9_]*$")


def die(msg):
    sys.exit(f"ERROR: {msg}")


def parse_date(s, fmts=("%Y-%m-%d",)):
    s = (s or "").strip()
    if not s:
        return None
    for f in fmts:
        try:
            return datetime.strptime(s if f != "%Y-%m-%d" else s[:10], f).date()
        except ValueError:
            continue
    return None


def as_bool(v):
    return str(v or "").strip().lower() in TRUE


def as_num(v):
    try:
        return float(str(v).replace(",", "").strip())
    except (ValueError, TypeError):
        return None


def read_csv(path, required=(), what="file"):
    if not os.path.exists(path):
        die(f"{what} not found: {path}")
    with open(path, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        cols = [(c or "").strip() for c in (reader.fieldnames or [])]
        missing = [c for c in required if c not in cols]
        if missing:
            die(f"{what} {path} is missing columns: {', '.join(missing)}. Found: {', '.join(cols)}")
        rows = [{(k or "").strip(): (v or "").strip() for k, v in r.items() if k} for r in reader]
    return cols, rows


def excel_safe(v):
    """Text a spreadsheet would run as a formula (=, +, -, @, tab, CR first) gets a leading quote.
    Numbers are left alone. Used only for files people open, never for data the scripts read back."""
    s = "" if v is None else str(v)
    if s and s[0] in "=+-@\t\r" and as_num(s) is None:
        return "'" + s
    return v


def write_csv(path, cols, rows, for_humans=False):
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    if for_humans:
        rows = [{k: excel_safe(v) for k, v in r.items()} for r in rows]
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)


def normalize_domain(v):
    d = (v or "").strip().lower()
    d = re.sub(r"^[a-z]+://", "", d)
    d = d.split("/")[0].split("?")[0].split("#")[0]
    if d.startswith("www."):
        d = d[4:]
    return d


def load_policy(path):
    with open(path, encoding="utf-8") as f:
        pol = json.load(f)
    for key in ("version", "notes"):
        if not str(pol.get(key) or "").strip():
            die(f"the policy needs a non-empty '{key}'. "
                "'notes' must say what changed since the previous version and why, from which feedback.")
    if not pol.get("signals"):
        die("the policy has no signals. Add at least one signal type with a weight.")
    for t, c in pol["signals"].items():
        if "weight" not in c:
            die(f"signal '{t}' has no weight in the policy")
        vs = c.get("value_scale")
        if vs is not None and (as_num(vs.get("min")) is None or as_num(vs.get("max")) is None
                               or float(vs["max"]) <= float(vs["min"])):
            die(f"signal '{t}': value_scale needs numeric min < max")
    r = pol.get("routing", {})
    for key in ("require_true", "exclude_if_true"):
        for col in r.get(key, []) or []:
            if not IDENT.match(col):
                die(f"routing.{key}: '{col}' must be a lowercase column name (letters, digits, _)")
    for d in r.get("skip_weekdays", []) or []:
        if d not in range(1, 8):
            die("routing.skip_weekdays uses ISO days: 1 = Monday ... 7 = Sunday")
    return pol


def scoring_hash(pol):
    """Fingerprint of everything that changes scores (routing excluded): a version must not change once run."""
    import hashlib
    body = {k: v for k, v in pol.items() if k not in ("routing", "notes")}
    return hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()[:16]
