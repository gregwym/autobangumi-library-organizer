import asyncio
import time
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from library_organizer.bridge40 import Bridge40
from library_organizer.engine import Engine
from library_organizer.planner import fingerprint
from library_organizer.store import Store

pytest_plugins = ["test_engine", "test_entry_scope"]


def inbox(engine):
    engine.bridge.notify_inbox = AsyncMock(side_effect=Bridge40.notify_inbox.__get__(engine.bridge))
    return engine.bridge.notify_inbox


async def messages():
    from module.database import Database

    async with Database() as db:
        return await db.inbox.list()


async def organize(engine):
    return await engine.apply((await engine.preview())["token"])


async def test_upgrade_baseline_suppresses_old_downloads_and_renames(env):
    engine, task, _ = env
    notify = inbox(engine)
    await organize(engine)
    engine.store.bind(task["key"], {"tmdb_id": 1, "season": 2})
    await organize(engine)
    notify.assert_not_called()
    assert await messages() == []


async def test_new_episode_persists_native_inbox_once_after_verified_commit(env, monkeypatch):
    from module.notification.manager import NotificationManager

    engine, task, source = env
    inbox(engine)
    external = AsyncMock()
    monkeypatch.setattr(NotificationManager, "send_event", external)
    task["completed_at"] = time.time() + 1
    await organize(engine)
    rows = await messages()
    assert len(rows) == 1
    assert rows[0].title == "新集已入库"
    assert "Show · S01E01" in rows[0].body
    target = Path(engine.store.files()[0]["target"])
    assert target.read_bytes() == source.read_bytes()
    assert engine.store.completed(
        {"key": task["key"] + ":0", "target": str(target), "fingerprint": fingerprint(source)}
    )
    restarted = Engine(Store(engine.store.path), engine.bridge, engine.options)
    await organize(restarted)
    await asyncio.gather(restarted.notifications.flush(), restarted.notifications.flush())
    assert len(await messages()) == 1
    external.assert_not_called()  # Inbox-only; no configured external notification channel.


async def test_notification_failure_retries_after_restart_even_if_automatic_disabled(env):
    engine, task, _ = env
    task["completed_at"] = time.time() + 1
    engine.bridge.notify_inbox = AsyncMock(side_effect=OSError("inbox unavailable"))
    result = await organize(engine)
    assert result["results"][0]["state"] == "linked"
    assert await messages() == []
    restarted = Engine(Store(engine.store.path), engine.bridge, engine.options)
    inbox(restarted)
    await restarted.tick()
    assert len(await messages()) == 1
    await restarted.tick()
    assert len(await messages()) == 1


async def test_inbox_commit_before_ack_is_deduplicated_on_retry(env):
    engine, task, _ = env
    task["completed_at"] = time.time() + 1

    async def lost_ack(event):
        await Bridge40.notify_inbox(engine.bridge, event)
        raise OSError("lost ack")

    engine.bridge.notify_inbox = lost_ack
    await organize(engine)
    assert len(await messages()) == 1
    inbox(engine)
    await engine.notifications.flush()
    assert len(await messages()) == 1


async def test_failed_or_incomplete_video_never_notifies_then_success_does(env, monkeypatch):
    engine, task, _ = env
    notify = inbox(engine)
    task["files"][0]["progress"] = 0.5
    assert (await organize(engine))["results"][0]["state"] == "held"
    notify.assert_not_called()
    task["files"][0]["progress"] = 1
    import library_organizer.engine as module

    publish = module.publish
    monkeypatch.setattr(
        module, "publish", lambda *args: (_ for _ in ()).throw(PermissionError("denied"))
    )
    assert (await organize(engine))["results"][0]["state"] == "failed"
    notify.assert_not_called()
    monkeypatch.setattr(module, "publish", publish)
    await organize(engine)
    assert len(await messages()) == 1


async def test_future_task_after_initial_scan_notifies_and_attachments_do_not(env):
    engine, task, source = env
    inbox(engine)
    await organize(engine)  # Historical baseline.
    task["key"] = "b" * 32
    task["completed_at"] = time.time() + 1
    engine.store.bind(task["key"], {"tmdb_id": 1, "season": 2})
    extra = source.parent / "fonts.txt"
    extra.write_text("font info")
    task["files"].append(
        {
            "index": 1,
            "name": extra.name,
            "source": str(extra),
            "progress": 1,
            "fingerprint": fingerprint(extra),
        }
    )
    await organize(engine)
    assert len(await messages()) == 1
    assert "S02E01" in (await messages())[0].body


async def test_cancelled_apply_keeps_notification_outbox(env, monkeypatch):
    engine, task, _ = env
    task["completed_at"] = time.time() + 1
    inbox(engine)
    original = engine.notifications.flush
    engine.notifications.flush = AsyncMock(side_effect=asyncio.CancelledError)
    with pytest.raises(asyncio.CancelledError):
        await organize(engine)
    assert engine.store.files()[0]["state"] == "linked"
    engine.notifications.flush = original
    await engine.notifications.flush()
    assert len(await messages()) == 1


async def test_incremental_download_through_real_bridge_notifies_only_after_completion(scoped):
    x = scoped
    await x.engine.tick()
    assert await messages() == []
    info, file = x.add("c", "GroupA", 2, progress=0.5)
    await x.record("c", x.a.id)
    await x.engine.tick()
    assert await messages() == []
    info["progress"] = file["progress"] = 1
    info["completion_on"] = int(time.time()) + 1
    await x.engine.tick()
    rows = await messages()
    assert len(rows) == 1 and "S03E02" in rows[0].body
    await x.engine.tick()
    assert len(await messages()) == 1


async def test_first_scan_with_known_old_completion_time_suppresses_backlog(env):
    engine, task, _ = env
    inbox(engine)
    task["completed_at"] = engine.store.get("notification_since") - 3600
    await organize(engine)
    assert await messages() == []
