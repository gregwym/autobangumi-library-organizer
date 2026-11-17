import xml.etree.ElementTree as ET
from pathlib import Path

import pytest
from test_planning import fixture
from test_resources import flac, plan

from library_organizer.collections import publish_manifest, reference_path, update_collections
from library_organizer.models import Options
from library_organizer.planner import fingerprint
from library_organizer.store import Store


def test_file_collection_incremental_membership_and_removal(tmp_path):
    music = tmp_path / "music"
    root = tmp_path / "collections"
    root.mkdir()
    opts = Options(
        source_root=str(tmp_path / "src"),
        library_root=str(tmp_path / "video"),
        music_root=str(music),
        collections_root=str(root),
    )
    store = Store(tmp_path / "state.db")

    def track(key, album):
        target = music / album / "01.flac"
        target.parent.mkdir(parents=True)
        target.write_bytes(key.encode())
        item = {
            "key": key,
            "target": str(target),
            "fingerprint": fingerprint(target),
            "album_target": str(target.parent),
            "collection": {"key": "tv:9", "title": "Show & Music"},
        }
        store.claim(item)
        return item

    a, b = track("a", "Disc set"), track("b", "HiRes")
    first = update_collections(store, opts, [a], [{"state": "linked"}])
    target = Path(first[0]["target"])

    def paths():
        return [n.text for n in ET.parse(target).findall("./CollectionItems/CollectionItem/Path")]

    assert paths() == [a["album_target"]]
    update_collections(store, opts, [b], [{"state": "linked"}])
    assert paths() == sorted([a["album_target"], b["album_target"]])
    assert ET.parse(target).findtext("LocalTitle") == "Show & Music"
    assert ET.parse(target).findtext("LockData") == "false"
    assert update_collections(store, opts, [b], [{"state": "exists"}])[0]["state"] == "exists"
    update_collections(
        store, opts, [{"key": "a"}, {"key": "b"}], [{"state": "exists"}, {"state": "exists"}]
    )
    assert paths() == []
    assert b"<CollectionItems></CollectionItems>" in target.read_bytes()


def test_collection_never_overwrites_foreign_or_modified_manifest(tmp_path):
    store = Store(tmp_path / "state.db")
    target = tmp_path / "collection.xml"
    target.write_bytes(b"user file")
    with pytest.raises(ValueError, match="未覆盖"):
        publish_manifest(store, tmp_path, target, b"ours")
    assert target.read_bytes() == b"user file"
    target.unlink()
    publish_manifest(store, tmp_path, target, b"ours")
    target.write_bytes(b"user edit")
    with pytest.raises(ValueError, match="未覆盖"):
        publish_manifest(store, tmp_path, target, b"new")
    assert target.read_bytes() == b"user edit"


def test_collections_only_include_unchanged_owned_tracks(tmp_path):
    music = tmp_path / "music"
    music.mkdir()
    root = tmp_path / "collections"
    root.mkdir()
    target = music / "album" / "01.flac"
    target.parent.mkdir()
    target.write_bytes(b"old")
    store = Store(tmp_path / "state.db")
    opts = Options(
        source_root=str(tmp_path / "src"),
        library_root=str(tmp_path / "video"),
        music_root=str(music),
        collections_root=str(root),
    )
    item = {
        "key": "a",
        "target": str(target),
        "fingerprint": fingerprint(target),
        "album_target": str(target.parent),
        "collection": {"key": "tv:1", "title": "Test"},
    }
    store.claim(item)
    target.write_bytes(b"changed")
    result = update_collections(store, opts, [item], [{"state": "exists"}])
    assert result[0]["albums"] == 0


def test_catalog_discs_group_but_editions_stay_separate(tmp_path):
    names = [
        "CDs/OST/LACA1/01.flac",
        "CDs/OST/LACA2/01.flac",
        "CDs/HiRes/01.flac",
        "CDs/HiRes/02.flac",
        "CDs/Single/EditionA/01.flac",
        "CDs/Single/EditionB/01.flac",
    ]
    task, opts = fixture(tmp_path, names)
    opts.music_root = str(tmp_path / "music")
    for f, disc, album, total in zip(
        task["files"],
        [1, 2, 1, 2, 1, 1],
        ["OST"] * 4 + ["Artist Edition", "Anime Edition"],
        [2, 2, 2, 2, 1, 1],
        strict=True,
    ):
        p = Path(f["source"])
        p.write_bytes(
            flac(
                {
                    "ALBUM": album,
                    "ALBUMARTIST": "Composer",
                    "DISCNUMBER": str(disc),
                    "DISCTOTAL": str(total),
                    "TITLE": "Track",
                    "TRACKNUMBER": "1",
                    "ARTIST": "Composer",
                }
            )
        )
        f["fingerprint"] = fingerprint(p)
    rows = plan(task, opts)
    assert rows[0]["target"].endswith("/OST/Disc 01/01.flac")
    assert rows[1]["target"].endswith("/OST/Disc 02/01.flac")
    assert rows[0]["album_target"] == rows[1]["album_target"]
    assert rows[2]["album_target"] == rows[3]["album_target"]
    assert rows[0]["album_target"] != rows[2]["album_target"]
    assert rows[4]["album_target"] != rows[5]["album_target"]
    assert rows[4]["target"].endswith("/Single/EditionA/01.flac")


def test_configuration_has_no_media_server_context():
    assert not any("jellyfin" in key for key in Options.model_fields)
    with pytest.raises(ValueError):
        Options(music_root="/music", collections_root="/music/collections")


def test_reference_paths_are_boundary_aware_and_longest_prefix_wins():
    opts = Options(
        collections_root="/collections",
        collection_path_map=[
            {"local": "/library", "remote": "/media/bangumi"},
            {"local": "/library/Movies", "remote": "/media/movies"},
        ],
    )
    assert reference_path(opts, "/library/Show") == "/media/bangumi/Show"
    assert reference_path(opts, "/library/Movies/Film") == "/media/movies/Film"
    assert reference_path(opts, "/library-other/Show") == "/library-other/Show"
    for mappings in (
        [{"local": "/library/../other", "remote": "/media"}],
        [{"local": "/library", "remote": "relative"}],
        [{"local": "/library", "remote": "/a"}, {"local": "/library/", "remote": "/b"}],
    ):
        with pytest.raises(ValueError):
            Options(collection_path_map=mappings)


def test_legacy_video_history_and_albums_form_one_incremental_collection(tmp_path):
    opts = Options(
        source_root=str(tmp_path / "src"),
        library_root=str(tmp_path / "video"),
        music_root=str(tmp_path / "music"),
        collections_root=str(tmp_path / "collections"),
        collection_path_map=[{"local": str(tmp_path / "video"), "remote": "/media/anime"}],
    )
    Path(opts.collections_root).mkdir()
    store = Store(tmp_path / "state.db")

    def output(key, path, **fields):
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(key.encode())
        item = dict(
            key=key,
            source="/downloads/" + key,
            target=str(target),
            fingerprint=fingerprint(target),
            **fields,
        )
        store.claim(item)
        store.record(item, "linked")
        return item

    folder = Path(opts.library_root) / "Show (2020) [tmdbid-9]"
    a = output(
        "a",
        folder / "Season 01" / "Show S01E01.mkv",
        identity={"media_type": "tv", "tmdb_id": 9},
        rule_id=1,
    )
    b = output(
        "b",
        folder / "Season 03" / "Show S03E01.mkv",
        identity={"media_type": "tv", "tmdb_id": 9},
        rule_id=2,
    )
    pending = output(
        "pending",
        Path(opts.library_root) / "Other" / "Season 01" / "S01E01.mkv",
        identity={"media_type": "tv", "tmdb_id": 9},
    )
    store.record(pending, "failed")  # A durable claim without a success is not enough.
    store.set("last_items", [a, b, pending])
    assert update_collections(store, opts, [], []) == []  # No one-member video-only collection.
    before = {i["target"]: fingerprint(Path(i["target"])) for i in (a, b)}
    album = Path(opts.music_root) / "Show" / "OST"
    music = output(
        "m", album / "01.flac", album_target=str(album), collection={"key": "tv:9", "title": "Show"}
    )
    # Exercise the upgrade from the pre-0.9 membership format.
    store.set("collection_members", None)
    store.set(
        "music_members",
        {
            "m": {
                "target": music["target"],
                "album": str(album),
                "collection": music["collection"],
            }
        },
    )
    result = update_collections(store, opts, [], [])
    manifest = Path(result[0]["target"])
    paths = [n.text for n in ET.parse(manifest).findall("./CollectionItems/CollectionItem/Path")]
    assert paths == sorted(["/media/anime/Show (2020) [tmdbid-9]", str(album)])
    assert result[0]["series"] == 1 and result[0]["albums"] == 1
    stamp = manifest.stat().st_mtime_ns
    # Removing a downloader task does not remove already-published members.
    store.set("last_items", [])
    assert update_collections(store, opts, [], [])[0]["state"] == "exists"
    assert manifest.stat().st_mtime_ns == stamp
    assert before == {i["target"]: fingerprint(Path(i["target"])) for i in (a, b)}
    # A failed/held new download must not contribute a directory.
    bad = output(
        "bad",
        Path(opts.music_root) / "Not ready" / "01.flac",
        album_target=str(Path(opts.music_root) / "Not ready"),
        collection=music["collection"],
    )
    update_collections(store, opts, [bad], [{"state": "held"}])
    assert manifest.stat().st_mtime_ns == stamp
    # A future successful download updates XML only; no re-copy of previous outputs.
    update_collections(store, opts, [bad], [{"state": "linked"}])
    assert len(ET.parse(manifest).findall("./CollectionItems/CollectionItem/Path")) == 3


def test_movie_member_and_modified_video_are_checked(tmp_path):
    opts = Options(
        source_root=str(tmp_path / "src"),
        library_root=str(tmp_path / "video"),
        music_root=str(tmp_path / "music"),
        collections_root=str(tmp_path / "collections"),
    )
    Path(opts.collections_root).mkdir()
    store = Store(tmp_path / "state.db")
    collection = {"key": "movie:1", "title": "Film"}
    folder = Path(opts.library_root) / "Movies" / "Film"
    video = folder / "Film.mkv"
    album = Path(opts.music_root) / "Film" / "OST"
    track = album / "01.flac"
    items = []
    for key, target, fields in [
        (
            "v",
            video,
            {"identity": {"media_type": "movie", "tmdb_id": 1}, "collection_target": str(folder)},
        ),
        ("m", track, {"album_target": str(album)}),
    ]:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(key.encode())
        item = dict(
            key=key,
            target=str(target),
            fingerprint=fingerprint(target),
            collection=collection,
            **fields,
        )
        store.claim(item)
        items.append(item)
    result = update_collections(store, opts, items, [{"state": "exists"}] * 2)
    assert result[0]["movies"] == 1 and result[0]["series"] == 0
    video.write_bytes(b"user changed video")
    result = update_collections(store, opts, [], [])
    assert result[0]["movies"] == 0 and result[0]["albums"] == 1


def test_upgrade_removes_only_organizer_legacy_credentials(tmp_path):
    import json

    from module.conf import settings
    from module.conf.config import CONFIG_PATH

    from library_organizer.bridge40 import Bridge40

    settings.plugins.options["library-organizer"] = {
        "music_root": "/music",
        "jellyfin_api_key": "old-secret",
        "jellyfin_url": "http://old",
        "jellyfin_library_root": "/old",
    }
    settings.plugins.options["media-server-refresh"] = {"api_key": "other-plugin"}
    settings.save()
    store = Store(tmp_path / "state.db")
    Bridge40(Options(), store).remove_legacy_media_server_options()
    saved = json.loads(CONFIG_PATH.read_text())["plugins"]["options"]
    assert saved["library-organizer"] == {"music_root": "/music"}
    assert settings.plugins.options["library-organizer"] == saved["library-organizer"]
    assert saved["media-server-refresh"] == {"api_key": "other-plugin"}
    assert list((store.path.parent / "backups").rglob("config.json"))
