import errno
import os
from pathlib import Path

import pytest
from test_engine import env as env

from library_organizer.engine import Engine
from library_organizer.planner import fingerprint
from library_organizer.transfer import rename_noreplace


@pytest.mark.parametrize("occupied", [False, True])
def test_atomic_rename_preserves_inode_and_never_replaces_target(tmp_path, occupied):
    old, new = tmp_path / "old", tmp_path / "new"
    old.write_bytes(b"media")
    fp = fingerprint(old)
    if occupied:
        new.write_bytes(b"foreign")
        with pytest.raises(FileExistsError):
            rename_noreplace(old, new)
        assert old.read_bytes() == b"media" and new.read_bytes() == b"foreign"
    else:
        rename_noreplace(old, new)
        assert not old.exists() and fingerprint(new) == fp


@pytest.mark.parametrize("rename_fails", [False, True])
async def test_replica_moves_without_hardlinks_or_copy(env, monkeypatch, rename_fails):
    e, task, src = env
    plan = await e.preview()
    old = Path(plan["items"][0]["work_root"]) / "Season 1" / src.name
    old.parent.mkdir(parents=True)
    old.write_bytes(src.read_bytes())
    fp, source_fp = fingerprint(old), fingerprint(src)
    plan = await e.preview()
    item = plan["items"][0]
    assert item["operation"] == "adopt"
    monkeypatch.setattr(os, "link", lambda *a, **kw: pytest.fail("Move must not create hardlinks"))
    monkeypatch.setattr(
        "library_organizer.engine.publish", lambda *a: pytest.fail("Must not copy a replica")
    )
    if rename_fails:

        def denied(*args):
            raise PermissionError(errno.EACCES, "directory permission denied")

        monkeypatch.setattr("library_organizer.engine.rename_noreplace", denied)
    result = await e.apply(plan["token"])
    row = result["results"][0]
    target = Path(item["target"])
    if rename_fails:
        assert row["state"] == "failed" and not target.exists()
        assert fingerprint(old) == fp and old.parent.is_dir()
    else:
        assert row["action"] == "relocated" and fingerprint(target) == fp
        assert not old.parent.exists()
        assert row["cleaned_directories"] == [str(old.parent)]
        assert "清理空目录 1 个" in row["detail"]
        assert len(e.store.owned()) == 1
        assert (await e.preview())["counts"] == {"unchanged": 1}
    assert fingerprint(src) == source_fp


async def test_mapping_change_prunes_only_empty_old_directories(env):
    e, task, src = env
    plan = await e.preview()
    old = Path(plan["items"][0]["target"])
    await e.apply(plan["token"])
    foreign = old.parent / ".keep"
    foreign.write_text("user file")
    e.store.bind(task["key"], {"tmdb_id": 1, "season": 2})
    result = await e.apply((await e.preview())["token"])
    assert result["results"][0]["cleaned_directories"] == []
    assert foreign.read_text() == "user file" and old.parent.is_dir()


def test_cleanup_stops_at_roots_nonempty_directories_and_symlinks(env, tmp_path):
    e, _, src = env
    root = Path(e.options.library_root)
    leaf = root / "old-work" / "Season 1"
    leaf.mkdir(parents=True)
    assert e.prune_empty_directories(leaf) == [str(leaf), str(leaf.parent)]
    assert root.is_dir()
    assert e.prune_empty_directories(root) == []
    outside = tmp_path / "outside"
    outside.mkdir()
    alias = root / "alias"
    alias.symlink_to(outside, target_is_directory=True)
    assert e.prune_empty_directories(alias) == [] and outside.is_dir() and alias.is_symlink()
    assert e.prune_empty_directories(src.parent) == [] and src.exists()
    e.options.source_root = str(root / "downloads")
    nested = Path(e.options.source_root) / "empty"
    nested.mkdir(parents=True)
    assert e.prune_empty_directories(nested) == [] and nested.is_dir()
    e.options.music_root = str(root / "music")
    album = Path(e.options.music_root) / "old-work" / "album"
    album.mkdir(parents=True)
    assert e.prune_empty_directories(album) == [str(album), str(album.parent)]
    assert Path(e.options.music_root).is_dir()


async def test_rename_recovers_after_crash_before_history_commit(env, monkeypatch):
    e, task, src = env
    plan = await e.preview()
    await e.apply(plan["token"])
    old = Path(plan["items"][0]["target"])
    fp = fingerprint(old)
    e.store.bind(task["key"], {"tmdb_id": 1, "season": 2})
    plan = await e.preview()
    monkeypatch.setattr(
        os,
        "link",
        lambda *a, **kw: pytest.fail("Move must not create hardlinks"),
    )
    original_record = e.store.record

    def crash(*args, **kwargs):
        raise RuntimeError("crash")

    monkeypatch.setattr(e.store, "record", crash)
    with pytest.raises(RuntimeError):
        await e.apply(plan["token"])
    assert not old.exists()
    monkeypatch.setattr(e.store, "record", original_record)
    restarted = Engine(e.store, e.bridge, e.options)
    plan = await restarted.preview()
    await restarted.apply(plan["token"])
    assert fingerprint(Path(plan["items"][0]["target"])) == fp
    assert len(e.store.owned()) == 1
