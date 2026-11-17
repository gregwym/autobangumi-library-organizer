"""Plugin-owned state. Never stores completion in downloader tags/categories."""

import json
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path


class Store:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.db() as c:
            c.executescript("""
              PRAGMA journal_mode=WAL;
              CREATE TABLE IF NOT EXISTS kv(key TEXT PRIMARY KEY,value TEXT NOT NULL);
              CREATE TABLE IF NOT EXISTS tasks(key TEXT PRIMARY KEY,payload TEXT NOT NULL);
              CREATE TABLE IF NOT EXISTS bindings(key TEXT PRIMARY KEY,payload TEXT NOT NULL);
              CREATE TABLE IF NOT EXISTS files(key TEXT PRIMARY KEY,source TEXT,target TEXT,fingerprint TEXT,state TEXT,detail TEXT,updated REAL);
              CREATE TABLE IF NOT EXISTS owned(target TEXT PRIMARY KEY,key TEXT,fingerprint TEXT);
              CREATE TABLE IF NOT EXISTS plans(token TEXT PRIMARY KEY,payload TEXT NOT NULL,created REAL,used INTEGER DEFAULT 0);
            """)

    @contextmanager
    def db(self):
        c = sqlite3.connect(self.path, timeout=15)
        c.row_factory = sqlite3.Row
        try:
            yield c
            c.commit()
        finally:
            c.close()

    def get(self, key, default=None):
        with self.db() as c:
            r = c.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
        return json.loads(r[0]) if r else default

    def set(self, key, value):
        with self.db() as c:
            c.execute("INSERT OR REPLACE INTO kv VALUES (?,?)", (key, json.dumps(value)))

    def save_task(self, task):
        with self.db() as c:
            c.execute("INSERT OR REPLACE INTO tasks VALUES (?,?)", (task["key"], json.dumps(task)))

    def tasks(self):
        with self.db() as c:
            return [json.loads(r[0]) for r in c.execute("SELECT payload FROM tasks ORDER BY key")]

    def bind(self, key, mapping):
        with self.db() as c:
            c.execute("INSERT OR REPLACE INTO bindings VALUES (?,?)", (key, json.dumps(mapping)))

    def binding(self, task):
        keys = [task["key"]]
        if task.get("rule_id") is not None:
            keys.append("rule:" + str(task["rule_id"]))
        with self.db() as c:
            for key in keys:
                r = c.execute("SELECT payload FROM bindings WHERE key=?", (key,)).fetchone()
                if r:
                    return json.loads(r[0])
        return None

    def record(self, item, state, detail=""):
        with self.db() as c:
            c.execute(
                "INSERT OR REPLACE INTO files VALUES (?,?,?,?,?,?,?)",
                (
                    item["key"],
                    item["source"],
                    item.get("target"),
                    json.dumps(item.get("fingerprint")),
                    state,
                    detail,
                    time.time(),
                ),
            )

    def files(self):
        with self.db() as c:
            return [dict(r) for r in c.execute("SELECT * FROM files ORDER BY updated DESC")]

    def plan(self, token, payload):
        with self.db() as c:
            c.execute("DELETE FROM plans WHERE created<? OR used=1", (time.time() - 900,))
            c.execute(
                "INSERT INTO plans VALUES (?,?,?,0)", (token, json.dumps(payload), time.time())
            )

    def take_plan(self, token):
        with self.db() as c:
            r = c.execute("SELECT * FROM plans WHERE token=?", (token,)).fetchone()
            if not r or r["used"] or time.time() - r["created"] > 900:
                raise ValueError("预览已过期，请重新预览")
            c.execute("UPDATE plans SET used=1 WHERE token=?", (token,))
        return json.loads(r["payload"])

    def backup(self):
        out = self.path.parent / "backups" / str(time.time_ns())
        out.mkdir(parents=True)
        with self.db() as src, sqlite3.connect(out / self.path.name) as dst:
            src.backup(dst)
        return out

    def owned(self, key=None):
        with self.db() as c:
            rows = (
                c.execute("SELECT * FROM owned WHERE key=?", (key,))
                if key
                else c.execute("SELECT * FROM owned")
            )
            return [dict(r) for r in rows]

    def claim(self, item):
        with self.db() as c:
            c.execute(
                "INSERT OR IGNORE INTO owned VALUES (?,?,?)",
                (item["target"], item["key"], json.dumps(item["fingerprint"])),
            )

    def forget(self, target):
        with self.db() as c:
            c.execute("DELETE FROM owned WHERE target=?", (target,))

    def unbind(self, key):
        with self.db() as c:
            c.execute("DELETE FROM bindings WHERE key=?", (key,))

    def direct_binding(self, key):
        with self.db() as c:
            row = c.execute("SELECT payload FROM bindings WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else None
