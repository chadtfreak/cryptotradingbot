"""SQLite storage for the account, trades, costs, equity history and the event log."""

import json
import sqlite3
import threading
import time
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS state (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS trades (
    id INTEGER PRIMARY KEY, ts INTEGER NOT NULL, side TEXT NOT NULL, qty REAL NOT NULL,
    price REAL NOT NULL, notional REAL NOT NULL, fee REAL NOT NULL, gas REAL NOT NULL,
    pnl REAL, usd_aud REAL, reason TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS ledger (
    id INTEGER PRIMARY KEY, ts INTEGER NOT NULL, kind TEXT NOT NULL, amount REAL NOT NULL, note TEXT
);
CREATE TABLE IF NOT EXISTS equity (
    ts INTEGER PRIMARY KEY, equity REAL NOT NULL, cash REAL NOT NULL, qty REAL NOT NULL, price REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY, ts INTEGER NOT NULL, level TEXT NOT NULL, message TEXT NOT NULL
);
"""


class Store:
    def __init__(self, path: str):
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.lock = threading.Lock()
        with self.lock:
            self.conn.executescript(SCHEMA)

    def _exec(self, sql: str, args: tuple = ()) -> sqlite3.Cursor:
        with self.lock:
            cur = self.conn.execute(sql, args)
            self.conn.commit()
            return cur

    def _rows(self, sql: str, args: tuple = ()) -> list[dict]:
        with self.lock:
            return [dict(r) for r in self.conn.execute(sql, args).fetchall()]

    # Key/value state
    def get(self, key: str, default=None):
        rows = self._rows("SELECT value FROM state WHERE key = ?", (key,))
        return json.loads(rows[0]["value"]) if rows else default

    def set(self, key: str, value) -> None:
        self._exec("INSERT OR REPLACE INTO state (key, value) VALUES (?, ?)", (key, json.dumps(value)))

    # Records
    def log(self, message: str, level: str = "info", ts: int | None = None) -> None:
        self._exec("INSERT INTO events (ts, level, message) VALUES (?, ?, ?)", (ts or int(time.time()), level, message))

    def add_trade(self, ts: int, fill, pnl: float | None, usd_aud: float | None, reason: str) -> None:
        self._exec(
            "INSERT INTO trades (ts, side, qty, price, notional, fee, gas, pnl, usd_aud, reason) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (ts, fill.side, fill.qty, fill.price, fill.notional, fill.fee, fill.gas, pnl, usd_aud, reason),
        )

    def add_ledger(self, ts: int, kind: str, amount: float, note: str = "") -> None:
        self._exec("INSERT INTO ledger (ts, kind, amount, note) VALUES (?, ?, ?, ?)", (ts, kind, amount, note))

    def add_equity(self, ts: int, equity: float, cash: float, qty: float, price: float) -> None:
        self._exec("INSERT OR REPLACE INTO equity (ts, equity, cash, qty, price) VALUES (?,?,?,?,?)", (ts, equity, cash, qty, price))

    # Queries
    def trades(self, limit: int = 1000) -> list[dict]:
        return self._rows("SELECT * FROM trades ORDER BY ts DESC, id DESC LIMIT ?", (limit,))

    def events(self, limit: int = 100) -> list[dict]:
        return self._rows("SELECT * FROM events ORDER BY id DESC LIMIT ?", (limit,))

    def equity_history(self, since: int = 0) -> list[dict]:
        return self._rows("SELECT * FROM equity WHERE ts >= ? ORDER BY ts", (since,))

    def ledger_total(self, kind: str) -> float:
        return self._rows("SELECT COALESCE(SUM(amount), 0) AS t FROM ledger WHERE kind = ?", (kind,))[0]["t"]
