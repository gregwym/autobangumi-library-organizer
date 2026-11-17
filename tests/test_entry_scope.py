from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from library_organizer.association import Associations, record_hash
from library_organizer.bridge40 import Bridge40
from library_organizer.engine import Engine
from library_organizer.models import Options
from library_organizer.planner import fingerprint, task_key
from library_organizer.store import Store


@pytest.fixture
async def scoped(tmp_path, monkeypatch):
    from module import downloader
    from module.database import Database
    from module.downloader import download_client
    from module.models import Bangumi, Torrent

    root = tmp_path / "source"
    root.mkdir()
    async with Database() as db:
        a = Bangumi(official_title="Show", title_raw="Show", season=3)
        b = Bangumi(
            official_title="Incorrect movie title",
            title_raw="Unmatched alternate release title",
            season=3,
        )
        db.add(a)
        db.add(b)
        await db.commit()
        db.add(Torrent(bangumi_id=a.id, qb_hash="a" * 40, downloader_id="default"))
        db.add(
            Torrent(
                bangumi_id=b.id,
                url="https://mikanani.me/Download/20250901/" + "b" * 40 + ".torrent",
                name="Different RSS title",
                downloader_id="default",
            )
        )
        await db.commit()
    infos, file_lists, queried = [], {}, []

    def add(char, group, ep, progress=1):
        name = f"[{group}] Show [{ep:02d}].mkv"
        (root / name).write_bytes(b"abc")
        infos.append(
            {
                "hash": char * 40,
                "name": name,
                "save_path": str(root),
                "progress": progress,
                "tags": f"ab:{a.id}",
            }
        )
        file_lists[char * 40] = [{"index": 0, "name": name, "size": 3, "progress": progress}]
        return infos[-1], file_lists[char * 40][0]

    add("a", "GroupA", 1)
    add("b", "GroupB", 1)

    class Client:
        def __init__(self, instance):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def get_torrent_info(self, **kwargs):
            return infos

        async def get_torrent_files(self, identity):
            queried.append(identity)
            return file_lists[identity]

    monkeypatch.setattr(downloader, "DownloadClient", Client)
    monkeypatch.setattr(download_client, "downloader_ids", lambda: ["default"])
    store = Store(tmp_path / "plugin.sqlite3")
    options = Options(
        source_root=str(root), library_root=str(tmp_path / "library"), tv_subdirectory=""
    )
    bridge = Bridge40(options, store)
    bridge.metadata = AsyncMock(
        return_value={"title": "Show", "year": "2020", "episodes": [1, 2, 3]}
    )
    bridge.health = lambda: {"native_rename": False, "native_hardlink": False}
    bridge.refresh_jellyfin = AsyncMock(return_value="ok")
    engine = Engine(store, bridge, options)
    store.set("active", True)
    store.set("automatic", True)
    for r in [a, b]:
        store.bind("rule:" + str(r.id), {"tmdb_id": 9, "season": 3, "follow_ab": True})

    async def record(char, owner):
        async with Database() as db:
            db.add(Torrent(bangumi_id=owner, qb_hash=char * 40, downloader_id="default"))
            await db.commit()

    return SimpleNamespace(
        engine=engine,
        bridge=bridge,
        store=store,
        a=a,
        b=b,
        root=root,
        infos=infos,
        files=file_lists,
        queried=queried,
        add=add,
        record=record,
    )


async def test_single_entry_picker_does_not_inspect_other_or_unknown_files(scoped):
    x = scoped
    x.add("c", "Unknown", 1)
    tasks = await x.bridge.scan(rule_id=x.a.id, discover_unassigned=True)
    assert x.queried == ["a" * 40]
    assert {t["rule_id"] for t in tasks} == {x.a.id, None}
    assert next(t for t in tasks if t["rule_id"] is None)["files"] == []
    assert [t["rule_id"] for t in x.store.tasks()] == [x.a.id]


async def test_entry_isolation_and_shared_library_identity(scoped):
    x = scoped
    e = x.engine
    x.store.set("assignment:" + task_key("default", "b" * 40), x.a.id)
    plan = await e.preview(rule_id=x.a.id)
    assert len(plan["items"]) == 1
    assert x.queried == ["a" * 40]
    await e.apply(plan["token"], rule_id=x.a.id)
    first = Path(plan["items"][0]["target"])
    assert not list(first.parent.glob("*GroupB*"))
    plan = await e.preview(rule_id=x.b.id)
    assert len(plan["items"]) == 1 and plan["items"][0]["rule_id"] == x.b.id
    await e.apply(plan["token"], rule_id=x.b.id)
    second = Path(plan["items"][0]["target"])
    original = fingerprint(second)
    assert first.parent == second.parent and first != second
    # Editing A must not relocate B, although both target the same TMDB show.
    from module.database import Database
    from module.models import Bangumi

    async with Database() as db:
        rule = await db.session.get(Bangumi, x.a.id)
        rule.season_offset = 1
        await db.commit()
    plan = await e.preview(rule_id=x.a.id)
    await e.apply(plan["token"], rule_id=x.a.id)
    assert fingerprint(second) == original and second.exists()
    assert "Season 04" in plan["items"][0]["target"]


async def test_incremental_completion_and_repeat_scan_do_not_recopy(scoped, monkeypatch):
    from library_organizer import engine as module

    x = scoped
    e = x.engine
    await e.tick()
    before = {r["target"]: fingerprint(Path(r["target"])) for r in x.store.owned()}
    created = []
    publish = module.publish

    def observe(src, dst, *args):
        created.append(str(dst))
        return publish(src, dst, *args)

    monkeypatch.setattr(module, "publish", observe)
    info, file = x.add("c", "GroupA", 2, progress=0.5)
    await x.record("c", x.a.id)
    await e.tick()
    assert not created
    info["progress"] = file["progress"] = 1
    await e.tick()
    assert len(created) == 1 and "S03E02" in created[0]
    assert all(fingerprint(Path(p)) == fp for p, fp in before.items())
    refreshed = x.bridge.refresh_jellyfin.await_count
    await e.tick()
    await e.tick()
    assert len(created) == 1 and x.bridge.refresh_jellyfin.await_count == refreshed
    # Similar title + misleading qB tag is not evidence of entry ownership.
    x.add("d", "GroupA", 3)
    x.queried.clear()
    await e.tick()
    assert "d" * 40 not in x.queried and len(created) == 1
    # A late AB record is picked up automatically when it becomes available.
    await x.record("d", x.a.id)
    await e.tick()
    assert len(created) == 2 and "S03E03" in created[-1]


async def test_late_subtitle_in_existing_task_is_incremental(scoped, monkeypatch):
    from library_organizer import engine as module

    x = scoped
    e = x.engine
    await e.tick()
    original = {r["target"]: fingerprint(Path(r["target"])) for r in x.store.owned()}
    name = "[GroupA] Show [01].chs.ass"
    (x.root / name).write_bytes(b"sub")
    file = {"index": 1, "name": name, "size": 3, "progress": 0.4}
    x.files["a" * 40].append(file)
    await e.tick()
    assert len(x.store.owned()) == 2
    created = []
    publish = module.publish

    def observe(src, dst, *args):
        created.append(str(dst))
        return publish(src, dst, *args)

    monkeypatch.setattr(module, "publish", observe)
    file["progress"] = 1
    await e.tick()
    assert len(created) == 1 and created[0].endswith("GroupA.zh-Hans.ass")
    assert all(fingerprint(Path(p)) == fp for p, fp in original.items())
    await e.tick()
    assert len(created) == 1


async def test_conflicting_ab_records_are_not_overridden_by_assignment(scoped):
    x = scoped
    await x.record("b", x.a.id)
    x.store.set("assignment:" + task_key("default", "b" * 40), x.a.id)
    tasks = await x.bridge.scan()
    task = next(t for t in tasks if t["task_id"] == "b" * 40)
    assert task["rule_id"] is None
    assert not task["association"]["assignable"]
    assert len(await x.bridge.scan(rule_id=x.a.id)) == 1


async def test_legacy_history_repartition_does_not_touch_files_or_bindings(scoped):
    x = scoped
    tasks = await x.bridge.scan()
    b = next(t for t in tasks if t["rule_id"] == x.b.id)
    b["rule_id"] = x.a.id
    x.store.save_task(b)
    key = b["key"] + ":0"
    x.store.set(
        "last_items",
        [
            {
                "key": key,
                "task": b["key"],
                "rule_id": x.a.id,
                "source": "old source",
                "target": "existing library file",
            }
        ],
    )
    x.store.set("last_results:rule:" + str(x.a.id), [{"key": key, "state": "linked"}])
    binding = x.store.direct_binding("rule:" + str(x.b.id))
    await x.bridge.reconcile_history()
    assert x.store.get("last_results:rule:" + str(x.a.id), []) == []
    assert x.store.get("last_results:rule:" + str(x.b.id))[0]["key"] == key
    assert x.store.get("last_items")[0]["target"] == "existing library file"
    assert x.store.get("last_items")[0]["rule_id"] == x.b.id
    assert x.store.direct_binding("rule:" + str(x.b.id)) == binding


@pytest.mark.parametrize(
    "url,expected",
    [
        ("magnet:?xt=urn:btih:" + "A" * 40, "a" * 40),
        ("https://mikanani.me/Download/20250901/" + "b" * 40 + ".torrent", "b" * 40),
        ("https://other.test/" + "b" * 40 + ".torrent", None),
        ("https://mikanani.me/page?token=" + "b" * 40, None),
    ],
)
def test_only_supported_stored_identities_are_resolved(url, expected):
    assert record_hash(SimpleNamespace(qb_hash=None, url=url))[0] == expected


def test_downloader_identity_and_deleted_owner_are_respected():
    r = SimpleNamespace(id=1, bangumi_id=7, qb_hash="a" * 40, url="", downloader_id="other")
    rule = SimpleNamespace(id=7, downloader_id="other")
    a = Associations([r], [rule])
    assert a.resolve("default", "a" * 40)[0] is None
    assert a.resolve("other", "a" * 40)[0] == 7
    a = Associations([r], [])
    assert not a.resolve("other", "a" * 40)[1]["assignable"]
