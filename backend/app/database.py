from __future__ import annotations

import json
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def new_id() -> str:
    return uuid.uuid4().hex


class Database:
    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.executescript("""
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS projects (
                    id TEXT PRIMARY KEY, title TEXT NOT NULL, created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL, session_id TEXT, published_build TEXT
                );
                CREATE TABLE IF NOT EXISTS messages (
                    id TEXT PRIMARY KEY, project_id TEXT NOT NULL, role TEXT NOT NULL,
                    content TEXT NOT NULL, created_at TEXT NOT NULL, attachments TEXT NOT NULL DEFAULT '[]'
                );
                CREATE TABLE IF NOT EXISTS tasks (
                    id TEXT PRIMARY KEY, project_id TEXT NOT NULL, status TEXT NOT NULL,
                    prompt TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                    error TEXT, build_id TEXT
                );
                CREATE TABLE IF NOT EXISTS events (
                    seq INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT NOT NULL,
                    type TEXT NOT NULL, data TEXT NOT NULL, created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS events_task ON events(task_id, seq);
                CREATE TABLE IF NOT EXISTS builds (
                    id TEXT PRIMARY KEY, project_id TEXT NOT NULL, task_id TEXT,
                    created_at TEXT NOT NULL, entry TEXT NOT NULL, source_path TEXT NOT NULL
                );
            """)

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=15)
        db.row_factory = sqlite3.Row
        try:
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def rows(self, sql: str, args: tuple = ()) -> list[dict]:
        with self.connect() as db:
            return [dict(row) for row in db.execute(sql, args).fetchall()]

    def one(self, sql: str, args: tuple = ()) -> dict | None:
        values = self.rows(sql, args)
        return values[0] if values else None

    def execute(self, sql: str, args: tuple = ()) -> None:
        with self.connect() as db:
            db.execute(sql, args)

    def create_project(self, title: str) -> dict:
        project_id, timestamp = new_id(), now()
        self.execute("INSERT INTO projects(id,title,created_at,updated_at) VALUES(?,?,?,?)",
                     (project_id, title[:100] or "新游戏", timestamp, timestamp))
        return self.project(project_id)

    def project(self, project_id: str) -> dict | None:
        return self.one("SELECT * FROM projects WHERE id=?", (project_id,))

    def message(self, project_id: str, role: str, content: str, attachments: list | None = None) -> dict:
        record = {"id": new_id(), "project_id": project_id, "role": role,
                  "content": content, "created_at": now(), "attachments": attachments or []}
        self.execute("INSERT INTO messages VALUES(?,?,?,?,?,?)",
                     (record["id"], project_id, role, content, record["created_at"],
                      json.dumps(record["attachments"], ensure_ascii=False)))
        self.execute("UPDATE projects SET updated_at=? WHERE id=?", (now(), project_id))
        return record

    def messages(self, project_id: str) -> list[dict]:
        rows = self.rows("SELECT * FROM messages WHERE project_id=? ORDER BY created_at,rowid", (project_id,))
        for row in rows:
            row["attachments"] = json.loads(row["attachments"])
        return rows

    def create_task(self, project_id: str, prompt: str) -> dict:
        task_id, timestamp = new_id(), now()
        self.execute("INSERT INTO tasks(id,project_id,status,prompt,created_at,updated_at) VALUES(?,?,?,?,?,?)",
                     (task_id, project_id, "queued", prompt, timestamp, timestamp))
        return self.task(task_id)

    def task(self, task_id: str) -> dict | None:
        return self.one("SELECT * FROM tasks WHERE id=?", (task_id,))

    def status(self, task_id: str, status: str, error: str | None = None, build_id: str | None = None):
        self.execute("UPDATE tasks SET status=?,updated_at=?,error=?,build_id=? WHERE id=?",
                     (status, now(), error, build_id, task_id))

    def event(self, task_id: str, event_type: str, data: dict):
        self.execute("INSERT INTO events(task_id,type,data,created_at) VALUES(?,?,?,?)",
                     (task_id, event_type, json.dumps(data, ensure_ascii=False), now()))

    def events(self, task_id: str, after: int = 0) -> list[dict]:
        rows = self.rows("SELECT seq,type,data,created_at FROM events WHERE task_id=? AND seq>? ORDER BY seq LIMIT 100",
                         (task_id, after))
        for row in rows:
            row["data"] = json.loads(row["data"])
        return rows

    def build(self, build_id: str) -> dict | None:
        row = self.one("SELECT * FROM builds WHERE id=?", (build_id,))
        return self.public_build(row) if row else None

    @staticmethod
    def public_build(row: dict) -> dict:
        return {"id": row["id"], "project_id": row["project_id"], "task_id": row["task_id"],
                "created_at": row["created_at"], "preview_url": f'/builds/{row["id"]}/{row["entry"]}'}

    def builds(self, project_id: str) -> list[dict]:
        return [self.public_build(row) for row in self.rows(
            "SELECT * FROM builds WHERE project_id=? ORDER BY created_at DESC", (project_id,))]
