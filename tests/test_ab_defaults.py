"""End-to-end regressions for native AB defaults, without a second binding step."""

import os
from pathlib import Path

import pytest

from library_organizer.bridge40 import Bridge40
from library_organizer.planner import fingerprint

pytest_plugins = ["test_engine", "test_entry_scope", "test_rule_api"]


async def clear_bindings(x):
    for rule in (x.a, x.b):
        x.store.unbind(f"rule:{rule.id}")


async def test_unbound_entry_and_future_episode_organize_without_tmdb(scoped):
    x = scoped
    await clear_bindings(x)
    x.add("z", "Unassigned", 1)
    await x.engine.tick()
    assert not x.store.get("error")
    x.bridge.metadata.assert_not_awaited()
    rows = [r for r in x.store.files() if Path(r["target"]).parent.parent.name == "Show"]
    assert len(rows) == 1 and "Season 03" in rows[0]["target"]
    before = fingerprint(Path(rows[0]["target"]))
    assert "z" * 40 not in x.queried
    info, file = x.add("c", "GroupA", 2, progress=0.4)
    await x.record("c", x.a.id)
    await x.engine.tick()
    assert len(x.store.files()) == 2
    info["progress"] = file["progress"] = 1
    await x.engine.tick()
    added = [r for r in x.store.files() if "E02" in r["target"]]
    assert len(added) == 1 and os.path.samefile(x.root / file["name"], added[0]["target"])
    assert fingerprint(Path(rows[0]["target"])) == before
    x.bridge.metadata.assert_not_awaited()
    assert not x.store.scan_state()[1]  # No generated per-entry bindings.


async def test_default_metadata_and_offsets_follow_ab_edits(scoped):
    from module.database import Database
    from module.models import Bangumi

    x = scoped
    await clear_bindings(x)
    async with Database() as db:
        r = await db.session.get(Bangumi, x.a.id)
        r.official_title, r.year, r.season_offset, r.episode_offset = "AB title", "2026", 1, 25
        await db.commit()
    p = await x.engine.preview(rule_id=x.a.id)
    assert p["held"] == 0 and len(p["items"]) == 1
    target = p["items"][0]["target"]
    assert "AB title (2026)/Season 04/AB title S04E26 - GroupA.mkv" in target
    assert "tmdbid" not in target
    await x.engine.apply(p["token"], rule_id=x.a.id)
    assert not list(Path(x.engine.options.library_root).rglob("*GroupB*"))


async def test_preview_invalidated_when_native_metadata_changes(scoped):
    from module.database import Database
    from module.models import Bangumi

    x = scoped
    await clear_bindings(x)
    p = await x.engine.preview(rule_id=x.a.id)
    async with Database() as db:
        rule = await db.session.get(Bangumi, x.a.id)
        rule.year = "2025"
        await db.commit()
    with pytest.raises(ValueError, match="文件或配置已变化"):
        await x.engine.apply(p["token"], rule_id=x.a.id)
    assert not x.store.owned()


async def test_tmdb_override_keeps_existing_layout_and_failure_isolated(scoped):
    x = scoped
    x.store.unbind(f"rule:{x.a.id}")
    x.bridge.metadata.side_effect = ValueError("TMDB 暂时不可用")
    await x.engine.tick()
    rows = x.store.get("last_results")
    assert len(rows) == 2
    assert any(r["state"] == "linked" and "Show/Season 03/" in r["target"] for r in rows)
    assert any(r["state"] == "held" and "TMDB 暂时不可用" in r["detail"] for r in rows)
    assert x.store.get("retry_at") > 0  # Metadata failure is retried, not permanently cached.
    x.bridge.metadata.side_effect = None
    x.store.set("retry_at", 0)
    await x.engine.tick()
    assert any("Show (2020) [tmdbid-9]" in r["target"] for r in x.store.files())


async def test_unassigned_task_needs_association_even_without_tmdb(scoped):
    x = scoped
    await clear_bindings(x)
    x.add("c", "Unknown", 1)
    p = await x.engine.preview()
    unknown = [i for i in p["items"] if i["rule_id"] is None]
    assert len(unknown) == 1 and not unknown[0]["target"]
    assert "AB 番剧归属" in unknown[0]["reason"]


@pytest.mark.parametrize(
    "kind,season,offset,expected",
    [("episode", 1, -1, 1), ("episode", 3, -8, 3), ("special", 1, -1, 0)],
)
def test_season_offset_matches_native_lower_bounds(kind, season, offset, expected):
    from module.downloader.path import gen_save_path
    from module.models import Bangumi

    rule = Bangumi(official_title="Show", season=season, season_offset=offset, episode_type=kind)
    native = gen_save_path(rule, root="/downloads")
    assert native.endswith(f"Season {expected}")
    assert Bridge40.rule_data(rule)["season"] == expected


async def test_automatic_preserves_user_deletion_manual_backfill_restores(env):
    e, task, source = env
    e.store.set("automatic", True)
    await e.tick()
    target = Path(e.store.files()[0]["target"])
    target.unlink()
    e.store.set("automatic_signature", None)  # A new download/config can trigger a full pass.
    await e.tick()
    assert not target.exists()
    assert "自动扫描不重建" in e.store.get("last_results")[0]["detail"]
    p = await e.preview(rule_id=1)
    await e.apply(p["token"], rule_id=1)
    assert os.path.samefile(source, target)


async def test_unpublished_ownership_intent_is_retried_automatically(env):
    e, task, source = env
    e.store.set("automatic", True)
    p = await e.preview()
    e.store.claim(p["items"][0])  # Crash before filesystem publication/success record.
    await e.tick()
    assert os.path.samefile(source, e.store.files()[0]["target"])


async def test_default_rule_ui_and_task_correction_need_no_tmdb(api):
    obj, task, client = api
    async with client:
        data = (await client.get("/rules/1/library")).json()
        assert data["metadata_source"] == "ab" and data["mapping"] is None
        assert data["effective_mapping"]["season"] == 2
        assert (
            await client.post("/rules/1/binding", json={"scope": "resources"})
        ).status_code == 200
        assert (
            await client.post(f"/rules/1/tasks/{task['key']}/binding", json={"season": 0})
        ).status_code == 200
        assert obj.store.binding(task)["tmdb_id"] is None
        obj.bridge.metadata.assert_not_awaited()
        # Explicit corrections still work, and saving AB mode restores the default.
        await client.post("/rules/1/binding", json={"tmdb_id": 99})
        assert (await client.get("/rules/1/library")).json()["metadata_source"] == "tmdb"
        await client.post("/rules/1/binding", json={"tmdb_id": None})
        assert (await client.get("/rules/1/library")).json()["metadata_source"] == "ab"
        assert obj.store.direct_binding(task["key"]) is not None  # Task corrections survive.


async def test_ab_movie_largest_primary_and_special_season(scoped):
    from module.database import Database
    from module.models import Bangumi

    x = scoped
    await clear_bindings(x)
    async with Database() as db:
        a = await db.session.get(Bangumi, x.a.id)
        b = await db.session.get(Bangumi, x.b.id)
        a.episode_type = "movie"
        b.episode_type, b.season, b.season_offset = "special", 1, -1
        await db.commit()
    extra = "Movie Alternative.mkv"
    (x.root / extra).write_bytes(b"x")
    x.files["a" * 40].append({"name": extra, "index": 1, "size": 1, "progress": 1})
    p = await x.engine.preview()
    assert p["held"] == 0
    a = [i for i in p["items"] if i["rule_id"] == x.a.id]
    assert any("Movies/Show/Show - GroupA.mkv" in i["target"] for i in a)
    assert any("extras/Movie Alternative.mkv" in i["target"] for i in a)
    assert any("Season 00" in i["target"] for i in p["items"] if i["rule_id"] == x.b.id)


async def test_default_music_and_collection_share_ab_identity_across_entries(scoped, tmp_path):
    import xml.etree.ElementTree as ET

    from module.database import Database
    from module.models import Bangumi
    from test_resources import add_music

    x = scoped
    await clear_bindings(x)
    async with Database() as db:
        b = await db.session.get(Bangumi, x.b.id)
        b.official_title = "Show"
        await db.commit()
    music, collections = tmp_path / "music", tmp_path / "collections"
    music.mkdir()
    collections.mkdir()
    x.engine.options.music_root = str(music)
    x.engine.options.collections_root = str(collections)
    sources = [add_music(x, "a"), add_music(x, "b")]
    await x.engine.tick()
    manifest = list(collections.rglob("collection.xml"))
    assert len(manifest) == 1 and manifest[0].parent.name == "Show [ab-tv] [boxset]"
    albums = [
        n.text for n in ET.parse(manifest[0]).findall("./CollectionItems/CollectionItem/Path")
    ]
    assert len(albums) == 2
    assert all(str(music / "Show") in album and "tmdbid" not in album for album in albums)
    assert all(any(os.path.samefile(src, r["target"]) for r in x.store.files()) for src in sources)
    x.bridge.metadata.assert_not_awaited()
