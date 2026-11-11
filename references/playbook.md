# The playbook behind GTM Cook

Seven steps, in the order I'd build them today. Each one comes with the mistake it prevents. The order is enforced because each step's mistakes are invisible from the next one: a bad catalog looks like a bad weight, a dirty domain field looks like a quiet account.

## 1. Know the buying moment and who you never call
Customers, blocked accounts, accounts without a domain, and a real ICP filter (size, industry, country). The easy path is to exclude and let the score sort the rest; then a strong signal on an account you'd never close lands at the top.

## 2. Pick signals from the buying moment, not from the data you have
Funding, leadership change, a champion moving, a hiring surge, expansion, and at least one negative signal (a recent lost deal). For each: why it precedes the buying moment, and which tool sees it. A signal nobody can source stays in the catalog as a known gap, not as a fake column.

## 3. Build the intake before the model
Raw exports in, two clean tables out, through a mapping you can rerun. Match on ids or clean domains, and look at what didn't match: unmatched rows are usually the CRM's domain field, not missing signals. Most "the score is wrong" bugs live here.

## 4. Score: latest signal per type x weight x decay x size, then caps
- Latest per type, so ten website visits don't count ten times.
- Decay by age: a funding round from last week and one from eight months ago are not the same call.
- Size for valued signals: 4 open roles and 40 are not the same surge.
- Caps per source and per family on positive points, so one noisy source can't hijack the ranking. Negatives always apply.
- Version the policy and write down why each version changed. Check where the points come from: often one source carries almost everything, and the custom scrapers you spent weeks on barely move who gets called.

## 5. Route callable accounts, and remember who got what
Fixed number of accounts per SDR per day, by rank, in snake order. Gate on what makes a call possible (a contact with an email, not owned by an AE, a minimum score, no weekends) and keep assigned accounts out for a cooldown, or SDRs get the same list every morning.

## 6. Ship it with an owner for outcomes
The nightly job is the easy part. The hard part is a named person writing outcomes back. Without outcomes, weights stay guesses forever.

## 7. Close the loop
Feedback from SDRs tells you what looks wrong; outcomes tell you what is wrong, once there is enough volume (n = 16·p·(1−p)/d² accounts per group). Below that, say "hunch" out loud. And size refreshes to the list: accounts in scope ÷ accounts refreshed per day = days between refreshes. If that's longer than your signals' half-life, you're scoring old news.
