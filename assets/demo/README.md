# Demo data (fictional)

Everything here is invented: the company (Ledgerline, spend management software), its 41 prospect accounts, their domains (all end in `.example`) and every signal. Use it to try the seven steps before exporting real data. Never mix it with real data: a real project starts in a new folder.

## Files

- `project.json`: step 1 answers for Ledgerline
- `signal_catalog.json`: step 2 catalog (one signal deliberately `not_sourced`: tech_change)
- `raw/`: five exports as a CRM, a job board, an enrichment tool and web analytics would produce them
- `mappings/`: how each export becomes accounts and signals (step 3)
- `policy_v1.json`: a starting scoring and routing policy (steps 4-5). Weights are uncalibrated assumptions.

## Planted anomalies

Real exports are dirty. These five rows are dirty on purpose, so you can see the intake catch them instead of guessing:

| file | row | what the intake does |
|---|---|---|
| raw/job_postings.csv | a posting from `unknownco.example` | unmatched: the company is not in the CRM |
| raw/job_postings.csv | Saffron Commerce, date `31/12/2026x` | skipped: unreadable date |
| raw/enrichment_events.csv, raw/web_analytics.csv | 4 rows for Granite Security and Willow Labs | unmatched: these accounts have no domain in the CRM, so a domain match is impossible. In a real project the fix is in the CRM, never a guessed match |

There are no recorded outcomes in the demo: step 7 (calibration) needs real results from real calls.
