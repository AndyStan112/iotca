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
    # load .env if available
    if load_dotenv:
        load_dotenv()

    DATABASE_URL = os.getenv('DATABASE_URL')
    if not DATABASE_URL:
        print('ERROR: DATABASE_URL not found in environment. Set it in .env or export it.')
        sys.exit(1)

    if psycopg2 is None:
        print('ERROR: psycopg2 not installed. Run: pip install psycopg2-binary python-dotenv')
        sys.exit(1)

    migrations_dir = Path(__file__).parent.parent / 'migrations'
    sql_paths = sorted(migrations_dir.glob('*.sql'))
    if not sql_paths:
        print('ERROR: no migration SQL files found in', migrations_dir)
        sys.exit(1)

    try:
        conn = psycopg2.connect(DATABASE_URL)
        conn.autocommit = True
        with conn.cursor() as cur:
            for sql_path in sql_paths:
                print('Applying', sql_path.name)
                sql = sql_path.read_text()
                cur.execute(sql)
        conn.close()
        print('Migrations applied successfully.')
    except Exception as e:
        print('Migration failed:', e)
        sys.exit(1)


if __name__ == '__main__':
    main()
