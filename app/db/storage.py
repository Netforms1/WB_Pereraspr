import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import aiosqlite
from cryptography.fernet import Fernet

from app.config import settings


SCHEMA = """
CREATE TABLE IF NOT EXISTS kv (
    key TEXT PRIMARY KEY,
    value BLOB NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS warehouses (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    available INTEGER NOT NULL DEFAULT 1,
    last_seen_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS requests (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    from_warehouse_id INTEGER NOT NULL,
    to_warehouse_id INTEGER NOT NULL,
    article TEXT NOT NULL,
    quantity INTEGER NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    last_error TEXT,
    attempts INTEGER NOT NULL DEFAULT 0,
    completed_at TEXT
);
"""


@dataclass
class Warehouse:
    id: int
    name: str
    available: bool


@dataclass
class Request:
    id: int
    from_warehouse_id: int
    to_warehouse_id: int
    article: str
    quantity: int
    status: str
    last_error: Optional[str]
    attempts: int


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class Storage:
    def __init__(self, path: Path):
        self.path = path
        self._fernet = Fernet(settings.session_encryption_key.encode())

    async def init(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        async with aiosqlite.connect(self.path) as db:
            await db.executescript(SCHEMA)
            await db.commit()

    async def save_session(self, payload: dict) -> None:
        blob = self._fernet.encrypt(json.dumps(payload).encode())
        async with aiosqlite.connect(self.path) as db:
            await db.execute(
                "INSERT INTO kv(key, value, updated_at) VALUES('wb_session', ?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
                (blob, _now()),
            )
            await db.commit()

    async def load_session(self) -> Optional[dict]:
        async with aiosqlite.connect(self.path) as db:
            async with db.execute("SELECT value FROM kv WHERE key='wb_session'") as cur:
                row = await cur.fetchone()
        if not row:
            return None
        return json.loads(self._fernet.decrypt(row[0]).decode())

    async def clear_session(self) -> None:
        async with aiosqlite.connect(self.path) as db:
            await db.execute("DELETE FROM kv WHERE key='wb_session'")
            await db.commit()

    async def upsert_warehouses(self, items: list[Warehouse]) -> None:
        now = _now()
        async with aiosqlite.connect(self.path) as db:
            for w in items:
                await db.execute(
                    "INSERT INTO warehouses(id, name, available, last_seen_at) VALUES(?, ?, ?, ?) "
                    "ON CONFLICT(id) DO UPDATE SET name=excluded.name, available=excluded.available, last_seen_at=excluded.last_seen_at",
                    (w.id, w.name, int(w.available), now),
                )
            await db.commit()

    async def list_warehouses(self, only_available: bool = False) -> list[Warehouse]:
        q = "SELECT id, name, available FROM warehouses"
        if only_available:
            q += " WHERE available = 1"
        q += " ORDER BY name"
        async with aiosqlite.connect(self.path) as db:
            async with db.execute(q) as cur:
                rows = await cur.fetchall()
        return [Warehouse(id=r[0], name=r[1], available=bool(r[2])) for r in rows]

    async def get_warehouse(self, wid: int) -> Optional[Warehouse]:
        async with aiosqlite.connect(self.path) as db:
            async with db.execute(
                "SELECT id, name, available FROM warehouses WHERE id=?", (wid,)
            ) as cur:
                row = await cur.fetchone()
        return Warehouse(id=row[0], name=row[1], available=bool(row[2])) if row else None

    async def add_request(
        self, from_id: int, to_id: int, article: str, quantity: int
    ) -> int:
        async with aiosqlite.connect(self.path) as db:
            cur = await db.execute(
                "INSERT INTO requests(created_at, from_warehouse_id, to_warehouse_id, article, quantity) "
                "VALUES(?, ?, ?, ?, ?)",
                (_now(), from_id, to_id, article, quantity),
            )
            await db.commit()
            return cur.lastrowid

    async def list_requests(self, status: Optional[str] = None) -> list[Request]:
        q = "SELECT id, from_warehouse_id, to_warehouse_id, article, quantity, status, last_error, attempts FROM requests"
        args: tuple = ()
        if status:
            q += " WHERE status = ?"
            args = (status,)
        q += " ORDER BY id"
        async with aiosqlite.connect(self.path) as db:
            async with db.execute(q, args) as cur:
                rows = await cur.fetchall()
        return [
            Request(
                id=r[0],
                from_warehouse_id=r[1],
                to_warehouse_id=r[2],
                article=r[3],
                quantity=r[4],
                status=r[5],
                last_error=r[6],
                attempts=r[7],
            )
            for r in rows
        ]

    async def get_request(self, rid: int) -> Optional[Request]:
        async with aiosqlite.connect(self.path) as db:
            async with db.execute(
                "SELECT id, from_warehouse_id, to_warehouse_id, article, quantity, status, last_error, attempts "
                "FROM requests WHERE id=?",
                (rid,),
            ) as cur:
                row = await cur.fetchone()
        if not row:
            return None
        return Request(
            id=row[0],
            from_warehouse_id=row[1],
            to_warehouse_id=row[2],
            article=row[3],
            quantity=row[4],
            status=row[5],
            last_error=row[6],
            attempts=row[7],
        )

    async def cancel_request(self, rid: int) -> bool:
        async with aiosqlite.connect(self.path) as db:
            cur = await db.execute(
                "UPDATE requests SET status='cancelled' WHERE id=? AND status='pending'",
                (rid,),
            )
            await db.commit()
            return cur.rowcount > 0

    async def mark_attempt(self, rid: int, error: Optional[str]) -> None:
        async with aiosqlite.connect(self.path) as db:
            await db.execute(
                "UPDATE requests SET attempts = attempts + 1, last_error = ? WHERE id = ?",
                (error, rid),
            )
            await db.commit()

    async def mark_done(self, rid: int) -> None:
        async with aiosqlite.connect(self.path) as db:
            await db.execute(
                "UPDATE requests SET status='done', completed_at=?, last_error=NULL WHERE id=?",
                (_now(), rid),
            )
            await db.commit()

    async def mark_failed(self, rid: int, error: str) -> None:
        async with aiosqlite.connect(self.path) as db:
            await db.execute(
                "UPDATE requests SET status='failed', last_error=? WHERE id=?",
                (error, rid),
            )
            await db.commit()

    async def reset_pending_for_today(self) -> None:
        async with aiosqlite.connect(self.path) as db:
            await db.execute(
                "UPDATE requests SET attempts = 0, last_error = NULL WHERE status = 'pending'"
            )
            await db.commit()


_storage: Optional[Storage] = None


def get_storage() -> Storage:
    global _storage
    if _storage is None:
        _storage = Storage(settings.db_path)
    return _storage
