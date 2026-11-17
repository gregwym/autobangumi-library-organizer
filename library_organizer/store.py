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
              CREATE TABLE IF NOT EXISTS transfers(target TEXT PRIMARY KEY,source_fingerprint TEXT,method TEXT);
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
        self.save_tasks([task])

    def save_tasks(self, tasks):
        with self.db() as c:
            c.executemany(
                "INSERT INTO tasks VALUES (?,?) ON CONFLICT(key) DO UPDATE SET payload=excluded.payload WHERE tasks.payload IS NOT excluded.payload",
                [(task["key"], json.dumps(task)) for task in tasks],
            )

    def scan_state(self):
        with self.db() as c:
            assignments = {
                r["key"].removeprefix("assignment:"): json.loads(r["value"])
                for r in c.execute("SELECT * FROM kv WHERE key LIKE 'assignment:%'")
            }
            bindings = {r["key"] for r in c.execute("SELECT key FROM bindings")}
        return assignments, bindings

    def tasks(self):
        with self.db() as c:
            return [json.loads(r[0]) for r in c.execute("SELECT payload FROM tasks ORDER BY key")]

    def reconcile_history(self, associations):
        """Correct cached ownership/history only; never move files or rewrite bindings."""
        with self.db() as c:
            kv = {r["key"]: json.loads(r["value"]) for r in c.execute("SELECT * FROM kv")}
            owners, changed = {}, False
            for row in c.execute("SELECT * FROM tasks").fetchall():
                task = json.loads(row["payload"])
                rid, evidence = associations.resolve(
                    task.get("instance", "default"),
                    task.get("task_id", ""),
                    kv.get("assignment:" + task["key"]),
                )
                owners[task["key"]] = rid
                changed |= task.get("rule_id") != rid
                task.update(rule_id=rid, association=evidence)
                payload = json.dumps(task)
                if payload != row["payload"]:
                    c.execute("UPDATE tasks SET payload=? WHERE key=?", (payload, task["key"]))
            if not changed:
                return
            items = kv.get("last_items", [])
            for item in items:
                item["rule_id"] = owners.get(item["task"])
            c.execute("INSERT OR REPLACE INTO kv VALUES ('last_items',?)", (json.dumps(items),))
            rows = {}
            for key, value in kv.items():
                if key.startswith("last_results:rule:"):
                    rows.update((r["key"], r) for r in value)
            rows.update((r["key"], r) for r in kv.get("last_results", []))
            grouped = {}
            for row in rows.values():
                rid = owners.get(row["key"].rsplit(":", 1)[0])
                if rid is not None:
                    grouped.setdefault(rid, []).append(row)
            c.execute("DELETE FROM kv WHERE key LIKE 'last_results:rule:%'")
            for rid, value in grouped.items():
                c.execute(
                    "INSERT INTO kv VALUES (?,?)", (f"last_results:rule:{rid}", json.dumps(value))
                )
            progress = kv.get("progress")
            if progress and progress.get("state") != "running":
                c.execute("DELETE FROM kv WHERE key='progress'")

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
                    value = json.loads(r[0])
                    if key.startswith("rule:") and value.get("follow_ab", True):
                        value.update(task.get("rule_mapping", {}))
                    return value
        return None

    def record(self, item, state, detail="", obsolete=()):
        with self.db() as c:
            for target in obsolete:
                c.execute("DELETE FROM owned WHERE target=?", (target,))
                c.execute("DELETE FROM transfers WHERE target=?", (target,))
            c.execute(
                """INSERT INTO files VALUES (?,?,?,?,?,?,?) ON CONFLICT(key) DO UPDATE SET
                source=excluded.source,target=excluded.target,fingerprint=excluded.fingerprint,
                state=excluded.state,detail=excluded.detail,updated=excluded.updated
                WHERE files.source IS NOT excluded.source OR files.target IS NOT excluded.target
                OR files.fingerprint IS NOT excluded.fingerprint OR files.state IS NOT excluded.state
                OR files.detail IS NOT excluded.detail""",
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
            query = (
                "SELECT o.*, COALESCE(t.source_fingerprint,o.fingerprint) source_fingerprint, "
                "COALESCE(t.method,'hardlink') method FROM owned o "
                "LEFT JOIN transfers t ON o.target=t.target"
            )
            rows = c.execute(query + " WHERE o.key=?", (key,)) if key else c.execute(query)
            return [dict(r) for r in rows]

    def claim(self, item, target_fingerprint=None, method="hardlink"):
        with self.db() as c:
            c.execute(
                "INSERT OR REPLACE INTO owned VALUES (?,?,?)",
                (
                    item["target"],
                    item["key"],
                    json.dumps(target_fingerprint or item["fingerprint"]),
                ),
            )
            c.execute(
                "INSERT OR REPLACE INTO transfers VALUES (?,?,?)",
                (item["target"], json.dumps(item["fingerprint"]), method),
            )

    def forget(self, target):
        with self.db() as c:
            c.execute("DELETE FROM owned WHERE target=?", (target,))
            c.execute("DELETE FROM transfers WHERE target=?", (target,))

    def unbind(self, key):
        with self.db() as c:
            c.execute("DELETE FROM bindings WHERE key=?", (key,))

    def direct_binding(self, key):
        with self.db() as c:
            row = c.execute("SELECT payload FROM bindings WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else None
