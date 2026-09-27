# db/migrations — LLM Manager database migration ledger

Numbered, forward-only SQL migrations with a ledger table (`schema_migrations`:
version, checksum, applied_at, git_sha).

- `schema.sql` stays the clean-install reference (all 33 tables), NOT an in-place tool.
- `migrate.py apply` applies pending migrations in order and records the ledger.
- Files named `NNN_destructive_*.sql` are refused by ordinary runs — they require a
  maintenance plan, an explicit `--i-know-this-is-destructive`, an operator-taken backup,
  and a documented recovery path (plan §8).
- Before ANY production migration the operator takes a protected local DB backup.
  Production DB dumps never leave the host and never enter GitHub artifacts.
- Rollback compatibility: application rollback assumes backward-compatible
  (expand-only) migrations; destructive ones block ordinary CD rollback by policy.
