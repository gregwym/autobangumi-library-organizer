import errno
import os
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


async def test_unmanaged_existing_same_inode_never_adopted(env):
    e, t, s = env
    p = await e.preview()
    old = Path(p["items"][0]["target"])
    old.parent.mkdir(parents=True)
    os.link(s, old)
    result = await e.apply(p["token"])
    assert result["results"][0]["state"] == "exists"
    e.store.bind(t["key"], {"tmdb_id": 1, "season": 2})
    p = await e.preview()
    await e.apply(p["token"])
    assert old.exists()
    assert not any(r["target"] == str(old) for r in e.store.owned())


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


async def test_cross_device_no_copy_fallback(env, monkeypatch):
    e, t, s = env
    p = await e.preview()

    def fail(*a, **kw):
        raise OSError(errno.EXDEV, "different mounts")

    monkeypatch.setattr(os, "link", fail)
    r = await e.apply(p["token"])
    assert "跨文件系统" in r["results"][0]["detail"]
    assert not Path(p["items"][0]["target"]).exists()


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
