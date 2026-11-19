"""Durable, inbox-only notices after a verified episode commit."""

import asyncio
import json
import logging
import time
from dataclasses import dataclass
from pathlib import Path
from typing import ClassVar

from ab_sdk.events import SystemEvent


@dataclass(frozen=True, slots=True)
class EpisodeReady(SystemEvent):
    kind: ClassVar[str] = "library-organizer.episode-ready"
    once: ClassVar[bool] = True
    file_key: str
    title: str
    season: int
    episode: int
    target: str

    def dedup_key(self):
        return self.kind + ":" + self.file_key

    def describe(self):
        return (
            "新集已入库",
            f"{self.title} · S{self.season:02d}E{self.episode:02d}\n{Path(self.target).name}",
        )


class Notifications:
    def __init__(self, store, bridge, audit):
        self.store, self.bridge, self.audit = store, bridge, audit
        self.lock = asyncio.Lock()
        if self.store.get("notification_since") is None:
            self.store.set("notification_since", time.time())

    def observe(self, tasks, *, full=False):
        """First scan baselines old downloads; completion timestamps cover startup races."""
        initialized = self.store.get("notification_initialized", False)
        since = self.store.get("notification_since")
        with self.store.db() as db:
            for task in tasks:
                completed_at = task.get("completed_at", 0)
                for file in task["files"]:
                    key = task["key"] + ":" + str(file["index"])
                    old = db.execute(
                        "SELECT 1 FROM files WHERE key=? AND state='linked'", (key,)
                    ).fetchone()
                    eligible = not old and (
                        completed_at >= int(since)
                        if completed_at > 0
                        else initialized or file.get("progress", 0) < 1
                    )
                    db.execute(
                        "INSERT OR IGNORE INTO notice_files VALUES (?,?)", (key, int(eligible))
                    )
        if full and not initialized:
            self.store.set("notification_initialized", True)
            self.audit.emit(
                "notification.baseline", tasks=len(tasks), detail="既有完整下载不补发通知"
            )

    async def flush(self):
        async with self.lock:
            await self._flush()

    async def _flush(self):
        # Await host persistence, rather than marking a fire-and-forget bus publish delivered.
        if not hasattr(self.bridge, "notify_inbox"):
            return
        with self.store.db() as db:
            rows = db.execute(
                "SELECT * FROM notice_outbox WHERE delivered=0 ORDER BY rowid LIMIT 100"
            ).fetchall()
        for row in rows:
            event = EpisodeReady(**json.loads(row["payload"]))
            try:
                message_id = await self.bridge.notify_inbox(event)
                with self.store.db() as db:
                    db.execute("UPDATE notice_outbox SET delivered=1 WHERE key=?", (row["key"],))
                self.audit.emit(
                    "notification.delivered",
                    key=row["key"],
                    target=event.target,
                    message_id=message_id,
                )
            except Exception as error:
                self.audit.emit(
                    "notification.retry",
                    level=logging.WARNING,
                    key=row["key"],
                    error=type(error).__name__,
                    detail=str(error),
                )
                break
