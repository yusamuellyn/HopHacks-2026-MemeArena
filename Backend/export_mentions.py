import os
import sys
import json
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

def main(left_id, right_id):
    if left_id not in MEME_SEARCH_TERMS or right_id not in MEME_SEARCH_TERMS:
        raise ValueError(f"Unknown meme id(s): {left_id}, {right_id}")

    conn = get_conn()
    cur = conn.cursor()

    cur.execute("""
        SELECT meme_id, count(*)
        FROM meme_mentions
        WHERE meme_id IN (%s, %s)
        GROUP BY meme_id
    """, [left_id, right_id])

    counts = dict(cur.fetchall())
    cur.close()
    conn.close()

    result = {
        "leftId": left_id,
        "rightId": right_id,
        "leftTotal": counts.get(left_id, 0),
        "rightTotal": counts.get(right_id, 0),
    }

    out_path = f"mentions_{left_id}_vs_{right_id}.json"
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(result, f, indent=2)

    print(f"Wrote totals to {out_path}: {result}")

if __name__ == "__main__":
    if len(sys.argv) != 3:
        print("Usage: python export_mentions.py <left_id> <right_id>")
        sys.exit(1)
    main(sys.argv[1], sys.argv[2])