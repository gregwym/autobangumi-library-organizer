import asyncio
from pathlib import Path
from unittest.mock import Mock

import httpx2
import pytest

from library_organizer.bridge40 import Bridge40
from library_organizer.planner import fingerprint

pytest_plugins = ["test_engine"]


@pytest.mark.parametrize(
    ("server", "method", "path", "header", "value"),
    [
        ("jellyfin", "POST", "/Library/Refresh", "authorization", 'MediaBrowser Token="ab"'),
        ("emby", "POST", "/emby/Library/Refresh", "x-emby-token", "ab"),
        ("plex", "GET", "/library/sections/all/refresh", "x-plex-token", "ab"),
    ],
)
async def test_changes_refresh_through_the_native_ab_plugin(
    env, tmp_path, monkeypatch, server, method, path, header, value
):
    from module.conf import settings
    from module.plugin import host
    from module.plugin.loader import discover
    from module.plugin.manager import PluginManager

    engine, task, _ = env
    request_refresh = Bridge40.request_media_server_refresh.__get__(engine.bridge)
    engine.bridge.request_media_server_refresh = Mock(wraps=request_refresh)
    settings.plugins.options["media-server-refresh"] = {
        "server": server,
        "url": "http://media.example:8096/",
        "api_key": "ab",
        "delay": 0.05,
    }
    candidates, errors = discover(
        local_root=tmp_path / "empty", entry_point_group="test-no-entry-points"
    )
    assert errors == []
    refresh = next(c for c in candidates if c.manifest.id == "media-server-refresh")
    manager = PluginManager(settings, discover_fn=lambda: ([refresh], []))
    monkeypatch.setattr(host, "_bus", manager.bus)
    await manager.start()
    try:
        native = manager._active["media-server-refresh"].instance
        requests = []

        def record(request):
            requests.append(request)
            return httpx2.Response(204)

        native.client_factory = lambda: httpx2.AsyncClient(transport=httpx2.MockTransport(record))
        plan = await engine.preview()
        engine.bridge.request_media_server_refresh.assert_not_called()
        result = await engine.apply(plan["token"])
        target = Path(result["results"][0]["target"])
        assert target.is_file()
        engine.bridge.request_media_server_refresh.assert_called_once_with()

        # A second post-write event during the debounce is coalesced by AB.
        request_refresh()
        await manager.bus.drain()
        first_refresh = native._pending
        await asyncio.wait_for(first_refresh, 1)
        assert len(requests) == 1
        assert requests[0].method == method
        assert requests[0].url.path == path
        assert requests[0].headers[header] == value

        await engine.apply((await engine.preview())["token"])
        engine.bridge.request_media_server_refresh.assert_called_once_with()
        await manager.bus.drain()
        assert native._pending is first_refresh

        engine.store.bind(task["key"], {"tmdb_id": 1, "season": 2})
        result = await engine.apply((await engine.preview())["token"])
        assert result["results"][0]["action"] == "relocated"
        assert not target.exists()
        await manager.bus.drain()
        await asyncio.wait_for(native._pending, 1)
        assert len(requests) == 2

        # The scheduled path uses the same post-write event after new media arrives.
        file = task["files"][0]
        new_source = Path(file["source"]).with_name("Show - 02.mkv")
        new_source.write_bytes(b"def")
        task["files"].append(
            {
                **file,
                "index": 1,
                "name": new_source.name,
                "source": str(new_source),
                "fingerprint": fingerprint(new_source),
            }
        )
        engine.bridge.metadata.return_value = {"title": "Show", "episodes": [1, 2]}
        engine.store.set("automatic", True)
        await engine.tick()
        await manager.bus.drain()
        await asyncio.wait_for(native._pending, 1)
        assert len(requests) == 3

        settings.plugins.enabled["media-server-refresh"] = False
        await manager.apply_settings()
        engine.store.bind(task["key"], {"tmdb_id": 1, "season": 3})
        await engine.apply((await engine.preview())["token"])
        await manager.bus.drain()
        assert "media-server-refresh" not in manager._active
        assert len(requests) == 3
    finally:
        await manager.stop()


async def test_archive_only_and_collection_only_changes(env, tmp_path):
    engine, task, _ = env
    engine.bridge.request_media_server_refresh = Mock()
    await engine.apply((await engine.preview())["token"])
    engine.bridge.request_media_server_refresh.reset_mock()

    archive = Path(engine.options.source_root) / "fonts.zip"
    archive.write_text("font archive")
    task["files"].append(
        {
            "index": 1,
            "name": archive.name,
            "source": str(archive),
            "progress": 1,
            "fingerprint": fingerprint(archive),
        }
    )
    result = await engine.apply((await engine.preview())["token"])
    assert any(row["state"] == "linked" for row in result["results"])
    engine.bridge.request_media_server_refresh.assert_not_called()

    music = tmp_path / "music"
    music.mkdir()
    album = music / "album"
    album.mkdir()
    track = album / "track.flac"
    track.write_bytes(b"audio")
    collection = tmp_path / "collections"
    collection.mkdir()
    engine.options.music_root = str(music)
    engine.options.collections_root = str(collection)
    engine.store.claim({"key": "music", "target": str(track), "fingerprint": fingerprint(track)})
    engine.store.set(
        "collection_members",
        {
            "music": {
                "collection": {"key": "tv:1", "title": "Show"},
                "kind": "music",
                "member": str(album),
                "target": str(track),
            },
        },
    )
    assert (await engine.refresh_collections())["collections"][0]["state"] == "written"
    assert (await engine.refresh_collections())["collections"][0]["state"] == "exists"
    engine.bridge.request_media_server_refresh.assert_called_once_with()
