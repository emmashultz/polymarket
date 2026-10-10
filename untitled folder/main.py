"""
Polymarket US P&L Tracker API
Pulls your positions from the authenticated Polymarket US API, computes
realized and unrealized P&L, and stores periodic snapshots in SQLite.

Run:
  export POLY_KEY_ID=...        # Key ID from polymarket.us/developer
  export POLY_SECRET_KEY=...    # secret key (never commit this)
  uvicorn main:app --reload
Docs: http://localhost:8000/docs
"""
import asyncio
import base64
import os
import sqlite3
import time
from contextlib import asynccontextmanager
from typing import Optional

import httpx
from cryptography.hazmat.primitives.asymmetric import ed25519
from fastapi import FastAPI, HTTPException

API = "https://api.polymarket.us"
KEY_ID = os.getenv("POLY_KEY_ID", "")
SECRET = os.getenv("POLY_SECRET_KEY", "")
DB_PATH = os.getenv("PNL_DB", "pnl.db")
SNAPSHOT_EVERY_SEC = int(os.getenv("SNAPSHOT_EVERY_SEC", "900"))  # 15 min
ACCOUNT = "default"


# ---------- auth ----------
_signer = None


def signer():
    global _signer
    if _signer is None:
        if not (KEY_ID and SECRET):
            raise HTTPException(500, "Set POLY_KEY_ID and POLY_SECRET_KEY env vars")
        _signer = ed25519.Ed25519PrivateKey.from_private_bytes(base64.b64decode(SECRET)[:32])
    return _signer


def auth_headers(method: str, path: str) -> dict:
    """Ed25519 signature over timestamp + METHOD + path (no query string)."""
    ts = str(int(time.time() * 1000))
    sig = signer().sign(f"{ts}{method.upper()}{path}".encode())
    return {
        "X-PM-Access-Key": KEY_ID,
        "X-PM-Timestamp": ts,
        "X-PM-Signature": base64.b64encode(sig).decode(),
    }


# ---------- storage ----------
def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with db() as c:
        c.execute("""
            CREATE TABLE IF NOT EXISTS snapshots (
                ts INTEGER NOT NULL,
                wallet TEXT NOT NULL,
                realized REAL, unrealized REAL, total REAL,
                open_value REAL, cost_basis REAL, open_count INTEGER
            )""")
        c.execute("CREATE INDEX IF NOT EXISTS ix_snap ON snapshots(wallet, ts)")


# ---------- polymarket us fetchers ----------
async def fetch_positions() -> list:
    """All positions, following nextCursor until eof. Returns a flat list."""
    path = "/v1/portfolio/positions"
    out, cursor = [], None
    async with httpx.AsyncClient(timeout=20) as client:
        while True:
            params = {"limit": 100}
            if cursor:
                params["cursor"] = cursor
            r = await client.get(API + path, params=params, headers=auth_headers("GET", path))
            if r.status_code == 401:
                raise HTTPException(401, "Polymarket rejected the key (check key, secret, clock)")
            r.raise_for_status()
            data = r.json()
            for slug, p in (data.get("positions") or {}).items():
                p["slug"] = slug
                out.append(p)
            cursor = data.get("nextCursor")
            if data.get("eof", True) or not cursor:
                break
    return out


def f(x) -> float:
    if isinstance(x, dict):  # Amount objects look like {"value": "1.23", "currency": "USD"}
        x = x.get("value")
    try:
        return float(x or 0)
    except (TypeError, ValueError):
        return 0.0


def is_open(p) -> bool:
    return not p.get("expired") and f(p.get("netPositionDecimal")) != 0


async def get_book():
    allpos = await fetch_positions()
    return [p for p in allpos if is_open(p)], [p for p in allpos if not is_open(p)]


def summarize(open_pos, closed_pos):
    unrealized = sum(f(p.get("cashValue")) for p in open_pos)
    realized = sum(f(p.get("realized")) for p in open_pos + closed_pos)
    cost_basis = sum(f(p.get("cost")) for p in open_pos)
    wins = sum(1 for p in closed_pos if f(p.get("realized")) > 0)
    return {
        "realized_pnl": round(realized, 2),
        "unrealized_pnl": round(unrealized, 2),
        "total_pnl": round(realized + unrealized, 2),
        "open_value": round(cost_basis + unrealized, 2),
        "open_cost_basis": round(cost_basis, 2),
        "open_positions": len(open_pos),
        "closed_positions": len(closed_pos),
        "win_rate_closed": round(wins / len(closed_pos), 3) if closed_pos else None,
    }


def slim(p):
    m = p.get("marketMetadata") or {}
    return {
        "slug": p.get("slug"),
        "title": m.get("title"),
        "outcome": m.get("outcome"),
        "eventSlug": m.get("eventSlug"),
        "netPosition": f(p.get("netPositionDecimal")),
        "qtyBought": f(p.get("qtyBoughtDecimal")),
        "qtySold": f(p.get("qtySoldDecimal")),
        "cost": f(p.get("cost")),
        "realizedPnl": f(p.get("realized")),
        "unrealizedPnl": f(p.get("cashValue")) if is_open(p) else 0.0,
        "expired": p.get("expired"),
        "updated": p.get("updateTime"),
    }


def resolve_wallet(_: Optional[str] = None) -> str:
    return ACCOUNT


async def take_snapshot(wallet: str = ACCOUNT):
    o, c = await get_book()
    s = summarize(o, c)
    with db() as conn:
        conn.execute(
            "INSERT INTO snapshots VALUES (?,?,?,?,?,?,?,?)",
            (int(time.time()), wallet, s["realized_pnl"], s["unrealized_pnl"],
             s["total_pnl"], s["open_value"], s["open_cost_basis"], s["open_positions"]),
        )
    return s


# ---------- background snapshot loop ----------
async def snapshot_loop():
    while True:
        try:
            if KEY_ID and SECRET:
                await take_snapshot()
        except Exception as e:  # keep the loop alive
            print("snapshot failed:", e)
        await asyncio.sleep(SNAPSHOT_EVERY_SEC)


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    task = asyncio.create_task(snapshot_loop())
    yield
    task.cancel()


app = FastAPI(title="Polymarket US P&L Tracker", lifespan=lifespan)


# ---------- routes ----------
@app.get("/pnl")
async def pnl():
    """Live P&L summary."""
    o, c = await get_book()
    return {"as_of": int(time.time()), **summarize(o, c)}


@app.get("/positions/open")
async def open_positions():
    o, _ = await get_book()
    return sorted((slim(p) for p in o), key=lambda r: r["unrealizedPnl"], reverse=True)


@app.get("/positions/closed")
async def closed_positions():
    _, c = await get_book()
    return sorted((slim(p) for p in c), key=lambda r: r["realizedPnl"], reverse=True)


@app.get("/pnl/by-event")
async def pnl_by_event():
    """Total P&L grouped by event."""
    o, c = await get_book()
    groups = {}
    for p in map(slim, o + c):
        key = p["eventSlug"] or p["slug"] or "unknown"
        g = groups.setdefault(key, {"event": key, "realized": 0.0, "unrealized": 0.0})
        g["realized"] += p["realizedPnl"]
        g["unrealized"] += p["unrealizedPnl"]
    for g in groups.values():
        g["total"] = round(g["realized"] + g["unrealized"], 2)
        g["realized"], g["unrealized"] = round(g["realized"], 2), round(g["unrealized"], 2)
    return sorted(groups.values(), key=lambda g: g["total"], reverse=True)


@app.post("/snapshot")
async def snapshot_now():
    """Force a snapshot right now (also runs automatically every SNAPSHOT_EVERY_SEC)."""
    return await take_snapshot()


@app.get("/pnl/history")
async def history(days: int = 30, wallet: Optional[str] = None):
    """Stored snapshots, oldest first. Good for charting an equity curve."""
    w = resolve_wallet(wallet)
    since = int(time.time()) - days * 86400
    with db() as conn:
        rows = conn.execute(
            "SELECT * FROM snapshots WHERE wallet=? AND ts>=? ORDER BY ts", (w, since)
        ).fetchall()
    return [dict(r) for r in rows]


@app.get("/pnl/daily")
async def daily(days: int = 30):
    """Change in total P&L per day, using the last snapshot of each day (UTC)."""
    snaps = await history(days + 1)
    last_per_day = {}
    for s in snaps:
        last_per_day[time.strftime("%Y-%m-%d", time.gmtime(s["ts"]))] = s["total"]
    out, prev = [], None
    for day, total in sorted(last_per_day.items()):
        out.append({"date": day, "total_pnl": total,
                    "day_change": None if prev is None else round(total - prev, 2)})
        prev = total
    return out


@app.get("/health")
async def health():
    return {"ok": True, "keys_configured": bool(KEY_ID and SECRET)}
