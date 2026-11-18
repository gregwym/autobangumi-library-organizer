from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from library_organizer.bridge40 import Bridge40
from library_organizer.models import Options
from library_organizer.store import Store


async def test_legacy_layout_reads_only_native_hardlink_records(tmp_path):
    from module.conf import settings
    from module.database import Database

    settings.plugins.options["hardlink"] = {"source_root": "/downloads", "library_root": "/library"}
    async with Database() as db:
        await db.plugin_kv.set("hardlink", "link:/library/old.mkv", [[1, 2, 3, 4], [1, 5, 3, 4]])
        await db.plugin_kv.set("other", "link:/library/foreign.mkv", [[1, 2, 3, 4], [1, 6, 3, 4]])
    bridge = Bridge40(Options(), Store(tmp_path / "state.sqlite3"))
    layout = await bridge.legacy_layout()
    assert layout == {
        "source_root": "/downloads",
        "library_root": "/library",
        "records": {"/library/old.mkv": [[1, 2, 3, 4], [1, 5, 3, 4]]},
    }


async def test_missing_download_mount_fails_before_querying_downloader(tmp_path, monkeypatch):
    from module import downloader

    def unexpected_query(*args):
        pytest.fail("Missing mount must be detected before contacting the downloader")

    monkeypatch.setattr(downloader, "DownloadClient", unexpected_query)
    bridge = Bridge40(
        Options(source_root=str(tmp_path / "unmounted"), library_root=str(tmp_path / "library")),
        Store(tmp_path / "state.sqlite3"),
    )
    with pytest.raises(ValueError, match="尚未挂载"):
        await bridge.scan()


async def test_scan_uses_only_read_apis_and_ignores_tags(tmp_path, monkeypatch):
    from module import downloader
    from module.database import Database
    from module.downloader import download_client
    from module.models import Bangumi, Torrent

    src = tmp_path / "downloads"
    src.mkdir()
    (src / "Show - 01.mkv").write_bytes(b"abc")
    opts = Options(source_root=str(src), library_root=str(tmp_path / "library"))
    async with Database() as db:
        rule = Bangumi(official_title="Show", title_raw="Show", season=1, deleted=False)
        db.add(rule)
        await db.commit()
        db.add(Torrent(bangumi_id=rule.id, qb_hash="a" * 40, downloader_id="default"))
        await db.commit()
    calls = []

    class Client:
        def __init__(self, instance):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            pass

        async def get_torrent_info(self, **kw):
            calls.append(kw)
            return [
                {
                    "hash": "a" * 40,
                    "name": "Show - 01",
                    "save_path": str(src),
                    "progress": 1,
                    "tags": "ab:999,ab:renamed",
                }
            ]

        async def get_torrent_files(self, task):
            return [{"index": 0, "name": "Show - 01.mkv", "size": 3, "progress": 1}]

    monkeypatch.setattr(downloader, "DownloadClient", Client)
    monkeypatch.setattr(download_client, "downloader_ids", lambda: ["default"])
    bridge = Bridge40(opts, Store(tmp_path / "state.sqlite3"))
    tasks = await bridge.scan()
    assert calls == [{"category": None, "status_filter": "completed", "tag": None}]
    assert tasks[0]["rule_id"] == rule.id
    assert tasks[0]["files"][0]["fingerprint"]
    assert "tags" not in tasks[0]

    async def unexpected(self, task):
        pytest.fail("Other rules must not fetch file lists")

    monkeypatch.setattr(Client, "get_torrent_files", unexpected)
    assert await bridge.scan(rule_id=rule.id + 1000) == []


async def test_native_guard_restores_and_version_gates(tmp_path, monkeypatch):
    from module import conf
    from module.manager.renamer import Renamer

    original = AsyncMock(return_value=["renamed"])
    monkeypatch.setattr(Renamer, "rename", original)
    store = Store(tmp_path / "state.sqlite3")
    bridge = Bridge40(Options(), store)
    bridge.setup()
    assert await Renamer.rename(None) == ["renamed"]
    store.set("active", True)
    assert await Renamer.rename(None) == []
    assert original.await_count == 1
    bridge.teardown()
    assert Renamer.rename is original
    monkeypatch.setattr(conf, "VERSION", "4.1.0")
    with pytest.raises(ValueError, match="4.0.0"):
        bridge.setup()


async def test_activation_persists_safe_native_disable(tmp_path, monkeypatch):
    from module.conf import config, settings

    settings.save()
    store = Store(tmp_path / "state.sqlite3")
    bridge = Bridge40(Options(), store)
    ctx = SimpleNamespace(is_running=True)

    async def stop():
        ctx.is_running = False

    async def start():
        ctx.is_running = True

    async def reload():
        settings.load()

    ctx.stop = AsyncMock(side_effect=stop)
    ctx.start_tasks = AsyncMock(side_effect=start)
    ctx.reload_settings = AsyncMock(side_effect=reload)
    result = await bridge.activate(ctx)
    assert store.get("active")
    assert not settings.bangumi_manage.enable
    assert not settings.plugins.enabled["hardlink"]
    assert ctx.is_running
    from pathlib import Path

    assert Path(result["backup"], "config.json").exists()
    assert config.CONFIG_PATH.exists()
