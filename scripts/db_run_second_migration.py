#!/usr/bin/env python3
import os
import sys
from pathlib import Path

try:
    from dotenv import load_dotenv
except Exception:
    load_dotenv = None

try:
    import psycopg2
except Exception:
    psycopg2 = None


def main():
    if load_dotenv:
        load_dotenv()

    database_url = os.getenv("DATABASE_URL")
    if not database_url:
        print("ERROR: DATABASE_URL not found in environment. Set it in .env or export it.")
        sys.exit(1)

    if psycopg2 is None:
        print("ERROR: psycopg2 not installed. Run: pip install psycopg2-binary python-dotenv")
        sys.exit(1)

    migration_path = Path(__file__).parent.parent / "migrations" / "002_create_commands_table.sql"
    if not migration_path.exists():
        print("ERROR: migration file not found:", migration_path)
        sys.exit(1)

    try:
        conn = psycopg2.connect(database_url)
        conn.autocommit = True
        with conn.cursor() as cur:
            print("Applying", migration_path.name)
            cur.execute(migration_path.read_text())
        conn.close()
        print("Second migration applied successfully.")
    except Exception as exc:
        print("Migration failed:", exc)
        sys.exit(1)


if __name__ == "__main__":
    main()
