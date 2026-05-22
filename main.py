import os
import random
import sqlite3
import urllib.parse
from datetime import datetime, timezone
from contextlib import asynccontextmanager

import httpx
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger
from dotenv import load_dotenv
from fastapi import FastAPI
from pydantic import BaseModel
from fastapi.staticfiles import StaticFiles

load_dotenv()

DB_PATH = os.getenv("DB_PATH", "usage.db")

# ── Polling configuration ───────────────────────────────────────────────────
# Hard safety floor: never poll faster than this, regardless of UI/env input.
MIN_INTERVAL = 60          # seconds (Feature 2: floor)
MAX_INTERVAL = 3600        # seconds (sane upper bound, 1 hour)
DEFAULT_INTERVAL = 180     # seconds (3 minutes — recommended default)

JITTER_SECONDS = 10        # Feature 3: random +/- offset to avoid clockwork timing

# Feature 4: backoff settings
BACKOFF_STATUS = {401, 403, 429}   # statuses that trigger slow-down
BACKOFF_MAX = 1800                 # cap backoff at 30 minutes

# Env var sets the *initial* interval the first time the DB is created.
ENV_INITIAL_INTERVAL = int(os.getenv("POLL_INTERVAL", str(DEFAULT_INTERVAL)))


def clamp_interval(seconds: int) -> int:
    """Enforce the safety floor and ceiling."""
    try:
        seconds = int(seconds)
    except (TypeError, ValueError):
        seconds = DEFAULT_INTERVAL
    return max(MIN_INTERVAL, min(MAX_INTERVAL, seconds))


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
    con.execute("""
        CREATE TABLE IF NOT EXISTS settings (
            key   TEXT PRIMARY KEY,
            value TEXT
        )
    """)
    con.commit()
    # Seed the poll interval once, from the env var, if not already present.
    cur = con.execute("SELECT value FROM settings WHERE key='poll_interval'").fetchone()
    if cur is None:
        con.execute(
            "INSERT INTO settings (key, value) VALUES ('poll_interval', ?)",
            (str(clamp_interval(ENV_INITIAL_INTERVAL)),),
        )
        con.commit()
    con.close()


def get_setting(key: str, default=None):
    con = sqlite3.connect(DB_PATH)
    row = con.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    con.close()
    return row[0] if row else default


def set_setting(key: str, value: str):
    con = sqlite3.connect(DB_PATH)
    con.execute(
        "INSERT INTO settings (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, str(value)),
    )
    con.commit()
    con.close()


def get_poll_interval() -> int:
    return clamp_interval(int(get_setting("poll_interval", DEFAULT_INTERVAL)))


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
    # datetime(ts) normalizes the stored ISO 8601 string so the time filter works.
    rows = con.execute(
        """SELECT ts, five_hour, seven_day FROM usage_history
           WHERE datetime(ts) >= datetime('now', ?)
           ORDER BY datetime(ts) ASC""",
        (f"-{hours} hours",),
    ).fetchall()
    con.close()
    return [{"ts": r[0], "five_hour": r[1], "seven_day": r[2]} for r in rows]


# ── Poller state ────────────────────────────────────────────────────────────

latest_data: dict = {"ok": False, "status": "starting"}

# Runtime poll state, surfaced to the UI so the user can see what's happening.
poll_state = {
    "interval": DEFAULT_INTERVAL,   # the configured base interval (seconds)
    "effective": DEFAULT_INTERVAL,  # current interval incl. any backoff (seconds)
    "backoff": False,               # are we currently backed off?
    "next_poll": None,              # ISO timestamp of next scheduled poll
}

scheduler = AsyncIOScheduler()
JOB_ID = "usage_poll"


def build_headers() -> dict:
    org_id    = os.environ["CLAUDE_ORG_ID"]
    anon_id   = os.environ["CLAUDE_ANON_ID"]
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


def schedule_next(seconds: int):
    """(Re)schedule the poll job with jitter. Feature 1 + 3."""
    base = clamp_interval(seconds)
    poll_state["effective"] = base
    scheduler.reschedule_job(
        JOB_ID,
        trigger=IntervalTrigger(seconds=base, jitter=JITTER_SECONDS),
    )
    job = scheduler.get_job(JOB_ID)
    poll_state["next_poll"] = job.next_run_time.isoformat() if job and job.next_run_time else None


def apply_backoff():
    """Feature 4: double the effective interval up to a cap on auth/rate errors."""
    current = poll_state["effective"]
    new_eff = min(current * 2, BACKOFF_MAX)
    poll_state["backoff"] = True
    schedule_next(new_eff)
    print(f"[backoff] slowing to {new_eff}s")


def clear_backoff():
    """Return to the user-configured interval after a successful poll."""
    if poll_state["backoff"]:
        poll_state["backoff"] = False
        schedule_next(poll_state["interval"])
        print(f"[backoff] cleared, back to {poll_state['interval']}s")


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
            # Feature 4: back off on auth/rate-limit responses
            if resp.status_code in BACKOFF_STATUS:
                apply_backoff()
            return

        data = resp.json()
        fh = data.get("five_hour") or {}
        sd = data.get("seven_day") or {}

        five_hour = fh.get("utilization")
        seven_day = sd.get("utilization")
        fh_resets = fh.get("resets_at")
        sd_resets = sd.get("resets_at")

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

        clear_backoff()  # success → resume normal cadence

        # refresh next_poll for the UI
        job = scheduler.get_job(JOB_ID)
        poll_state["next_poll"] = job.next_run_time.isoformat() if job and job.next_run_time else None

    except Exception as e:
        latest_data = {"ok": False, "status": str(e)}
        print(f"[{datetime.now().isoformat()}] Error: {e}")


# ── App lifecycle ─────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    interval = get_poll_interval()
    poll_state["interval"] = interval
    poll_state["effective"] = interval

    await poll_usage()  # immediate first poll

    scheduler.add_job(
        poll_usage,
        trigger=IntervalTrigger(seconds=interval, jitter=JITTER_SECONDS),
        id=JOB_ID,
    )
    scheduler.start()

    job = scheduler.get_job(JOB_ID)
    poll_state["next_poll"] = job.next_run_time.isoformat() if job and job.next_run_time else None

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


@app.get("/api/settings")
async def api_get_settings():
    return {
        "interval": poll_state["interval"],
        "effective": poll_state["effective"],
        "backoff": poll_state["backoff"],
        "next_poll": poll_state["next_poll"],
        "min_interval": MIN_INTERVAL,
        "max_interval": MAX_INTERVAL,
    }


class IntervalUpdate(BaseModel):
    interval: int


@app.post("/api/settings")
async def api_set_interval(body: IntervalUpdate):
    """Feature 1: change the poll interval live, with the Feature 2 floor enforced."""
    requested = body.interval
    interval = clamp_interval(requested)

    set_setting("poll_interval", interval)
    poll_state["interval"] = interval
    poll_state["backoff"] = False  # a manual change clears any backoff
    schedule_next(interval)

    return {
        "ok": True,
        "interval": interval,
        "requested": requested,
        "clamped": interval != requested,
        "min_interval": MIN_INTERVAL,
        "next_poll": poll_state["next_poll"],
    }


@app.post("/api/poll-now")
async def api_poll_now():
    """Trigger an immediate poll (used by the Refresh button)."""
    await poll_usage()
    return {"ok": latest_data.get("ok", False)}


app.mount("/", StaticFiles(directory="public", html=True), name="static")