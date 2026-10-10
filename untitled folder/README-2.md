# Polymarket US P&L Tracker

A small FastAPI service that signs requests to the Polymarket US API with your Ed25519 key, pulls your positions, and tracks realized, unrealized, and total P&L over time.

## Setup

```bash
pip install -r requirements.txt
export POLY_KEY_ID=your_key_id
export POLY_SECRET_KEY=your_secret_key
uvicorn main:app --reload
```

Open http://localhost:8000/docs to try every endpoint.

Never paste the secret into the code or commit it. Keep it in env vars or a `.env` file listed in `.gitignore`. If you host this, use the host's secrets manager.

## Endpoints

| Route | What it returns |
|---|---|
| `GET /pnl` | Live summary: realized, unrealized, total, cost basis, win rate |
| `GET /positions/open` | Open positions with unrealized P&L |
| `GET /positions/closed` | Closed or expired positions with realized P&L |
| `GET /pnl/by-event` | P&L grouped by event |
| `POST /snapshot` | Save a snapshot now |
| `GET /pnl/history?days=30` | Stored snapshots (equity curve) |
| `GET /pnl/daily?days=30` | Day over day change in total P&L |

## How P&L is computed

Realized = sum of `realized` across all positions.
Unrealized = sum of `cashValue` on open positions (the docs describe this field as unrealized P&L).

A background task snapshots every 15 minutes into `pnl.db`, so history starts when you first run it.
