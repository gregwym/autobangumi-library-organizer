from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import FastAPI

from library_organizer.engine import Engine
from library_organizer.models import Options
from library_organizer.plugin import LibraryOrganizer
from library_organizer.store import Store


@pytest.fixture
def api(tmp_path):
    store = Store(tmp_path / "library.sqlite3")
    options = Options(source_root=str(tmp_path / "source"), library_root=str(tmp_path / "library"))
    task = {"key": "a" * 32, "name": "Show", "rule_id": 1, "files": []}
    bridge = SimpleNamespace(
        rule=AsyncMock(
            return_value={
                "id": 1,
                "title": "Show",
                "season": 2,
                "episode_offset": 0,
                "media_type": "tv",
            }
        ),
        scan=AsyncMock(return_value=[task]),
        metadata=AsyncMock(return_value={"title": "Show", "episodes": [1]}),
    )
    obj = LibraryOrganizer(SimpleNamespace(config=options))
    obj.store = store
    obj.bridge = bridge
    obj.engine = Engine(store, bridge, options)
    app = FastAPI()
    app.include_router(obj.api())
    return (
        obj,
        task,
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test"),
    )


async def test_rule_can_be_configured_before_any_download(api):
    obj, task, client = api
    obj.bridge.scan.return_value = []
    async with client:
        r = await client.post("/rules/1/binding", json={"tmdb_id": 8, "season": 2})
        assert r.status_code == 200
        data = (await client.get("/rules/1/library")).json()
        assert data["mapping"]["tmdb_id"] == 8
        assert obj.store.binding({"key": "new", "rule_id": 1})["tmdb_id"] == 8
        assert (await client.post("/rules/1/scan")).json() == {"tasks": [], "unassigned": []}


async def test_task_scope_cannot_modify_another_show(api):
    obj, task, client = api
    async with client:
        assert (
            await client.post(f"/rules/2/tasks/{task['key']}/binding", json={"tmdb_id": 9})
        ).status_code == 404
        assert (
            await client.post("/rules/2/assign", json={"task_key": task["key"]})
        ).status_code == 409
        assert obj.store.binding(task) is None


async def test_explicit_assignment_and_inheritance(api):
    obj, task, client = api
    task["rule_id"] = None
    async with client:
        assert (
            await client.post("/rules/1/assign", json={"task_key": task["key"]})
        ).status_code == 200
        assert obj.store.get("assignment:" + task["key"]) == 1
        await client.post("/rules/1/binding", json={"tmdb_id": 8, "season": 2})
        assert (
            await client.post(
                f"/rules/1/tasks/{task['key']}/binding", json={"tmdb_id": 8, "season": 0}
            )
        ).status_code == 200
        assert obj.store.binding(task)["season"] == 0
        assert (await client.post(f"/rules/1/tasks/{task['key']}/inherit")).status_code == 200
        assert obj.store.binding(task)["season"] == 2


async def test_rule_binding_rejects_file_specific_overrides(api):
    obj, task, client = api
    async with client:
        response = await client.post(
            "/rules/1/binding", json={"tmdb_id": 8, "episodes": {"one.mkv": 1}}
        )
        assert response.status_code == 422


async def test_rule_offsets_follow_ab_and_ignore_duplicate_input(api):
    obj, task, client = api
    async with client:
        r = await client.post(
            "/rules/1/binding", json={"tmdb_id": 8, "season": 99, "episode_offset": 500}
        )
        assert r.status_code == 200
        mapping = obj.store.direct_binding("rule:1")
        assert mapping["season"] == 2 and mapping["episode_offset"] == 0
        obj.bridge.rule.return_value.update(season=3, episode_offset=-12)
        data = (await client.get("/rules/1/library")).json()
        assert data["mapping"]["season"] == 3
        assert data["mapping"]["episode_offset"] == -12
        task["rule_mapping"] = {"season": 3, "episode_offset": -12}
        assert obj.store.binding(task)["episode_offset"] == -12
        obj.store.bind(task["key"], {"tmdb_id": 8, "season": 0, "episode_offset": 0})
        assert obj.store.binding(task)["season"] == 0
