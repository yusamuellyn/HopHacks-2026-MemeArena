"""One-shot build of the meme_mentions table.

Scans the full tweets table ONCE for all memes in MEME_SEARCH_TERMS and
stores every matching tweet in meme_mentions. Battle queries then read the
small precomputed table instead of ILIKE-scanning 29M rows per request.

Re-run this after loading more parquet files into tweets (it rebuilds
from scratch, so it never double-counts).
"""
import argparse
import os
import time
from pathlib import Path
import psycopg2
from dotenv import load_dotenv
from search_terms import MEME_SEARCH_TERMS

load_dotenv(Path(__file__).resolve().parent / ".env")


def get_conn():
    return psycopg2.connect(
        host=os.environ["TIGER_HOST"],
        port=os.environ["TIGER_PORT"],
        dbname=os.environ["TIGER_DB"],
        user=os.environ["TIGER_USER"],
        password=os.environ["TIGER_PASSWORD"],
        sslmode="require",
    )


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--missing",
        action="store_true",
        help="Index only meme ids that are not already in meme_mentions.",
    )
    args = parser.parse_args()

    conn = get_conn()
    conn.autocommit = True
    cur = conn.cursor()
    cur.execute("SET statement_timeout = 0")

    cur.execute("""
        CREATE TABLE IF NOT EXISTS meme_mentions (
            meme_id    text NOT NULL,
            tweet_id   text NOT NULL,
            created_at timestamptz,
            body       text,
            PRIMARY KEY (meme_id, tweet_id)
        )
    """)
    cur.execute("""
        CREATE INDEX IF NOT EXISTS meme_mentions_meme_time_idx
        ON meme_mentions (meme_id, created_at)
    """)

    terms = MEME_SEARCH_TERMS
    if args.missing:
        cur.execute("SELECT DISTINCT meme_id FROM meme_mentions")
        have = {row[0] for row in cur.fetchall()}
        terms = {meme_id: pats for meme_id, pats in MEME_SEARCH_TERMS.items() if meme_id not in have}
        if not terms:
            print("All memes already indexed.")
            cur.close()
            conn.close()
            return
        print(f"Indexing {len(terms)} missing memes (keeping existing rows).")
    else:
        print("Rebuilding meme_mentions from scratch.")
        cur.execute("TRUNCATE meme_mentions")

    # (meme_id, %pattern%) pairs for every search term of every meme
    pairs = [
        (meme_id, f"%{term}%")
        for meme_id, pats in terms.items()
        for term in pats
    ]
    values_sql = ", ".join(["(%s, %s)"] * len(pairs))
    params = [x for pair in pairs for x in pair]

    print(f"Scanning tweets for {len(pairs)} search patterns "
          f"across {len(terms)} memes...")
    print("This does one full scan of the tweets table; expect several minutes.")

    start = time.time()
    # GROUP BY dedupes engagement-snapshot rows (same tweet id appears
    # multiple times in tweets) and tweets matching several terms of a meme.
    cur.execute(f"""
        INSERT INTO meme_mentions (meme_id, tweet_id, created_at, body)
        SELECT tm.meme_id, t.id, min(t.created_at), min(t.body)
        FROM tweets t
        JOIN (VALUES {values_sql}) AS tm (meme_id, pattern)
          ON t.body ILIKE tm.pattern
        WHERE t.created_at >= date_trunc('month', CURRENT_DATE) - interval '1 month'
          AND t.created_at <  date_trunc('month', CURRENT_DATE)
        GROUP BY tm.meme_id, t.id
        ON CONFLICT DO NOTHING
    """, params)
    print(f"Done in {time.time() - start:.0f}s")

    cur.execute("""
        SELECT meme_id, count(*) FROM meme_mentions
        GROUP BY meme_id ORDER BY count(*) DESC
    """)
    print("\nMentions per meme:")
    for meme_id, n in cur.fetchall():
        print(f"  {meme_id:24s} {n:>8,}")

    cur.close()
    conn.close()


if __name__ == "__main__":
    main()
