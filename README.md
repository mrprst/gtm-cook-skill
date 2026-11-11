# GTM Cook

A Claude skill that walks a GTM, RevOps or sales person through building their own signal engine: buying moment and ICP, signal catalog, intake of raw exports, scoring, daily routing of accounts to SDRs, a nightly Postgres job, and calibration against outcomes. Seven steps, each locked until the previous one is approved.

Claude designs with you. Deterministic Python scripts (stdlib only) do every computation. The generated SQL reproduces the scripts exactly.

## Install (Claude Code)

```bash
git clone <this-repo> ~/.claude/skills/gtm-cook
```

Or copy the `gtm-cook` folder into `.claude/skills/` inside a project.

## Use

"Help me build a signal engine to tell my SDRs which accounts to call." The first thing it asks: your own data, or a demo with fictional data.

## Files

- `SKILL.md`: the gated workflow
- `scripts/check.py`: step gate (status, check, approve)
- `scripts/ingest.py`: raw exports to `accounts.csv` / `signals.csv` through mapping files
- `scripts/score.py`: scoring, routing, assignment memory, report
- `scripts/to_sql.py`: policy to a Postgres transaction (same results as score.py --commit)
- `scripts/calibration.py`: is there enough outcome volume to calibrate each weight
- `scripts/common.py`: shared helpers
- `assets/schema.sql`: production tables
- `assets/demo/`: a fictional company to try the seven steps (exports, mappings, catalog, policy, planted anomalies listed in its README)
- `references/`: playbook, mapping format, common signals
- `tests/parity.py` + `tests/fixtures/`: Python vs SQL on deliberately tricky data (ties, missing values, same-day outcomes). Not demo data. Needs a scratch Postgres

## What it does not do

No provider API calls, scraping, email finding, CRM writes or sequences. It tells you what to pull and how often, then takes it from there.
