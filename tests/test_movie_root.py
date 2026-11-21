import errno
import shutil
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest
from pydantic import ValidationError

from library_organizer import transfer
from library_organizer.models import CollectionPathMap, Options
from library_organizer.planner import fingerprint

pytest_plugins = ["test_engine"]


def movie_task(engine, *, key="b", group="GroupA", companions=()):
    name = f"[{group}] Film - Movie"
    root = Path(engine.options.source_root) / group
    files = []
    for index, relative in enumerate((name + ".mkv", *companions)):
        source = root / relative
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_bytes(relative.encode())
        files.append(
            {
                "index": index,
                "name": relative,
                "source": str(source),
                "fingerprint": fingerprint(source),
                "progress": 1,
            }
        )
    task = {"name": f"[{group}] Film", "key": key * 32, "rule_id": 31, "files": files}
    engine.store.bind(task["key"], {"media_type": "movie", "tmdb_id": 50})
    return task


async def apply_rule(engine, rule=31):
    plan = await engine.preview(rule_id=rule)
    return await engine.apply(plan["token"], rule_id=rule)


async def test_independent_movie_root_migrates_owned_outputs_and_collection_paths(env, tmp_path):
    engine, tv, _ = env
    feature = movie_task(
        engine,
        companions=(
            "[GroupA] Film - Movie.chs.ass",
            "[GroupA] Film - Movie.jpn.mka",
            "[GroupA] Film - Song MV.mkv",
            "Fonts/Font.ttf",
            "CDs/OST/01.flac",
        ),
    )
    second_version = movie_task(engine, key="c", group="GroupB")
    engine.bridge.scan.return_value = [tv, feature, second_version]

    async def metadata(mapping):
        return (
            {"title": "Film", "year": "2020"}
            if mapping.media_type == "movie"
            else {"title": "Show", "episodes": [1]}
        )

    engine.bridge.metadata.side_effect = metadata
    movies = tmp_path / "film library"
    music = tmp_path / "albums"
    collections = tmp_path / "collections"
    for path in (movies, music, collections):
        path.mkdir()
    tv_root = engine.options.library_root
    engine.options.music_root = str(music)
    engine.options.collections_root = str(collections)
    engine.options.collection_path_map = [
        CollectionPathMap(local=source, remote=remote)
        for source, remote in (
            (tv_root, "/scan/video"),
            (str(movies), "/scan/films"),
            (str(music), "/scan/albums"),
        )
    ]
    initial = await engine.preview()
    await engine.apply(initial["token"])
    initial_paths = {row["name"]: Path(row["target"]) for row in initial["items"]}
    initial_fingerprints = {name: fingerprint(path) for name, path in initial_paths.items()}
    work = "Film (2020) [tmdbid-50]"
    assert initial_paths["[GroupA] Film - Movie.mkv"].parent == Path(tv_root) / "Movies" / work
    manifest = next(collections.rglob("collection.xml"))

    def members():
        return {
            node.text
            for node in ET.parse(manifest).findall("./CollectionItems/CollectionItem/Path")
        }

    assert members() == {f"/scan/video/Movies/{work}", f"/scan/albums/{work}/CDs/OST"}
    engine.options = Options.model_validate(
        {**engine.options.model_dump(), "movie_root": str(movies)}
    )
    assert (await engine.refresh_collections())["collections"][0]["state"] == "exists"
    assert f"/scan/video/Movies/{work}" in members()

    plan = await engine.preview(rule_id=31)
    assert plan["counts"] == {"reuse": 6, "unchanged": 1}
    assert not list(movies.iterdir())  # Preview writes no media or directories.
    result = await engine.apply(plan["token"], rule_id=31)
    rows = {row["name"]: row for row in result["results"]}
    assert all(row["state"] in {"linked", "exists"} for row in rows.values())
    folder = movies / work
    assert {p.name for p in folder.glob("*.mkv")} == {
        f"{work} - GroupA.mkv",
        f"{work} - GroupB.mkv",
    }
    assert (folder / f"{work} - GroupA.zh-Hans.ass").is_file()
    assert (folder / f"{work} - GroupA.ja.mka").is_file()
    assert (folder / "extras" / "[GroupA] Film - Song MV.mkv").is_file()
    assert (folder / "_resources" / ".ignore").is_file()
    assert next((folder / "_resources").rglob("Font.ttf")).read_bytes() == b"Fonts/Font.ttf"
    assert members() == {f"/scan/films/{work}", f"/scan/albums/{work}/CDs/OST"}
    assert not (movies / "Movies").exists()
    for name, old in initial_paths.items():
        if name in rows and rows[name]["action"] == "relocated":
            assert not old.exists()
            assert fingerprint(Path(rows[name]["target"])) == initial_fingerprints[name]
        else:
            assert fingerprint(old) == initial_fingerprints[name]
    for task in (tv, feature, second_version):
        for item in task["files"]:
            assert fingerprint(Path(item["source"])) == item["fingerprint"]
    assert (await engine.preview())["counts"] == {"unchanged": 8}


@pytest.mark.parametrize("root_removed_after_preview", [False, True])
async def test_missing_movie_root_preserves_existing_movie(
    env, tmp_path, root_removed_after_preview
):
    engine, _, _ = env
    task = movie_task(engine)
    engine.bridge.scan.return_value = [task]
    old = Path((await apply_rule(engine))["results"][0]["target"])
    movies = tmp_path / "movies"
    engine.options.movie_root = str(movies)
    if root_removed_after_preview:
        movies.mkdir()
    plan = await engine.preview(rule_id=31)
    if root_removed_after_preview:
        assert plan["counts"] == {"reuse": 1}
        movies.rmdir()
    row = (await engine.apply(plan["token"], rule_id=31))["results"][0]
    assert row["state"] == ("failed" if root_removed_after_preview else "held")
    assert "独立电影库根目录不存在" in row["detail"]
    assert old.is_file() and not movies.exists()


@pytest.mark.parametrize("io_failure", [False, True])
async def test_cross_mount_migration_preserves_source_and_verifies_before_removal(
    env, tmp_path, monkeypatch, io_failure
):
    engine, _, _ = env
    task = movie_task(engine)
    engine.bridge.scan.return_value = [task]
    old = Path((await apply_rule(engine))["results"][0]["target"])
    movies = tmp_path / "movies"
    movies.mkdir()
    engine.options.movie_root = str(movies)
    source = Path(task["files"][0]["source"])
    before = fingerprint(source)

    def cross_mount(src, dst):
        assert src == old and movies in dst.parents
        raise OSError(errno.EXDEV, "cross mount")

    def materialize(src, stage):
        assert src == old and old.is_file()
        if io_failure:
            raise OSError(errno.ENOSPC, "out of space")
        shutil.copy2(src, stage)
        return "copy"

    monkeypatch.setattr("library_organizer.engine.rename_noreplace", cross_mount)
    monkeypatch.setattr(transfer, "materialize", materialize)
    plan = await engine.preview(rule_id=31)
    assert plan["counts"] == {"reuse": 1}
    assert "跨挂载可能需要复制" in plan["items"][0]["explanation"]
    row = (await engine.apply(plan["token"], rule_id=31))["results"][0]
    target = Path(row["target"])
    assert fingerprint(source) == before
    if io_failure:
        assert row["state"] == "failed"
        assert old.is_file() and not target.exists()
    else:
        assert row["action"] == "relocated" and row["method"] == "copy"
        assert not old.exists() and target.read_bytes() == source.read_bytes()
        assert engine.store.completed(plan["items"][0])


async def test_legacy_movie_candidate_and_unknown_former_root_do_not_create_duplicates(
    env, tmp_path
):
    engine, _, _ = env
    task = movie_task(engine)
    engine.bridge.scan.return_value = [task]
    item = (await engine.preview(rule_id=31))["items"][0]
    legacy = Path(item["target"]).with_name("old movie name.mkv")
    legacy.parent.mkdir(parents=True)
    shutil.copy2(item["source"], legacy)
    movies = tmp_path / "first movies"
    movies.mkdir()
    engine.options.movie_root = str(movies)
    plan = await engine.preview(rule_id=31)
    assert plan["counts"] == {"adopt": 1} and plan["items"][0]["adopt"]["path"] == str(legacy)
    result = await engine.apply(plan["token"], rule_id=31)
    assert result["results"][0]["action"] == "relocated" and not legacy.exists()
    old = Path(result["results"][0]["target"])
    engine.options.movie_root = ""
    held = await engine.preview(rule_id=31)
    assert held["counts"] == {"held": 1}
    assert "原电影路径不在当前可核验范围" in held["items"][0]["reason"]
    await engine.apply(held["token"], rule_id=31)
    assert old.is_file() and not Path(item["target"]).exists()


@pytest.mark.parametrize(
    "roots",
    [
        {"movie_root": "movies"},
        {"movie_root": "/movie/../other"},
        {"movie_root": "/downloads/films"},
        {"movie_root": "/library/films"},
        {"movie_root": "/movies", "music_root": "/movies/music"},
        {"movie_root": "/movies", "collections_root": "/movies/collections"},
    ],
)
def test_dedicated_movie_root_requires_disjoint_absolute_output(roots):
    with pytest.raises(ValidationError):
        Options(**roots)
