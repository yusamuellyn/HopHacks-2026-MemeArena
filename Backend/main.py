import os
from datetime import date, timedelta
from contextlib import contextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response
from pydantic import BaseModel
import psycopg2
from dotenv import load_dotenv
from elevenlabs.client import ElevenLabs

from search_terms import MEME_SEARCH_TERMS

from google import genai

load_dotenv(Path(__file__).resolve().parent / ".env")

gemini_client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])


import base64

class MemeHistoryRequest(BaseModel):
    memeId: str
  





app = FastAPI()
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

elevenlabs_client = ElevenLabs(api_key=os.environ["ELEVENLABS_API_KEY"])
VOICE_ID = os.environ["ELEVENLABS_VOICE_ID"]

DECAY_LAMBDA = 0.00002 # tune this: higher = more weight on recent mentions


class BattleRequest(BaseModel):
    leftId: str
    rightId: str


class RecordBattleRequest(BaseModel):
    leftId: str
    rightId: str
    winnerId: str
    leftTotal: int
    rightTotal: int
    winnerShare: float


class PickRequest(BaseModel):
    memeName: str


class StartRequest(BaseModel):
    leftName: str
    rightName: str


class WinnerRequest(BaseModel):
    winnerName: str
    pct: int


@contextmanager
def db():
    conn = psycopg2.connect(
        host=os.environ["TIGER_HOST"],
        port=os.environ["TIGER_PORT"],
        dbname=os.environ["TIGER_DB"],
        user=os.environ["TIGER_USER"],
        password=os.environ["TIGER_PASSWORD"],
        sslmode="require",
    )
    try:
        yield conn
    finally:
        conn.close()


def ensure_tables(conn):
    cur = conn.cursor()
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS battle_records (
            id serial PRIMARY KEY,
            left_id text NOT NULL,
            right_id text NOT NULL,
            winner_id text NOT NULL,
            left_total integer NOT NULL,
            right_total integer NOT NULL,
            winner_share double precision NOT NULL,
            created_at timestamptz NOT NULL DEFAULT now()
        )
        """
    )
    cur.execute(
        """
        CREATE INDEX IF NOT EXISTS battle_records_created_idx
        ON battle_records (created_at DESC)
        """
    )
    conn.commit()
    cur.close()


@app.on_event("startup")
def startup():
    with db() as conn:
        ensure_tables(conn)


def require_meme(meme_id: str) -> str:
    if meme_id not in MEME_SEARCH_TERMS:
        raise HTTPException(status_code=400, detail=f"Unknown meme id: {meme_id}")
    return meme_id


def mention_window(cur):
    cur.execute("SELECT max(created_at)::date FROM meme_mentions")
    latest = cur.fetchone()[0]
    if latest is None:
        return None, None
    return latest - timedelta(days=1), latest


def fetch_mention_stats(cur, meme_ids):
    yesterday, latest = mention_window(cur)
    stats = {
        meme_id: {"yesterdayMentions": 0, "latestMentions": 0, "totalMentions": 0}
        for meme_id in meme_ids
    }
    if not meme_ids:
        return stats, yesterday, latest
    cur.execute(
        """
        WITH bounds AS (
            SELECT max(created_at)::date AS latest FROM meme_mentions
        )
        SELECT
            meme_id,
            count(*) FILTER (
                WHERE created_at::date = (SELECT latest FROM bounds) - 1
            ) AS yesterday,
            count(*) FILTER (
                WHERE created_at::date = (SELECT latest FROM bounds)
            ) AS latest,
            count(*) AS total
        FROM meme_mentions
        WHERE meme_id = ANY(%s)
        GROUP BY meme_id
        """,
        [list(meme_ids)],
    )
    for meme_id, yday, latest_count, total in cur.fetchall():
        stats[meme_id] = {
            "yesterdayMentions": int(yday or 0),
            "latestMentions": int(latest_count or 0),
            "totalMentions": int(total or 0),
        }
    return stats, yesterday, latest


def fetch_records(cur):
    cur.execute("SELECT count(*) FROM battle_records")
    battles = int(cur.fetchone()[0])
    cur.execute(
        """
        SELECT winner_id
        FROM battle_records
        ORDER BY created_at DESC, id DESC
        LIMIT 1
        """
    )
    last_row = cur.fetchone()
    last_champion = last_row[0] if last_row else None

    fighters = {
        meme_id: {"wins": 0, "losses": 0, "lastShare": None}
        for meme_id in MEME_SEARCH_TERMS
    }
    cur.execute(
        """
        SELECT
            meme_id,
            sum(wins)::int AS wins,
            sum(losses)::int AS losses
        FROM (
            SELECT
                left_id AS meme_id,
                CASE WHEN winner_id = left_id THEN 1 ELSE 0 END AS wins,
                CASE WHEN winner_id = left_id THEN 0 ELSE 1 END AS losses
            FROM battle_records
            UNION ALL
            SELECT
                right_id,
                CASE WHEN winner_id = right_id THEN 1 ELSE 0 END,
                CASE WHEN winner_id = right_id THEN 0 ELSE 1 END
            FROM battle_records
        ) scored
        GROUP BY meme_id
        """
    )
    for meme_id, wins, losses in cur.fetchall():
        if meme_id not in fighters:
            fighters[meme_id] = {"wins": 0, "losses": 0, "lastShare": None}
        fighters[meme_id]["wins"] = int(wins or 0)
        fighters[meme_id]["losses"] = int(losses or 0)

    cur.execute(
        """
        SELECT meme_id, share
        FROM (
            SELECT
                meme_id,
                share,
                row_number() OVER (
                    PARTITION BY meme_id ORDER BY created_at DESC, id DESC
                ) AS rn
            FROM (
                SELECT
                    left_id AS meme_id,
                    CASE WHEN winner_id = left_id THEN winner_share ELSE 100 - winner_share END AS share,
                    created_at,
                    id
                FROM battle_records
                UNION ALL
                SELECT
                    right_id,
                    CASE WHEN winner_id = right_id THEN winner_share ELSE 100 - winner_share END,
                    created_at,
                    id
                FROM battle_records
            ) all_fights
        ) ranked
        WHERE rn = 1
        """
    )
    for meme_id, share in cur.fetchall():
        if meme_id in fighters:
            fighters[meme_id]["lastShare"] = float(share)

    today = date.today().isoformat()
    cur.execute(
        """
        SELECT winner_id, count(*)
        FROM battle_records
        WHERE created_at::date = CURRENT_DATE
        GROUP BY winner_id
        """
    )
    daily_wins = {meme_id: int(n) for meme_id, n in cur.fetchall()}
    cur.execute(
        "SELECT count(*) FROM battle_records WHERE created_at::date = CURRENT_DATE"
    )
    daily_battles = int(cur.fetchone()[0])

    cur.execute(
        """
        SELECT id, left_id, right_id, winner_id, left_total, right_total, winner_share, created_at
        FROM battle_records
        ORDER BY created_at DESC, id DESC
        LIMIT 12
        """
    )
    recent = [
        {
            "id": row[0],
            "leftId": row[1],
            "rightId": row[2],
            "winnerId": row[3],
            "leftTotal": int(row[4]),
            "rightTotal": int(row[5]),
            "winnerShare": float(row[6]),
            "createdAt": row[7].isoformat() if row[7] else None,
        }
        for row in cur.fetchall()
    ]

    return {
        "battles": battles,
        "lastChampionId": last_champion,
        "fighters": fighters,
        "daily": {"date": today, "wins": daily_wins, "battles": daily_battles},
        "recent": recent,
    }


def build_stats_payload(cur):
    mention_stats, yesterday, latest = fetch_mention_stats(cur, list(MEME_SEARCH_TERMS))
    records = fetch_records(cur)
    fighters = {}
    max_yesterday = 0
    for meme_id in MEME_SEARCH_TERMS:
        mentions = mention_stats.get(meme_id, {})
        record = records["fighters"].get(meme_id, {"wins": 0, "losses": 0, "lastShare": None})
        yday = int(mentions.get("yesterdayMentions") or 0)
        max_yesterday = max(max_yesterday, yday)
        fighters[meme_id] = {
            **record,
            "yesterdayMentions": yday,
            "latestMentions": int(mentions.get("latestMentions") or 0),
            "totalMentions": int(mentions.get("totalMentions") or 0),
        }
    return {
        "battles": records["battles"],
        "lastChampionId": records["lastChampionId"],
        "fighters": fighters,
        "daily": records["daily"],
        "recent": records["recent"],
        "window": {
            "yesterday": yesterday.isoformat() if yesterday else None,
            "latest": latest.isoformat() if latest else None,
        },
        "maxYesterday": max_yesterday,
    }



@app.post("/api/meme-history")
def meme_history(payload: MemeHistoryRequest):
    meme_name = payload.memeId.replace('-', ' ')
    history_prompt = (
        f"You are an internet culture historian. In exactly one short sentence, "
        f"explain when and how the internet meme \"{meme_name}\" became popular. "
        "Only describe this exact meme, not a similarly named person, place, or product. "
        "If unsure of the exact origin, say so briefly. "
        "Plain simple language, no preamble, no markdown."
    )

    history_response = gemini_client.models.generate_content(
        model = "gemini-flash-lite-latest",
        contents=history_prompt,
    )
    return {"text": history_response.text}

@app.get("/api/stats")
def stats():
    with db() as conn:
        cur = conn.cursor()
        payload = build_stats_payload(cur)
        cur.close()
    return payload


@app.post("/api/battle")
def battle(payload: BattleRequest):
    left_id = require_meme(payload.leftId)
    right_id = require_meme(payload.rightId)

    with db() as conn:
        cur = conn.cursor()
        cur.execute(
            f"""
            WITH bounds AS (
                SELECT max(created_at) AS latest FROM meme_mentions
            ),
            scored AS (
                SELECT
                    meme_id,
                    exp(-{DECAY_LAMBDA} * extract(epoch FROM (
                        (SELECT latest FROM bounds) - created_at
                    ))) AS weight
                FROM meme_mentions
                WHERE meme_id IN (%(left)s, %(right)s)
            )
            SELECT
                count(*) FILTER (WHERE meme_id = %(left)s) AS left_total,
                count(*) FILTER (WHERE meme_id = %(right)s) AS right_total,
                coalesce(sum(weight) FILTER (WHERE meme_id = %(left)s), 0) AS left_score,
                coalesce(sum(weight) FILTER (WHERE meme_id = %(right)s), 0) AS right_score
            FROM scored
            """,
            {"left": left_id, "right": right_id},
        )
        left_total, right_total, left_score, right_score = cur.fetchone()
        cur.close()

    if left_total == 0 and right_total == 0:
        winner = None
    else:
        winner = payload.leftId if left_score >= right_score else payload.rightId

    return {
        "leftTotal": left_total,
        "rightTotal": right_total,
        "leftScore": float(left_score),
        "rightScore": float(right_score),
        "winnerId": winner,
    }


@app.post("/api/record-battle")
def record_battle(payload: RecordBattleRequest):
    left_id = require_meme(payload.leftId)
    right_id = require_meme(payload.rightId)
    winner_id = require_meme(payload.winnerId)
    if winner_id not in (left_id, right_id):
        raise HTTPException(status_code=400, detail="Winner must be one of the fighters")

    with db() as conn:
        ensure_tables(conn)
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO battle_records (
                left_id, right_id, winner_id, left_total, right_total, winner_share
            )
            VALUES (%s, %s, %s, %s, %s, %s)
            """,
            [
                left_id,
                right_id,
                winner_id,
                int(payload.leftTotal),
                int(payload.rightTotal),
                float(payload.winnerShare),
            ],
        )
        conn.commit()
        stats_payload = build_stats_payload(cur)
        cur.close()
    return stats_payload


def generate_speech(text: str) -> bytes:
    audio_stream = elevenlabs_client.text_to_speech.convert(
        text=text,
        voice_id=VOICE_ID,
        model_id="eleven_turbo_v2_5",
        output_format="mp3_44100_128",
    )
    return b"".join(audio_stream)


@app.post("/api/announce-pick")
def announce_pick(payload: PickRequest):
    audio = generate_speech(payload.memeName.upper() + "!")
    return Response(content=audio, media_type="audio/mpeg")


@app.post("/api/announce-start")
def announce_start(payload: StartRequest):
    text = f"{payload.leftName} versus {payload.rightName}! Fight!"
    audio = generate_speech(text)
    return Response(content=audio, media_type="audio/mpeg")


@app.post("/api/announce-winner")
def announce_winner(payload: WinnerRequest):
    text = f"{payload.winnerName} wins! {payload.pct} percent meme dominance!"
    audio = generate_speech(text)
    return Response(content=audio, media_type="audio/mpeg")