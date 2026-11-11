-- GTM Cook production tables. The signal engine writes accounts and signals; scoring.sql reads them
-- and writes assignments. Add one boolean column per routing gate (routing.require_true / exclude_if_true).
CREATE TABLE IF NOT EXISTS accounts (
  account_id        text PRIMARY KEY,
  account_name      text NOT NULL,
  domain            text,
  employees         int,
  industry          text,
  country           text,
  is_customer       boolean DEFAULT FALSE,
  is_blocked        boolean DEFAULT FALSE,
  has_contact_email boolean DEFAULT FALSE,  -- example gate: refreshed by your contact pipeline
  owned_by_ae       boolean DEFAULT FALSE   -- example gate: synced from the CRM
);

CREATE TABLE IF NOT EXISTS signals (
  account_id    text NOT NULL REFERENCES accounts(account_id),
  signal_type   text NOT NULL,
  signal_source text NOT NULL,
  signal_date   date NOT NULL,
  signal_value  numeric,                 -- only for valued signals (e.g. open roles count)
  ingested_at   timestamptz DEFAULT now() -- when the engine last saw it: use it to check source freshness
);
CREATE INDEX IF NOT EXISTS signals_account_type_date ON signals (account_id, signal_type, signal_date DESC);

CREATE TABLE IF NOT EXISTS assignments (
  account_id     text NOT NULL,
  assigned_to    text NOT NULL,
  assigned_on    date NOT NULL,
  policy_version text NOT NULL,
  outcome        text
);
CREATE INDEX IF NOT EXISTS assignments_on ON assignments (assigned_on);
