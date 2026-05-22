import os
import sqlite3
import urllib.parse
from datetime import datetime, timezone
from contextlib import asynccontextmanager

import httpx
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

load_dotenv()

DB_PATH = os.getenv("DB_PATH", "usage.db")
POLL_INTERVAL = int(os.getenv("POLL_INTERVAL", "60"))

# ── DB setup ──────────────────────────────────────────────────────────────────

def init_db():
    con = sqlite3.connect(DB_PATH)
    con.execute("""
        CREATE TABLE IF NOT EXISTS usage_history (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            ts          TEXT    NOT NULL,
            five_hour   REAL,
            seven_day   REAL,
            fh_resets   TEXT,
            sd_resets   TEXT
        )
    """)
    con.commit()
    con.close()


def insert_record(five_hour, seven_day, fh_resets, sd_resets):
    con = sqlite3.connect(DB_PATH)
    con.execute(
        "INSERT INTO usage_history (ts, five_hour, seven_day, fh_resets, sd_resets) VALUES (?,?,?,?,?)",
        (datetime.now(timezone.utc).isoformat(), five_hour, seven_day, fh_resets, sd_resets),
    )
    con.commit()
    con.close()


def get_history(hours: int = 24):
    con = sqlite3.connect(DB_PATH)
    # datetime(ts) normalizes the stored ISO 8601 string (with 'T' separator and
    # timezone offset) into SQLite's comparable format. Without this wrapper the
    # comparison is a raw string compare and the time filter does not work.
    rows = con.execute(
        """SELECT ts, five_hour, seven_day FROM usage_history
           WHERE datetime(ts) >= datetime('now', ?)
           ORDER BY datetime(ts) ASC""",
        (f"-{hours} hours",),
    ).fetchall()
    con.close()
    return [{"ts": r[0], "five_hour": r[1], "seven_day": r[2]} for r in rows]


def get_latest():
    con = sqlite3.connect(DB_PATH)
    row = con.execute(
        "SELECT ts, five_hour, seven_day, fh_resets, sd_resets FROM usage_history ORDER BY id DESC LIMIT 1"
    ).fetchone()
    con.close()
    return row


# ── Claude API poller ─────────────────────────────────────────────────────────

latest_data: dict = {"ok": False, "status": "starting"}


def build_headers() -> dict:
    org_id   = os.environ["CLAUDE_ORG_ID"]
    anon_id  = os.environ["CLAUDE_ANON_ID"]
    device_id = os.environ["CLAUDE_DEVICE_ID"]

    cookie_parts = [
        f"sessionKey={os.environ['CLAUDE_SESSION_KEY']}",
        f"lastActiveOrg={org_id}",
        f"anthropic-device-id={device_id}",
        f"ajs_anonymous_id={anon_id}",
    ]
    if os.getenv("CLAUDE_CF_CLEARANCE"):
        cookie_parts.append(f"cf_clearance={os.environ['CLAUDE_CF_CLEARANCE']}")
    if os.getenv("CLAUDE_CF_BM"):
        cookie_parts.append(f"__cf_bm={os.environ['CLAUDE_CF_BM']}")

    return {
        "accept": "application/json",
        "content-type": "application/json",
        "anthropic-client-platform": "web_claude_ai",
        "anthropic-client-version": "1.0.0",
        "anthropic-device-id": device_id,
        "anthropic-anonymous-id": urllib.parse.unquote(anon_id),
        "referer": "https://claude.ai/settings/usage",
        "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "cookie": "; ".join(cookie_parts),
    }


async def poll_usage():
    global latest_data
    try:
        org_id = os.environ["CLAUDE_ORG_ID"]
        url = f"https://claude.ai/api/organizations/{org_id}/usage"

        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(url, headers=build_headers())

        if resp.status_code != 200:
            latest_data = {"ok": False, "status": f"http_{resp.status_code}"}
            print(f"[{datetime.now().isoformat()}] Poll failed: {resp.status_code}")
            return

        data = resp.json()
        fh = data.get("five_hour") or {}
        sd = data.get("seven_day") or {}

        five_hour  = fh.get("utilization")
        seven_day  = sd.get("utilization")
        fh_resets  = fh.get("resets_at")
        sd_resets  = sd.get("resets_at")

        insert_record(five_hour, seven_day, fh_resets, sd_resets)

        latest_data = {
            "ok": True,
            "five_hour": five_hour,
            "seven_day": seven_day,
            "fh_resets": fh_resets,
            "sd_resets": sd_resets,
            "last_updated": datetime.now(timezone.utc).isoformat(),
            "extra_usage": data.get("extra_usage"),
        }
        print(f"[{datetime.now().isoformat()}] 5h={five_hour}%  7d={seven_day}%")

    except Exception as e:
        latest_data = {"ok": False, "status": str(e)}
        print(f"[{datetime.now().isoformat()}] Error: {e}")


# ── App lifecycle ─────────────────────────────────────────────────────────────

scheduler = AsyncIOScheduler()


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    await poll_usage()                          # immediate first poll
    scheduler.add_job(poll_usage, "interval", seconds=POLL_INTERVAL)
    scheduler.start()
    yield
    scheduler.shutdown()


app = FastAPI(lifespan=lifespan)

# ── Routes ────────────────────────────────────────────────────────────────────

@app.get("/api/usage")
async def api_usage():
    return latest_data


@app.get("/api/history")
async def api_history(hours: int = 24):
    return get_history(hours)


app.mount("/", StaticFiles(directory="public", html=True), name="static")