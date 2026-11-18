import asyncio
import errno
import os
import threading
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from library_organizer.engine import Engine
from library_organizer.models import Options
from library_organizer.planner import fingerprint
from library_organizer.store import Store


@pytest.fixture
def env(tmp_path):
    src = tmp_path / "source"
    src.mkdir()
    media = src / "Show - 01.mkv"
    media.write_bytes(b"abc")
    opts = Options(source_root=str(src), library_root=str(tmp_path / "library"))
    store = Store(tmp_path / "state/library.sqlite3")
    store.set("active", True)
    task = {
        "key": "a" * 32,
        "name": "Show",
        "rule_id": 1,
        "files": [
            {
                "name": media.name,
                "source": str(media),
                "fingerprint": fingerprint(media),
                "progress": 1,
                "index": 0,
            }
        ],
    }
    store.bind(task["key"], {"media_type": "tv", "tmdb_id": 1, "season": 1, "episode_offset": 0})
    bridge = SimpleNamespace(
        scan=AsyncMock(return_value=[task]),
        metadata=AsyncMock(return_value={"title": "Show", "episodes": [1]}),
        health=lambda: {"native_rename": False, "native_hardlink": False},
        refresh_jellyfin=AsyncMock(return_value="ok"),
    )
    return Engine(store, bridge, opts), task, media


async def test_real_hardlink_idempotence_and_preview_replay(env):
    engine, task, src = env
    p = await engine.preview()
    dst = Path(p["items"][0]["target"])
    assert not dst.exists()
    result = await engine.apply(p["token"])
    assert result["results"][0]["state"] == "linked"
    assert os.path.samefile(src, dst)
    assert src.name == "Show - 01.mkv"
    with pytest.raises(ValueError):
        await engine.apply(p["token"])
    p = await engine.preview()
    await engine.apply(p["token"])
    assert os.path.samefile(src, dst)


async def test_progress_is_readable_during_copy_and_survives_page_reload(env, monkeypatch):
    e, task, src = env
    plan = await e.preview(rule_id=1)
    entered, resume = threading.Event(), threading.Event()
    original = e.link

    def blocked(item):
        entered.set()
        assert resume.wait(5)
        return original(item)

    monkeypatch.setattr(e, "link", blocked)
    applying = asyncio.create_task(e.apply(plan["token"], rule_id=1))
    try:
        assert await asyncio.to_thread(entered.wait, 5)
        p = e.progress(1)
        assert p["state"] == "running" and p["completed"] == 0 and p["total"] == 1
        assert p["current"] == src.name
        assert e.progress(2) is None
        p["counts"]["copy"] = 99
        assert not e.progress(1)["counts"]
    finally:
        resume.set()
    await applying
    p = e.progress(1)
    assert p["state"] == "completed" and p["completed"] == 1
    assert p["counts"] == {"hardlink": 1}
    assert Engine(e.store, e.bridge, e.options).progress(1) == p


async def test_interrupted_progress_is_not_reported_as_running(env, monkeypatch):
    e, task, src = env
    plan = await e.preview()

    def crash(item):
        raise RuntimeError("unexpected failure")

    monkeypatch.setattr(e, "link", crash)
    with pytest.raises(RuntimeError):
        await e.apply(plan["token"])
    assert e.progress()["state"] == "interrupted"
    e.store.set("progress", {**e.progress(), "state": "running"})
    assert Engine(e.store, e.bridge, e.options).progress()["state"] == "interrupted"


async def test_mapping_change_rebuilds_owned_links_only(env):
    e, t, s = env
    p = await e.preview()
    old = Path(p["items"][0]["target"])
    await e.apply(p["token"])
    e.store.bind(t["key"], {"tmdb_id": 1, "season": 2})
    p = await e.preview()
    new = Path(p["items"][0]["target"])
    assert str(old) in p["items"][0]["old_targets"]
    await e.apply(p["token"])
    assert not old.exists()
    assert os.path.samefile(new, s)


async def test_unmanaged_target_is_only_adopted_after_fresh_review(env):
    e, t, s = env
    p = await e.preview()
    old = Path(p["items"][0]["target"])
    old.parent.mkdir(parents=True)
    os.link(s, old)
    result = await e.apply(p["token"])
    assert result["results"][0]["state"] == "exists"
    assert old.exists() and not e.store.owned()
    e.store.bind(t["key"], {"tmdb_id": 1, "season": 2})
    p = await e.preview()
    assert p["items"][0]["operation"] == "adopt"
    assert p["items"][0]["existing_paths"] == [str(old)]
    assert old.exists() and not e.store.owned()
    await e.apply(p["token"])
    assert not old.exists() and os.path.samefile(s, p["items"][0]["target"])


async def test_modified_owned_path_is_preserved(env):
    e, t, s = env
    p = await e.preview()
    old = Path(p["items"][0]["target"])
    await e.apply(p["token"])
    old.unlink()
    old.write_bytes(b"user replacement")
    e.store.bind(t["key"], {"tmdb_id": 1, "season": 2})
    p = await e.preview()
    await e.apply(p["token"])
    assert old.read_bytes() == b"user replacement"


async def test_foreign_target_never_overwritten(env):
    e, t, s = env
    p = await e.preview()
    dst = Path(p["items"][0]["target"])
    dst.parent.mkdir(parents=True)
    dst.write_bytes(b"other")
    result = await e.apply(p["token"])
    assert result["results"][0]["state"] == "failed"
    assert dst.read_bytes() == b"other"


async def test_stale_binding_rejected(env):
    e, t, s = env
    p = await e.preview()
    e.store.bind(t["key"], {"tmdb_id": 2})
    with pytest.raises(ValueError, match="映射已变化"):
        await e.apply(p["token"])


async def test_changed_source_no_link(env):
    e, t, s = env
    p = await e.preview()
    s.write_bytes(b"changed")
    r = await e.apply(p["token"])
    assert r["results"][0]["state"] == "failed"
    assert not Path(p["items"][0]["target"]).exists()


def cross_device(monkeypatch, source, method):
    import shutil

    from library_organizer import transfer

    real_link = os.link

    def link(src, dst, **kw):
        if Path(src) == source:
            raise OSError(errno.EXDEV, "different mounts")
        return real_link(src, dst, **kw)

    def clone(src, dst):
        if method == "copy":
            Path(dst).write_bytes(b"partial clone")
            raise OSError(errno.EOPNOTSUPP, "clone unsupported")
        shutil.copyfile(src, dst)  # simulate successful ioctl on non-Linux test hosts

    monkeypatch.setattr(os, "link", link)
    monkeypatch.setattr(transfer, "reflink", clone)


@pytest.mark.parametrize("method", ["reflink", "copy"])
async def test_fallback_idempotence_restart_and_mapping_change(env, monkeypatch, method):
    e, t, s = env
    cross_device(monkeypatch, s, method)
    p = await e.preview()
    old = Path(p["items"][0]["target"])
    r = await e.apply(p["token"])
    assert r["results"][0]["method"] == method
    assert old.read_bytes() == s.read_bytes()
    assert not os.path.samefile(old, s)
    original_fp = fingerprint(old)
    e = Engine(Store(e.store.path), e.bridge, e.options)
    p = await e.preview()
    assert p["held"] == 0
    r = await e.apply(p["token"])
    assert r["results"][0]["method"] == method
    assert fingerprint(old) == original_fp
    e.store.bind(t["key"], {"tmdb_id": 1, "season": 2})
    p = await e.preview()
    r = await e.apply(p["token"])
    assert r["results"][0]["action"] == "relocated"
    assert e.progress()["counts"] == {"reused": 1}
    assert not old.exists()
    assert fingerprint(Path(p["items"][0]["target"])) == original_fp
    assert Path(p["items"][0]["target"]).read_bytes() == s.read_bytes()


@pytest.mark.parametrize("error", [errno.ENOSPC, errno.EACCES, errno.EIO])
async def test_reflink_real_failure_does_not_copy(env, monkeypatch, error):
    from library_organizer import transfer

    e, t, s = env
    cross_device(monkeypatch, s, "reflink")

    def fail(src, dst):
        Path(dst).write_bytes(b"partial")
        raise OSError(error, "disk or permission failure")

    monkeypatch.setattr(transfer, "reflink", fail)
    monkeypatch.setattr(
        transfer.shutil, "copyfileobj", lambda *a, **kw: pytest.fail("must not copy")
    )
    p = await e.preview()
    r = await e.apply(p["token"])
    dst = Path(p["items"][0]["target"])
    assert r["results"][0]["state"] == "failed"
    assert not dst.exists()
    assert not list(dst.parent.glob(".ab-organizer-*"))


async def test_copy_disk_full_removes_partial_file(env, monkeypatch):
    from library_organizer import transfer

    e, t, s = env
    cross_device(monkeypatch, s, "copy")

    def fail(source, target, **kw):
        target.write(b"partial")
        raise OSError(errno.ENOSPC, "full")

    monkeypatch.setattr(transfer.shutil, "copyfileobj", fail)
    p = await e.preview()
    r = await e.apply(p["token"])
    dst = Path(p["items"][0]["target"])
    assert r["results"][0]["state"] == "failed"
    assert not dst.exists()
    assert not list(dst.parent.glob(".ab-organizer-*"))


async def test_target_created_during_copy_never_overwritten(env, monkeypatch):
    e, t, s = env
    cross_device(monkeypatch, s, "copy")
    p = await e.preview()
    dst = Path(p["items"][0]["target"])
    claim = e.store.claim

    def race(*a, **kw):
        claim(*a, **kw)
        dst.write_bytes(b"another writer")

    monkeypatch.setattr(e.store, "claim", race)
    r = await e.apply(p["token"])
    assert r["results"][0]["state"] == "failed"
    assert dst.read_bytes() == b"another writer"
    p = await e.preview()
    assert p["held"] == 1


async def test_copy_modified_source_is_not_published(env, monkeypatch):
    from library_organizer import transfer

    e, t, s = env
    cross_device(monkeypatch, s, "copy")
    original = transfer.shutil.copyfileobj

    def changing(source, target, **kw):
        original(source, target, **kw)
        s.write_bytes(b"changed")

    monkeypatch.setattr(transfer.shutil, "copyfileobj", changing)
    p = await e.preview()
    r = await e.apply(p["token"])
    assert r["results"][0]["state"] == "failed"
    assert not Path(p["items"][0]["target"]).exists()


async def test_restart_recovers_published_copy_intent(env, monkeypatch):
    e, t, s = env
    cross_device(monkeypatch, s, "copy")
    p = await e.preview()
    real_record = e.store.record

    def fail_after_publish(item, state, detail="", **kwargs):
        if state == "linked":
            raise RuntimeError("simulate process death")
        real_record(item, state, detail, **kwargs)

    monkeypatch.setattr(e.store, "record", fail_after_publish)
    with pytest.raises(RuntimeError):
        await e.apply(p["token"])
    dst = Path(p["items"][0]["target"])
    fp = fingerprint(dst)
    e = Engine(Store(e.store.path), e.bridge, e.options)
    p = await e.preview()
    r = await e.apply(p["token"])
    assert r["results"][0]["method"] == "copy"
    assert fingerprint(dst) == fp


async def test_native_pipeline_blocks_execution(env):
    e, t, s = env
    p = await e.preview()
    e.bridge.health = lambda: {"native_rename": True, "native_hardlink": False}
    with pytest.raises(ValueError, match="原生"):
        await e.apply(p["token"])


async def test_restart_recovers_link_intent(env):
    e, t, s = env
    p = await e.preview()
    i = p["items"][0]
    dst = Path(i["target"])
    dst.parent.mkdir(parents=True)
    e.store.claim(i)
    os.link(s, dst)  # crash between filesystem mutation and result record
    restarted = Engine(Store(e.store.path), e.bridge, e.options)
    p = await restarted.preview()
    await restarted.apply(p["token"])
    assert restarted.store.files()[0]["state"] == "linked"


async def test_automatic_conflict_does_not_repeat(env):
    e, t, s = env
    e.store.set("automatic", True)
    e.store.bind(t["key"], {"tmdb_id": 1, "season": 1, "episode_offset": 10})
    await e.tick()
    count = len(list((e.store.path.parent / "backups").iterdir()))
    await e.tick()
    assert len(list((e.store.path.parent / "backups").iterdir())) == count
    assert e.store.get("last_results")[0]["state"] == "held"


async def test_rule_plan_does_not_include_other_show_or_invalidate_on_its_change(env):
    import copy

    e, t, src = env
    other = copy.deepcopy(t)
    other["key"] = "b" * 32
    other["rule_id"] = 2
    e.bridge.scan.return_value = [t, other]
    p = await e.preview(rule_id=1)
    assert {i["task"] for i in p["items"]} == {t["key"]}
    other["name"] = "Other changed while previewing"
    r = await e.apply(p["token"], rule_id=1)
    assert len(r["results"]) == 1
    assert all(row["key"].startswith(t["key"]) for row in e.store.files())


async def test_rule_token_cannot_execute_for_other_rule(env):
    e, t, src = env
    p = await e.preview(rule_id=1)
    with pytest.raises(ValueError, match="不属于当前番剧"):
        await e.apply(p["token"], rule_id=2)
    assert e.store.files() == []


async def test_future_download_automatically_inherits_rule_mapping(env):
    import copy

    e, t, src = env
    e.store.unbind(t["key"])
    e.store.bind("rule:1", {"tmdb_id": 8, "season": 2})
    e.store.set("automatic", True)
    await e.tick()
    new = copy.deepcopy(t)
    new["key"] = "b" * 32
    next_file = src.with_name("Show - 02.mkv")
    next_file.write_bytes(b"next episode")
    new["files"][0].update(
        name=next_file.name, source=str(next_file), fingerprint=fingerprint(next_file)
    )
    e.bridge.metadata.return_value = {"title": "Show", "episodes": [1, 2]}
    e.bridge.scan.return_value = [t, new]
    await e.tick()
    assert any(
        row["key"].startswith(new["key"]) and row["state"] == "linked" for row in e.store.files()
    )


async def test_relocated_copy_recovers_after_final_bookkeeping_crash(env, monkeypatch):
    e, task, source = env
    cross_device(monkeypatch, source, "copy")
    plan = await e.preview()
    await e.apply(plan["token"])
    old = Path(plan["items"][0]["target"])
    original_fp = fingerprint(old)
    e.store.bind(task["key"], {"tmdb_id": 1, "season": 2})
    plan = await e.preview()

    def crash(*args, **kwargs):
        raise RuntimeError("process stopped after old path removal")

    monkeypatch.setattr(e.store, "record", crash)
    with pytest.raises(RuntimeError):
        await e.apply(plan["token"])
    assert not old.exists()
    assert len(e.store.owned()) == 2
    e = Engine(Store(e.store.path), e.bridge, e.options)
    plan = await e.preview()
    assert plan["held"] == 0
    await e.apply(plan["token"])
    assert len(e.store.owned()) == 1
    assert fingerprint(Path(plan["items"][0]["target"])) == original_fp


@pytest.mark.parametrize("native_record", [False, True])
async def test_native_replica_is_previewed_then_renamed_without_copy(
    env, monkeypatch, native_record
):
    from library_organizer import legacy

    e, task, src = env
    old = Path(e.options.library_root) / src.name
    old.parent.mkdir()
    old.write_bytes(src.read_bytes())
    original = fingerprint(old)
    layout = {"records": {str(old): [fingerprint(src), original]}} if native_record else {}
    e.bridge.legacy_layout = AsyncMock(return_value=layout)
    if native_record:
        monkeypatch.setattr(
            legacy, "content_hash", lambda *a: pytest.fail("Recorded identity needs no rehash")
        )
    p = await e.preview(automatic=True)
    item = p["items"][0]
    assert item["operation"] == "adopt" and item["existing_paths"] == [str(old)]
    assert old.exists() and not Path(item["target"]).exists() and not e.store.owned()
    monkeypatch.setattr(
        "library_organizer.engine.publish",
        lambda *a: pytest.fail("Must reuse the existing replica"),
    )
    result = await e.apply(p["token"], enable_automatic=True)
    assert result["automatic"] is True
    assert result["results"][0]["action"] == "relocated"
    assert not old.exists() and fingerprint(Path(item["target"])) == original
    assert src.exists()
    p = await e.preview(automatic=True)
    assert p["items"][0]["operation"] == "unchanged"
    assert p["counts"] == {"unchanged": 1}


async def test_native_output_at_final_target_is_adopted_without_copy(env):
    e, task, src = env
    p = await e.preview()
    dst = Path(p["items"][0]["target"])
    dst.parent.mkdir(parents=True)
    dst.write_bytes(src.read_bytes())
    fp = fingerprint(dst)
    p = await e.preview()
    assert p["items"][0]["operation"] == "adopt_existing"
    await e.apply(p["token"])
    assert fingerprint(dst) == fp and e.store.completed(p["items"][0])


async def test_different_native_version_is_not_reused(env):
    e, task, src = env
    old = Path(e.options.library_root) / src.name
    old.parent.mkdir()
    old.write_bytes(b"xyz")  # Same size, different content.
    p = await e.preview()
    assert p["items"][0]["operation"] == "create"
    await e.apply(p["token"])
    assert old.read_bytes() == b"xyz"


async def test_multiple_native_paths_are_held_without_adoption(env):
    e, task, src = env
    root = Path(e.options.library_root)
    root.mkdir()
    paths = [root / src.name, root / "old-name.mkv"]
    for path in paths:
        path.write_bytes(src.read_bytes())
    e.bridge.legacy_layout = AsyncMock(
        return_value={
            "records": {str(path): [fingerprint(src), fingerprint(path)] for path in paths}
        }
    )
    p = await e.preview()
    assert p["held"] == 1 and p["counts"] == {"held": 1}
    assert set(p["items"][0]["existing_paths"]) == {str(p) for p in paths}
    await e.apply(p["token"])
    assert all(p.exists() for p in paths) and not e.store.owned()


async def test_existing_replica_changed_after_preview_never_triggers_new_copy(env):
    e, task, src = env
    old = Path(e.options.library_root) / src.name
    old.parent.mkdir()
    old.write_bytes(src.read_bytes())
    p = await e.preview()
    old.write_bytes(b"user changed this file")
    result = await e.apply(p["token"])
    assert result["results"][0]["state"] == "failed"
    assert old.exists() and not Path(p["items"][0]["target"]).exists()


async def test_automatic_enable_requires_complete_valid_preview(env):
    e, task, src = env
    p = await e.preview(rule_id=1)
    with pytest.raises(ValueError):
        await e.apply(p["token"], rule_id=1, enable_automatic=True)
    assert not e.store.get("automatic", False)
    p = await e.preview(automatic=True)
    e.options.version_suffix = False
    with pytest.raises(ValueError, match="配置已变化"):
        await e.apply(p["token"], enable_automatic=True)
    assert not e.store.get("automatic", False)
    p = await e.preview(automatic=True)
    assert p["counts"] == {"create": 1}
    assert not e.store.get("automatic", False)
    await e.apply(p["token"], enable_automatic=True)
    assert e.store.get("automatic")


async def test_removed_owned_replica_is_skipped_in_automatic_preview(env):
    e, task, src = env
    p = await e.preview()
    await e.apply(p["token"])
    Path(p["items"][0]["target"]).unlink()
    p = await e.preview(automatic=True)
    assert p["items"][0]["operation"] == "skipped"


async def test_native_deleted_replica_is_not_resurrected_automatically(env):
    e, task, src = env
    old = Path(e.options.library_root) / src.name
    e.bridge.legacy_layout = AsyncMock(
        return_value={"records": {str(old): [fingerprint(src), [1, 2, 3, 4]]}}
    )
    p = await e.preview(automatic=True)
    assert p["items"][0]["operation"] == "skipped"
    r = await e.apply(p["token"])
    assert r["results"][0]["state"] == "skipped"
    assert not Path(p["items"][0]["target"]).exists()
    p = await e.preview()
    assert p["items"][0]["operation"] == "create"


async def test_preview_never_adopts_file_owned_by_another_entry(env):
    e, task, src = env
    old = Path(e.options.library_root) / src.name
    old.parent.mkdir()
    old.write_bytes(src.read_bytes())
    e.store.claim(
        {"key": "other-entry-file", "target": str(old), "fingerprint": fingerprint(src)},
        fingerprint(old),
        "existing",
    )
    p = await e.preview(rule_id=1)
    assert p["items"][0]["operation"] == "create"
    await e.apply(p["token"], rule_id=1)
    assert (
        old.exists()
        and next(r for r in e.store.owned() if r["target"] == str(old))["key"] == "other-entry-file"
    )
