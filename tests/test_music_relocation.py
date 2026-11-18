import json
import shutil
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from library_organizer.engine import Engine
from library_organizer.models import Options
from library_organizer.planner import fingerprint
from library_organizer.relocation import snapshot
from library_organizer.store import Store


@pytest.fixture
async def moved(tmp_path):
    source, old, new = [tmp_path / name for name in ("downloads", "old music", "new music")]
    (source / "CDs/OST").mkdir(parents=True)
    new.mkdir()
    tracks = []
    for index in range(2):
        path = source / f"CDs/OST/{index + 1:02d}.flac"
        path.write_bytes(b"track-" + bytes([index]))
        tracks.append(
            {
                "name": str(path.relative_to(source)),
                "source": str(path),
                "fingerprint": fingerprint(path),
                "progress": 1,
                "index": index,
            }
        )
    options = Options(
        source_root=str(source),
        library_root=str(tmp_path / "video"),
        music_root=str(old),
        collections_root=str(tmp_path / "collections"),
    )
    old.mkdir()
    Path(options.collections_root).mkdir()
    task = {"key": "a" * 32, "name": "Show", "rule_id": 1, "files": tracks}
    bridge = SimpleNamespace(
        scan=AsyncMock(return_value=[task]),
        metadata=AsyncMock(return_value={"title": "Show", "year": 2024, "episodes": [1]}),
        health=lambda: {"native_rename": False, "native_hardlink": False},
    )
    store = Store(tmp_path / "state/library.sqlite3")
    store.set("active", True)
    store.bind(task["key"], {"tmdb_id": 1, "season": 1, "media_type": "tv"})
    engine = Engine(store, bridge, options)
    initial = await engine.preview()
    await engine.apply(initial["token"])
    for row in store.owned():
        previous = Path(row["target"])
        target = new / previous.relative_to(old)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(previous, target)
        previous.unlink()
    options.music_root = str(new)
    return engine, old, new, task


def state(store):
    with store.db() as connection:
        return snapshot(connection)


async def test_external_move_updates_history_and_collections_then_incremental_is_noop(moved):
    engine, old, new, task = moved
    before = {
        str(path): fingerprint(path)
        for root in (new, Path(engine.options.source_root))
        for path in root.rglob("*")
        if path.is_file()
    }
    source_fingerprints = {row["key"]: row["fingerprint"] for row in engine.store.files()}
    backup_count = len(list((engine.store.path.parent / "backups").glob("*")))
    blocked = await engine.preview(automatic=True)
    assert all(
        item["operation"] == "held" and "音乐目录已变更" in item["reason"]
        for item in blocked["items"]
    )
    engine.bridge.scan.reset_mock()
    plan = await engine.music_relocation(old_root=str(old))
    assert plan["ready"] and len(plan["items"]) == 2
    assert len(list((engine.store.path.parent / "backups").glob("*"))) == backup_count
    engine.bridge.scan.assert_not_awaited()
    result = await engine.music_relocation(token=plan["token"])
    assert result["files"] == 2 and not result["automatic"]
    assert Path(result["backup"]).is_dir()
    engine.bridge.scan.assert_not_awaited()
    for row in engine.store.owned():
        assert Path(row["target"]).is_relative_to(new)
        assert json.loads(row["fingerprint"]) == fingerprint(Path(row["target"]))
    assert {row["key"]: row["fingerprint"] for row in engine.store.files()} == source_fingerprints
    assert before == {path: fingerprint(Path(path)) for path in before}
    manifest = next(Path(engine.options.collections_root).rglob("collection.xml")).read_text()
    assert str(new) in manifest and str(old) not in manifest
    assert all(str(new) in row["target"] for row in engine.store.get("last_items"))
    plan = await engine.preview(automatic=True)
    assert all(item["operation"] == "unchanged" for item in plan["items"])
    await engine.apply(plan["token"], enable_automatic=True)
    assert before == {path: fingerprint(Path(path)) for path in before}
    # An additional completed download is still organized incrementally.
    extra = Path(engine.options.source_root) / "CDs/OST/03.flac"
    extra.write_bytes(b"next track")
    task["files"].append(
        {
            "name": "CDs/OST/03.flac",
            "source": str(extra),
            "fingerprint": fingerprint(extra),
            "progress": 1,
            "index": 2,
        }
    )
    plan = await engine.preview(automatic=True)
    assert sum(item["operation"] == "create" for item in plan["items"]) == 1
    assert sum(item["operation"] == "unchanged" for item in plan["items"]) == 2


@pytest.mark.parametrize("change", ["missing", "content", "symlink", "source", "owner"])
async def test_one_unverified_file_prevents_all_record_changes(moved, change):
    engine, old, new, task = moved
    row = engine.store.owned()[0]
    target = new / Path(row["target"]).relative_to(old)
    if change == "missing":
        target.unlink()
    elif change == "content":
        target.write_bytes(b"wrong!!")
    elif change == "symlink":
        target.unlink()
        target.symlink_to(task["files"][0]["source"])
    elif change == "source":
        Path(task["files"][0]["source"]).write_bytes(b"changed")
    else:
        engine.store.claim(
            {"key": "someone-else", "target": str(target), "fingerprint": [1, 2, 3, 4]}
        )
    before = state(engine.store)
    plan = await engine.music_relocation(old_root=str(old))
    assert not plan["ready"] and any(row["state"] == "held" for row in plan["items"])
    with pytest.raises(ValueError, match="尚未更新任何记录"):
        await engine.music_relocation(token=plan["token"])
    assert state(engine.store) == before


@pytest.mark.parametrize("change", ["target", "records", "config"])
async def test_apply_rechecks_preview_files_records_and_config(moved, change):
    engine, old, new, task = moved
    plan = await engine.music_relocation(old_root=str(old))
    if change == "target":
        Path(plan["items"][0]["target"]).write_bytes(b"altered")
    elif change == "records":
        engine.store.bind("rule:1", {"scope": "resources"})
    else:
        engine.options.version_suffix = False
    before = state(engine.store)
    with pytest.raises(ValueError, match="变化"):
        await engine.music_relocation(token=plan["token"])
    assert state(engine.store) == before


async def test_relocation_requires_paused_organizer_and_rejects_other_plan_types(moved):
    engine, old, new, task = moved
    engine.store.set("automatic", True)
    with pytest.raises(ValueError, match="关闭自动整理"):
        await engine.music_relocation(old_root=str(old))
    engine.store.set("automatic", False)
    plan = await engine.music_relocation(old_root=str(old))
    with pytest.raises(ValueError, match="类型不匹配"):
        await engine.apply(plan["token"])
    for invalid in (
        "/",
        str(new),
        str(new / "inside"),
        str(Path(engine.options.source_root).parent),
        "relative/path",
    ):
        with pytest.raises(ValueError):
            await engine.music_relocation(old_root=invalid)


async def test_video_and_other_entries_stay_unchanged_and_replay_paths_are_rebased(moved):
    engine, old, new, task = moved
    video = Path(engine.options.library_root) / "Another entry/Season 01/E01.mkv"
    video.parent.mkdir(parents=True)
    video.write_bytes(b"different entry")
    other = {
        "key": "other-entry:0",
        "source": "/downloads/other.mkv",
        "target": str(video),
        "fingerprint": [5, 6, 7, 8],
    }
    engine.store.claim(other, fingerprint(video), "copy")
    engine.store.record(other, "linked")
    row = engine.store.owned(task["key"] + ":0")[0]
    engine.store.save_task(
        {
            "key": task["key"],
            "files": [
                {
                    "source": task["files"][0]["source"],
                    "fingerprint": task["files"][0]["fingerprint"],
                    "replay_source": row["target"],
                    "replay_fingerprint": json.loads(row["fingerprint"]),
                }
            ],
        }
    )
    engine.store.set(
        "proof",
        {row["target"]: {"path": row["target"], "fingerprint": json.loads(row["fingerprint"])}},
    )
    other_before = engine.store.owned(other["key"])
    video_before = fingerprint(video)
    plan = await engine.music_relocation(old_root=str(old))
    await engine.music_relocation(token=plan["token"])
    assert engine.store.owned(other["key"]) == other_before and fingerprint(video) == video_before
    saved = engine.store.tasks()[0]["files"][0]
    assert saved["fingerprint"] == task["files"][0]["fingerprint"]
    assert saved["replay_source"].startswith(str(new) + "/")
    assert saved["replay_fingerprint"] == fingerprint(Path(saved["replay_source"]))
    proof = engine.store.get("proof")
    assert list(proof) == [saved["replay_source"]]
    assert proof[saved["replay_source"]]["fingerprint"] == saved["replay_fingerprint"]
