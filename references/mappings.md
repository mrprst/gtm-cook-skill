# Mapping files (scripts/ingest.py)

A mapping tells `ingest.py` how to read one raw export. You write it with the user after looking at the export's columns. Column names are the export's own, exactly as in its header.

## Accounts mapping

```json
{
  "columns": {"account_id": "Account ID", "account_name": "Account Name", "domain": "Website",
              "employees": "Employees", "industry": "Industry", "country": "Billing Country"},
  "flags": {
    "is_customer":       [{"column": "Type", "op": "eq", "value": "Customer"}],
    "is_blocked":        [{"column": "Do Not Call", "op": "eq", "value": "Yes"}],
    "has_contact_email": [{"column": "Contacts With Email", "op": "gte", "value": 1}],
    "owned_by_ae":       [{"column": "Account Owner Role", "op": "eq", "value": "AE"}]
  }
}
```

- `columns`: output column ← export column. `account_id` and `account_name` are required. `domain` is normalized (lowercase, no `https://`, no `www.`, no path).
- `flags`: true/false columns computed from conditions (all must hold). Use them for exclusions and routing gates.
- Duplicate `account_id` stops the run: deduplicate in the CRM.

## Signals mapping

```json
{
  "source": "job_board",
  "match": {"column": "company_url", "on": "domain"},
  "date": {"column": "posted_on", "formats": ["%d/%m/%Y"]},
  "rules": [
    {"signal_type": "hiring_surge", "aggregate": {"window_days": 30, "min_count": 4}},
    {"signal_type": "finance_role_posted",
     "when": [{"column": "job_title", "op": "in", "value": ["Controller", "FP&A Manager"]}]},
    {"signal_type": "funding_round", "when": [{"column": "event", "op": "eq", "value": "funding"}],
     "value_column": "amount_usd"}
  ]
}
```

- `source`: name stored in `signal_source`. Re-ingesting replaces all rows of this source and keeps the others.
- `match.on`: `account_id` (the export carries the CRM id) or `domain`. A domain shared by several accounts is never guessed: those rows go to `data/unmatched_<source>.csv` with the unmatched ones.
- `date.formats`: Python date formats tried in order (`%Y-%m-%d`, `%d/%m/%Y`, `%m/%d/%Y`...). Unreadable dates are counted and skipped.
- `rules`: each export row can produce several signals. `when` is a list of conditions, all must hold (no `when` = every row).
- `value_column`: stores a number as `signal_value` (valued signals).
- `aggregate`: counts matching rows per account over a trailing window. A signal is written on each date where the count reaches `min_count`, with the count as `signal_value`. Scoring keeps the latest.

## Operators

`eq`, `ne`, `contains`, `not_contains` (text, case-insensitive) · `in`, `not_in` (value is a list) · `gt`, `gte`, `lt`, `lte` (numbers; non-numbers never match) · `not_empty`, `empty` (no value needed).

## Units

A valued signal fed by two sources must mean the same thing in both (the catalog's `value_unit`). "Open roles listed today" from one tool and "postings in 30 days" from another are two signal types, not one.
