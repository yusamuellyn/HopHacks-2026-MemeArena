"""Live ingester: polls X (Twitter) for fresh meme mentions via twscrape.

Runs forever, sweeping every meme in MEME_SEARCH_TERMS and inserting the
newest matching tweets into meme_mentions in Tiger. Battles read that table
with time decay, so fresh rows dominate the score.

Needs in .env (use a throwaway account):
  X_USERNAME  account handle
  X_COOKIES   "auth_token=...; ct0=..." copied from a logged-in browser
twscrape stores the account in x_accounts.db and handles rate limits itself.
"""
import asyncio
import os
import time
from pathlib import Path

import psycopg2
from dotenv import load_dotenv
from twscrape import API

from search_terms import MEME_SEARCH_TERMS

BASE = Path(__file__).resolve().parent
load_dotenv(BASE / ".env")

ACCOUNTS_DB = BASE / "x_accounts.db"
PER_MEME = 20      # newest tweets to fetch per meme per sweep
SEARCH_DELAY = 20  # seconds between searches; X allows ~50 searches / 15 min


def get_conn():
    conn = psycopg2.connect(
        host=os.environ["TIGER_HOST"],
        port=os.environ["TIGER_PORT"],
        dbname=os.environ["TIGER_DB"],
        user=os.environ["TIGER_USER"],
        password=os.environ["TIGER_PASSWORD"],
        sslmode="require",
    )
    conn.autocommit = True
    return conn


async def get_api():
    api = API(str(ACCOUNTS_DB))
    username = os.environ["X_USERNAME"]
    await api.pool.delete_accounts(username)
    await api.pool.add_account(
        username,
        password=os.environ.get("X_PASSWORD", ""),
        email=os.environ.get("X_EMAIL", ""),
        email_password="",
        cookies=os.environ["X_COOKIES"],
    )
    return api


def build_query(terms):
    return "(" + " OR ".join(f'"{t}"' for t in terms) + ") -filter:retweets"


async def sweep(api, cur):
    for meme_id, terms in MEME_SEARCH_TERMS.items():
        new = 0
        async for t in api.search(build_query(terms), limit=PER_MEME, kv={"product": "Latest"}):
            cur.execute(
                """
                INSERT INTO meme_mentions (meme_id, tweet_id, created_at, body)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT DO NOTHING
                """,
                (meme_id, t.id_str, t.date, t.rawContent),
            )
            new += cur.rowcount
        print(f"{meme_id:24s} +{new}")
        await asyncio.sleep(SEARCH_DELAY)


async def main():
    api = await get_api()
    conn = get_conn()
    cur = conn.cursor()
    while True:
        start = time.time()
        await sweep(api, cur)
        print(f"Sweep done in {time.time() - start:.0f}s")


if __name__ == "__main__":
    asyncio.run(main())
