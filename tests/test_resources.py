import errno
import os
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from test_entry_scope import scoped as entry_scope
from test_planning import fixture

from library_organizer.bridge40 import Bridge40
from library_organizer.models import Mapping, Options
from library_organizer.planner import build, destination, fingerprint
from library_organizer.resources import music_notice
from library_organizer.store import Store

scoped = entry_scope


def plan(task, options, **kwargs):
    return build(
        task,
        Mapping(tmdb_id=9, season=3, **kwargs),
        {"title": "Show", "episodes": [1], "specials": [7]},
        options,
    )


def flac(tags):
    comments = [f"{key}={value}".encode() for key, value in tags.items()]
    block = (4).to_bytes(4, "little") + b"test" + len(comments).to_bytes(4, "little")
    block += b"".join(len(tag).to_bytes(4, "little") + tag for tag in comments)
    return b"fLaC" + b"\x84" + len(block).to_bytes(3, "big") + block


def test_season_extras_and_explicit_special_can_coexist(tmp_path):
    task, opts = fixture(
        tmp_path,
        [
            "[Group] Show [01].mkv",
            "SPs/Extra Episode 01.mkv",
            "SPs/Extra Episode 01.chs.ass",
            "SPs/NCOP.mkv",
            "SPs/CM 01.mkv",
            "SPs/Making01.mkv",
            "SPs/Interview.mkv",
            "SPs/Menu01.mkv",
        ],
    )
    rows = plan(task, opts, specials={"SPs/Extra Episode 01.mkv": 7})
    assert "Season 03/Show S03E01" in rows[0]["target"]
    assert "Season 00/Show S00E07" in rows[1]["target"]
    assert rows[2]["target"] == str(Path(rows[1]["target"]).with_suffix("")) + ".zh-Hans.ass"
    assert "/Season 03/extras/" in rows[3]["target"]
    assert "/Season 03/trailers/" in rows[4]["target"]
    assert "/Season 03/behind the scenes/" in rows[5]["target"]
    assert "/Season 03/interviews/" in rows[6]["target"]
    assert "/Season 03/extras/" in rows[7]["target"]
    assert "/Season 03/extras/" in plan(task, opts)[1]["target"]
    assert plan(task, opts, specials={"SPs/Extra Episode 01.mkv": 99})[1]["target"] is None


def test_subtitle_versions_are_local_and_preserve_track_labels(tmp_path):
    task, opts = fixture(
        tmp_path,
        [
            "A/Show [01].mkv",
            "B/Show [01].mkv",
            "A/Show [01].chs.ass",
            "A/Show [01].chs.signs.ass",
            "Subtitles/Show [01].cht.ass",
        ],
    )
    # Explicit names alone do not avoid video target collisions; sidecars must wait too.
    rows = plan(task, opts)
    assert rows[0]["target"] is None and rows[2]["target"] is None
    assert rows[4].get("archive_root")
    task["files"][1]["name"] = "B/[Other] Show [01].mkv"
    rows = plan(task, opts)
    assert rows[2]["target"].endswith("Group.zh-Hans.ass")
    assert rows[3]["target"].endswith("Group.zh-Hans.signs.ass")
    assert rows[4]["target"].endswith("Group.zh-Hant.ass")


def test_external_audio_is_not_an_album_track(tmp_path):
    task, opts = fixture(
        tmp_path,
        [
            "Show [01].mkv",
            "Show [01].commentary.ja.flac",
            "CDs/Album/Show [01].flac",
            "Show [02].ja.aac",
            "Unidentified.dts",
        ],
    )
    opts.music_root = str(tmp_path / "music")
    rows = plan(task, opts)
    assert rows[1]["target"].endswith("Group.ja.commentary.flac")
    assert rows[2]["target"].startswith(opts.music_root)
    assert rows[3].get("archive_root") and rows[4].get("archive_root")


def test_music_keeps_album_layout_and_only_explicit_cover_and_lyrics(tmp_path):
    task, opts = fixture(
        tmp_path,
        [
            "CDs/Album/Disc 1/01.flac",
            "CDs/Album/Disc 1/cover.jpg",
            "CDs/Album/Disc 1/01.lrc",
            "CDs/Album/Scans/01.webp",
            "CDs/Album/rip.log",
            "Fonts/Fonts.7z",
        ],
    )
    archived = plan(task, opts)
    assert all(x.get("archive_root") for x in archived)
    opts.music_root = str(tmp_path / "music")
    rows = plan(task, opts)
    assert all(x["target"].startswith(opts.music_root) for x in rows[:3])
    assert rows[0]["target"].endswith("CDs/Album/Disc 1/01.flac")
    assert all(x.get("archive_root") for x in rows[3:])
    assert rows[5]["target"].endswith("Fonts/Fonts.7z")


@pytest.mark.parametrize(
    "cue", ['FILE "disc.flac" WAVE\n TRACK 01 AUDIO\n TRACK 02 AUDIO\n', "broken CUE", "\ufffd"]
)
def test_disc_images_and_ambiguous_cues_stay_archived(tmp_path, cue):
    task, opts = fixture(tmp_path, ["CDs/Album/disc.flac", "CDs/Album/disc.cue"])
    path = Path(task["files"][1]["source"])
    path.write_text(cue)
    task["files"][1]["fingerprint"] = fingerprint(path)
    opts.music_root = str(tmp_path / "music")
    rows = plan(task, opts)
    assert all(x.get("archive_root") for x in rows)
    assert "CUE" in rows[0]["notice"]


def test_cue_for_individual_tracks_does_not_block_music(tmp_path):
    task, opts = fixture(tmp_path, ["CDs/Album/01.flac", "CDs/Album/02.flac", "CDs/Album/disc.cue"])
    Path(task["files"][2]["source"]).write_text(
        'FILE "01.flac" WAVE\n TRACK 01 AUDIO\nFILE "02.flac" WAVE\n TRACK 02 AUDIO\n'
    )
    opts.music_root = str(tmp_path / "music")
    rows = plan(task, opts)
    assert all(x["target"].startswith(opts.music_root) for x in rows[:2])
    assert rows[2].get("archive_root")


def test_flac_tag_inspection_is_bounded_and_does_not_mutate(tmp_path):
    path = tmp_path / "01.flac"
    content = flac({"ALBUM": "Album", "ARTIST": "Artist", "TITLE": "Track", "TRACKNUMBER": "1"})
    path.write_bytes(content)
    assert "保留内嵌" in music_notice(path)
    assert path.read_bytes() == content
    path.write_bytes(flac({"TITLE": "Track"}))
    assert "缺少专辑、曲号、艺术家" in music_notice(path)
    path.write_bytes(b"fLaC\x84\xff\xff\xff")
    assert "无法校验" in music_notice(path)


@pytest.mark.parametrize(
    "music", ["relative", "/downloads/music", "/library", "/library/music", "/"]
)
def test_music_root_cannot_overlap_or_escape(music):
    with pytest.raises(ValueError):
        Options(music_root=music)


def test_second_root_cannot_bypass_symlink_checks(tmp_path):
    options = Options(
        source_root=str(tmp_path / "src"),
        library_root=str(tmp_path / "lib"),
        music_root=str(tmp_path / "music"),
    )
    Path(options.music_root).mkdir()
    (Path(options.music_root) / "escape").symlink_to(tmp_path)
    with pytest.raises(ValueError):
        destination(options, Path(options.music_root) / "escape" / "outside.flac")


async def test_tmdb_fetches_specials_only_when_explicitly_mapped(tmp_path):
    bridge = Bridge40(Options(), Store(tmp_path / "state.sqlite3"))
    bridge.tmdb = AsyncMock(
        side_effect=[
            {"name": "Show"},
            {"episodes": [{"episode_number": 1}]},
            {"name": "Show"},
            {"episodes": [{"episode_number": 1}]},
            {"episodes": [{"episode_number": 7}]},
        ]
    )
    regular = await bridge.metadata(Mapping(tmdb_id=9, season=3))
    assert not regular["specials"] and bridge.tmdb.await_count == 2
    extra = await bridge.metadata(Mapping(tmdb_id=9, season=3, specials={"SP.mkv": 7}))
    assert extra["specials"] == [7]
    bridge.tmdb.assert_awaited_with("tv/9/season/0")


def add_music(x, char, *, cover=False):
    name = f"{char}/CDs/Album/01.flac" if not cover else f"{char}/CDs/Album/cover.jpg"
    src = x.root / name
    src.parent.mkdir(parents=True, exist_ok=True)
    src.write_bytes(b"audio-or-cover")
    index = len(x.files[char * 40])
    x.files[char * 40].append(
        {"index": index, "name": name, "size": src.stat().st_size, "progress": 1}
    )
    return src


async def test_archive_then_music_incremental_entry_scope_and_removed_task(
    scoped, tmp_path, monkeypatch
):
    from library_organizer import engine as engine_module

    x = scoped
    a, b = add_music(x, "a"), add_music(x, "b")
    cover = add_music(x, "a", cover=True)
    await x.engine.tick()
    archived = {r["source"]: Path(r["target"]) for r in x.store.files()}
    a_archive, b_archive = archived[str(a)], archived[str(b)]
    assert (next(p for p in a_archive.parents if p.name == "_resources") / ".ignore").exists()
    preserved = fingerprint(b_archive)
    # The downloader/source can disappear; only verified owned archives are replayable.
    x.infos[:] = [info for info in x.infos if info["hash"] != "a" * 40]
    a.unlink()
    cover.unlink()
    x.engine.options.music_root = str(tmp_path / "music")
    Path(x.engine.options.music_root).mkdir()
    p = await x.engine.preview(rule_id=x.a.id)
    assert len(p["items"]) == 2 and all(i["rule_id"] == x.a.id for i in p["items"])
    assert all(i.get("replay_source") for i in p["items"])
    r = await x.engine.apply(p["token"], rule_id=x.a.id)
    assert all(i["state"] == "linked" for i in r["results"])
    music = next(Path(i["target"]) for i in p["items"] if i["kind"] == "music")
    assert music.read_bytes() == b"audio-or-cover" and not a_archive.exists()
    assert b_archive.exists() and fingerprint(b_archive) == preserved
    # Music-only replay retains main episode history/verification.
    assert any(i.get("identity") for i in x.store.get("last_items") if i["rule_id"] == x.a.id)
    assert len(x.store.get(f"last_results:rule:{x.a.id}")) == 3
    created = []
    original = engine_module.publish

    def observe(src, dst, *args):
        created.append(dst)
        return original(src, dst, *args)

    monkeypatch.setattr(engine_module, "publish", observe)
    p = await x.engine.preview(rule_id=x.a.id)
    await x.engine.apply(p["token"], rule_id=x.a.id)
    assert not created
    # Unbinding the entry prevents archive replay, including automatic scans.
    with x.store.db() as db:
        db.execute("DELETE FROM bindings WHERE key=?", (f"rule:{x.a.id}",))
    assert not await x.bridge.scan(rule_id=x.a.id, bound_only=True)


async def test_archive_replay_rejects_modified_replica(scoped, tmp_path):
    x = scoped
    src = add_music(x, "a")
    await x.engine.tick()
    archived = next(Path(r["target"]) for r in x.store.files() if r["source"] == str(src))
    x.infos[:] = [i for i in x.infos if i["hash"] != "a" * 40]
    archived.write_bytes(b"replaced outside plugin")
    x.engine.options.music_root = str(tmp_path / "music")
    Path(x.engine.options.music_root).mkdir()
    assert not await x.bridge.scan(rule_id=x.a.id)


async def test_cross_filesystem_archive_migration_and_no_overwrite(scoped, tmp_path, monkeypatch):
    from library_organizer import transfer

    x = scoped
    src = add_music(x, "a")
    await x.engine.tick()
    old = next(Path(r["target"]) for r in x.store.files() if r["source"] == str(src))
    x.engine.options.music_root = str(tmp_path / "music")
    Path(x.engine.options.music_root).mkdir()
    real_link = os.link

    def cross(src, dst, **kwargs):
        if Path(src) == old:
            raise OSError(errno.EXDEV, "cross device")
        return real_link(src, dst, **kwargs)

    def no_clone(*args):
        raise OSError(errno.EOPNOTSUPP, "no clone")

    monkeypatch.setattr(os, "link", cross)
    monkeypatch.setattr(transfer, "reflink", no_clone)
    p = await x.engine.preview(rule_id=x.a.id)
    music_item = next(i for i in p["items"] if i["kind"] == "music")
    target = Path(music_item["target"])
    target.parent.mkdir(parents=True)
    target.write_bytes(b"user file")
    conflict = await x.engine.preview(rule_id=x.a.id)
    assert any(not i["target"] and "目标已存在" in i["reason"] for i in conflict["items"])
    assert old.exists() and target.read_bytes() == b"user file"
    target.unlink()
    p = await x.engine.preview(rule_id=x.a.id)
    r = await x.engine.apply(p["token"], rule_id=x.a.id)
    migrated = next(i for i in r["results"] if i["source"] == str(src))
    assert migrated["method"] == "copy" and not old.exists()
    assert target.read_bytes() == src.read_bytes()


async def test_missing_music_mount_keeps_archive(scoped, tmp_path):
    x = scoped
    add_music(x, "a")
    await x.engine.tick()
    before = {r["target"]: fingerprint(Path(r["target"])) for r in x.store.owned()}
    x.engine.options.music_root = str(tmp_path / "not-mounted")
    p = await x.engine.preview(rule_id=x.a.id)
    assert any(not i["target"] and "尚未挂载" in i["reason"] for i in p["items"])
    await x.engine.apply(p["token"], rule_id=x.a.id)
    assert all(fingerprint(Path(path)) == fp for path, fp in before.items())
    assert not Path(x.engine.options.music_root).exists()


async def test_foreign_ignore_file_is_not_replaced(scoped):
    x = scoped
    add_music(x, "a")
    p = await x.engine.preview(rule_id=x.a.id)
    archive = next(Path(i["archive_root"]) for i in p["items"] if i.get("archive_root"))
    archive.mkdir(parents=True)
    (archive / ".ignore").write_text("*.log")
    r = await x.engine.apply(p["token"], rule_id=x.a.id)
    assert any(i["state"] == "failed" and ".ignore" in i["detail"] for i in r["results"])
    assert (archive / ".ignore").read_text() == "*.log"


async def test_new_ignore_marker_refreshes_even_when_archives_already_exist(scoped):
    x = scoped
    add_music(x, "a")
    p = await x.engine.preview(rule_id=x.a.id)
    await x.engine.apply(p["token"], rule_id=x.a.id)
    archive = next(Path(i["archive_root"]) for i in p["items"] if i.get("archive_root"))
    (archive / ".ignore").unlink()  # Simulate archives made by the previous plugin version.
    previous = x.bridge.refresh_jellyfin.await_count
    p = await x.engine.preview(rule_id=x.a.id)
    r = await x.engine.apply(p["token"], rule_id=x.a.id)
    assert all(i["action"] == "unchanged" for i in r["results"])
    assert x.bridge.refresh_jellyfin.await_count == previous + 1
    p = await x.engine.preview(rule_id=x.a.id)
    await x.engine.apply(p["token"], rule_id=x.a.id)
    assert x.bridge.refresh_jellyfin.await_count == previous + 1
