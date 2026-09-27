#!/usr/bin/env python3
"""migrate.py — ledger-based forward-only DB migration runner for LLM Manager.

Ledger table: schema_migrations(version TEXT PK, checksum TEXT, applied_at TIMESTAMPTZ, git_sha TEXT).

Usage:
  migrate.py status                     # show applied + pending
  migrate.py apply  [--git-sha SHA]     # apply pending migrations in order
  migrate.py verify                     # verify checksums of applied migrations

Rules (plan §8):
- forward-only; destructive migrations are NEVER auto-applied (file must be
  named NNN_destructive_*.sql and the runner refuses them outside --i-know-this-is-destructive)
- never uploads/backups anywhere: backups are an operator action pre-migration
"""
import hashlib
import os
import sys

import psycopg2

MIGRATIONS_DIR = os.path.dirname(os.path.abspath(__file__))

# Connection inputs come from env only — never from code or files committed here.
# LLM_MANAGER_PG_DSN      full DSN, OR the four vars below
# LLM_MANAGER_PG_HOST / _DB / _USER / _PASSWORD


def connect():
    dsn = os.environ.get("LLM_MANAGER_PG_DSN")
    if dsn:
        return psycopg2.connect(dsn, connect_timeout=5)
    host = os.environ.get("LLM_MANAGER_PG_HOST", "127.0.0.1")
    db = os.environ.get("LLM_MANAGER_PG_DB", "llmmanager")
    user = os.environ.get("LLM_MANAGER_PG_USER", "llmmanager")
    pw = os.environ.get("LLM_MANAGER_PG_PASSWORD")
    if pw is None:
        # Same credential contract as the app (main.py _load_pg_pw): the
        # PG_PW= line inside /etc/llm-manager/secrets/pg_app_creds. Keeping
        # one source of truth means the transaction needs no duplicated
        # secret env plumbing (plan §8).
        pw_file = os.environ.get(
            "LLM_MANAGER_PG_PW_FILE", "/etc/llm-manager/secrets/pg_app_creds")
        try:
            with open(pw_file) as f:
                for line in f:
                    if line.startswith("PG_PW="):
                        pw = line.strip().split("=", 1)[1]
                        break
        except OSError:
            pw = None
    return psycopg2.connect(host=host, dbname=db, user=user, password=pw or "", connect_timeout=5)


def ensure_ledger(cur):
    cur.execute("""
        CREATE TABLE IF NOT EXISTS schema_migrations (
            version TEXT PRIMARY KEY,
            checksum TEXT NOT NULL,
            applied_at TIMESTAMPTZ DEFAULT now(),
            git_sha TEXT
        )""")


def local_migrations():
    out = []
    if not os.path.isdir(MIGRATIONS_DIR):
        return out
    for name in sorted(os.listdir(MIGRATIONS_DIR)):
        if not name.endswith(".sql"):
            continue
        path = os.path.join(MIGRATIONS_DIR, name)
        version = name.split("_", 1)[0]
        checksum = hashlib.sha256(open(path, "rb").read()).hexdigest()
        destructive = "_destructive_" in name
        out.append({"version": version, "name": name, "path": path,
                    "checksum": checksum, "destructive": destructive})
    return out


def applied_versions(cur):
    cur.execute("SELECT version, checksum FROM schema_migrations ORDER BY version")
    return dict(cur.fetchall())


def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else "status"
    git_sha = None
    if "--git-sha" in sys.argv:
        git_sha = sys.argv[sys.argv.index("--git-sha") + 1]
    force_destructive = "--i-know-this-is-destructive" in sys.argv

    conn = connect()
    try:
        with conn.cursor() as cur:
            ensure_ledger(cur)
            conn.commit()
            applied = applied_versions(cur)
            migrations = local_migrations()

            if cmd == "status":
                for m in migrations:
                    state = "applied" if m["version"] in applied else \
                            ("PENDING-DESTRUCTIVE" if m["destructive"] else "PENDING")
                    print(f"{m['version']}  {state:<20} {m['name']}")
                return 0

            if cmd == "verify":
                bad = []
                for m in migrations:
                    if m["version"] in applied and applied[m["version"]] != m["checksum"]:
                        bad.append(m["version"])
                if bad:
                    print(f"FATAL: checksum drift for applied migrations: {bad}")
                    return 1
                print(f"verified {len([m for m in migrations if m['version'] in applied])} applied migrations")
                return 0

            if cmd == "apply":
                failed = False
                for m in migrations:
                    if m["version"] in applied:
                        continue
                    if m["destructive"] and not force_destructive:
                        print(f"SKIP {m['version']}: destructive migration requires "
                              f"maintenance plan + --i-know-this-is-destructive")
                        continue
                    sql = open(m["path"]).read()
                    try:
                        cur.execute(sql)
                        cur.execute(
                            "INSERT INTO schema_migrations (version, checksum, git_sha) VALUES (%s,%s,%s)",
                            (m["version"], m["checksum"], git_sha))
                        conn.commit()
                        print(f"applied {m['version']} ({m['name']})")
                    except Exception as e:
                        conn.rollback()
                        print(f"FATAL: {m['version']} failed: {e.__class__.__name__}: {e}")
                        failed = True
                        break
                return 1 if failed else 0

            print(f"unknown command {cmd}")
            return 2
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
