from pathlib import Path

from ab_sdk import Plugin, points, provider
from ab_sdk.tasks import ScheduledTask
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from .bridge40 import Bridge40
from .engine import Engine
from .models import Binding, Confirm, Mapping, Options
from .store import Store
from .updater import Updater


class Toggle(BaseModel):
    enabled: bool
    token: str | None = Field(default=None, min_length=20, max_length=100)


class TaskRequest(BaseModel):
    task_key: str = Field(min_length=1, max_length=64)


class UpdateRequest(BaseModel):
    version: str = Field(pattern=r"^\d+\.\d+\.\d+$", max_length=40)


class LibraryOrganizer(Plugin[Options]):
    config_model = Options

    async def setup(self):
        self.store = Store(self.ctx.data_dir / "library.sqlite3")
        self.bridge = Bridge40(self.config, self.store)
        await self.bridge.reconcile_history()
        self.bridge.setup()
        self.engine = Engine(self.store, self.bridge, self.config)
        self.updater = Updater(Path(__file__).resolve().parents[1], self.ctx.data_dir, self.engine)

    async def teardown(self):
        self.bridge.teardown()

    @provider(points.SCHEDULED_TASK, id="library-organizer-organize")
    def organize_task(self):
        return ScheduledTask(run=self.engine.tick, interval=60, initial_delay=30)

    @provider(points.SCHEDULED_TASK, id="library-organizer-check-update")
    def update_task(self):
        return ScheduledTask(run=self.updater.check, interval=21600, initial_delay=60)

    @provider(points.API_ROUTER, id="api")
    def api(self):
        router = APIRouter()

        def result_rows(rows):
            # Older releases kept full paths in last_items, but not in result rows.
            items = {i["key"]: i for i in self.store.get("last_items", [])}
            return [
                {
                    **{k: items.get(r["key"], {}).get(k) for k in ("name", "source", "target")},
                    **r,
                }
                for r in rows
            ]

        @router.get("/progress")
        async def progress(rule_id: int | None = None):
            return self.engine.progress(rule_id)

        async def scan_tasks(**kwargs):
            try:
                return await self.bridge.scan(**kwargs)
            except ValueError as e:
                raise HTTPException(422, str(e)) from e

        @router.get("/status")
        async def status():
            return {
                "active": self.store.get("active", False),
                "automatic": self.store.get("automatic", False),
                "health": self.bridge.health(),
                "error": self.store.get("error"),
                "results": result_rows(self.store.get("last_results", [])),
                "collections": self.store.get("collection_results", []),
                "update": self.updater.status(),
            }

        @router.post("/scan")
        async def scan():
            async with self.engine.lock:
                tasks = await scan_tasks()
                return [
                    {
                        "key": t["key"],
                        "name": t["name"],
                        "rule_id": t["rule_id"],
                        "suggested_title": t["suggested_title"],
                        "mapping": self.store.binding(t),
                        "files": [
                            {"name": f["name"], "complete": f["progress"] >= 1} for f in t["files"]
                        ],
                    }
                    for t in tasks
                ]

        async def require_rule(rid):
            try:
                return await self.bridge.rule(rid)
            except ValueError as e:
                raise HTTPException(404, str(e)) from e

        async def validate_mapping(mapping, task=None, rule=None):
            try:
                if mapping.tmdb_id:
                    await self.bridge.metadata(mapping)
                else:
                    from .identity import from_ab

                    from_ab(
                        {**(task or {}), "rule_id": rule["id"], "ab_metadata": rule},
                        mapping.model_dump(),
                    )
            except ValueError as e:
                raise HTTPException(422, str(e)) from e

        def task_view(task):
            return {
                "key": task["key"],
                "name": task["name"],
                "rule_id": task["rule_id"],
                "association": task.get("association", {}),
                "override": self.store.direct_binding(task["key"]),
                "files": [
                    {"name": f["name"], "complete": f["progress"] >= 1} for f in task["files"]
                ],
            }

        @router.get("/rules/{rid}/library")
        async def rule_library(rid: int):
            rule = await require_rule(rid)
            async with self.engine.lock:
                await self.bridge.reconcile_history()
            mapping = self.store.direct_binding(f"rule:{rid}")
            if mapping and mapping.get("follow_ab", True):
                mapping.update(
                    season=rule["season"], episode_offset=rule["episode_offset"], follow_ab=True
                )
            return {
                "rule": rule,
                "mapping": mapping,
                "effective_mapping": mapping
                or Mapping(
                    media_type=rule["media_type"],
                    season=rule["season"],
                    episode_offset=rule["episode_offset"],
                ).model_dump(),
                "metadata_source": "tmdb" if mapping and mapping.get("tmdb_id") else "ab",
                "active": self.store.get("active", False),
                "automatic": self.store.get("automatic", False),
                "results": result_rows(self.store.get(f"last_results:rule:{rid}", [])),
            }

        @router.post("/rules/{rid}/scan")
        async def rule_scan(rid: int):
            await require_rule(rid)
            async with self.engine.lock:
                tasks = await scan_tasks(rule_id=rid, discover_unassigned=True)
                return {
                    "tasks": [task_view(t) for t in tasks if t["rule_id"] == rid],
                    "unassigned": [task_view(t) for t in tasks if t["rule_id"] is None],
                }

        @router.post("/rules/{rid}/binding")
        async def rule_binding(rid: int, body: Mapping):
            rule = await require_rule(rid)
            if body.episodes or body.specials or body.main_file:
                raise HTTPException(422, "逐文件设置请保存在对应下载任务中")
            async with self.engine.lock:
                body = body.model_copy(
                    update={
                        "season": rule["season"],
                        "episode_offset": rule["episode_offset"],
                        "media_type": body.media_type if body.tmdb_id else rule["media_type"],
                    }
                )
                await validate_mapping(body, rule=rule)
                self.store.bind(f"rule:{rid}", {**body.model_dump(), "follow_ab": True})
            return {"saved": True}

        @router.post("/rules/{rid}/assign")
        async def assign_task(rid: int, body: TaskRequest):
            await require_rule(rid)
            async with self.engine.lock:
                task = next((t for t in await scan_tasks() if t["key"] == body.task_key), None)
                if task is None:
                    raise HTTPException(404, "下载任务不存在")
                if not task.get("association", {}).get("assignable", True):
                    raise HTTPException(
                        409, "此任务已有 AB 归属或归属冲突，请在 AB 修正，不能在插件中跨 entry 关联"
                    )
                if task["rule_id"] is not None and task["rule_id"] != rid:
                    raise HTTPException(409, "任务已关联其他番剧，不能直接改绑")
                self.store.set("assignment:" + body.task_key, rid)
                task["rule_id"] = rid
                self.store.save_task(task)
            return {"assigned": True}

        @router.post("/rules/{rid}/tasks/{task_key}/binding")
        async def task_binding(rid: int, task_key: str, body: Mapping):
            rule = await require_rule(rid)
            async with self.engine.lock:
                tasks = await scan_tasks()
                task = next(
                    (t for t in tasks if t["key"] == task_key and t["rule_id"] == rid), None
                )
                if task is None:
                    raise HTTPException(404, "该下载任务不属于当前番剧")
                from .planner import VIDEO

                names = {
                    f["name"] for f in task["files"] if Path(f["name"]).suffix.lower() in VIDEO
                }
                if (
                    not (body.episodes.keys() | body.specials.keys()) <= names
                    or body.main_file
                    and body.main_file not in names
                ):
                    raise HTTPException(422, "文件映射不属于该下载任务")
                await validate_mapping(body, task, rule)
                self.store.bind(task_key, body.model_dump())
            return {"saved": True}

        @router.post("/rules/{rid}/tasks/{task_key}/inherit")
        async def inherit(rid: int, task_key: str):
            await require_rule(rid)
            async with self.engine.lock:
                task = next(
                    (t for t in await scan_tasks() if t["key"] == task_key and t["rule_id"] == rid),
                    None,
                )
                if task is None:
                    raise HTTPException(404, "该下载任务不属于当前番剧")
                self.store.unbind(task_key)
            return {"inherited": True}

        @router.post("/rules/{rid}/preview")
        async def rule_preview(rid: int):
            await require_rule(rid)
            try:
                return await self.engine.preview(rule_id=rid)
            except (OSError, ValueError) as e:
                raise HTTPException(409, str(e)) from e

        @router.post("/rules/{rid}/apply")
        async def rule_apply(rid: int, body: Confirm):
            await require_rule(rid)
            try:
                return await self.engine.apply(body.token, rule_id=rid)
            except ValueError as e:
                raise HTTPException(409, str(e)) from e

        @router.get("/search")
        async def search(query: str, kind: str = "tv"):
            try:
                return await self.bridge.search(query, kind)
            except ValueError as e:
                raise HTTPException(422, str(e)) from e

        @router.post("/binding")
        async def binding(body: Binding):
            async with self.engine.lock:
                task = next((t for t in await scan_tasks() if t["key"] == body.task_key), None)
                if not task:
                    raise HTTPException(404, "任务不存在，请重新扫描")
                if body.rule_id is not None and task["rule_id"] != body.rule_id:
                    raise HTTPException(409, "规则关联已变化")
                rule = await require_rule(task["rule_id"]) if task["rule_id"] is not None else None
                if not body.mapping.tmdb_id and rule is None:
                    raise HTTPException(422, "请先将下载任务关联到 AB 番剧")
                await validate_mapping(body.mapping, task, rule)
                from .planner import VIDEO

                names = {
                    f["name"] for f in task["files"] if Path(f["name"]).suffix.lower() in VIDEO
                }
                if (
                    not (body.mapping.episodes.keys() | body.mapping.specials.keys()) <= names
                    or body.mapping.main_file
                    and body.mapping.main_file not in names
                ):
                    raise HTTPException(422, "文件覆盖映射必须使用完整源文件名")
                if body.rule_id is not None and (
                    body.mapping.episodes or body.mapping.specials or body.mapping.main_file
                ):
                    raise HTTPException(422, "逐文件映射仅适用于当前任务，不能应用到未来任务")
                self.store.bind(
                    "rule:" + str(body.rule_id) if body.rule_id is not None else body.task_key,
                    body.mapping.model_dump(),
                )
                if body.rule_id is not None:
                    self.store.unbind(body.task_key)
                return {"saved": True}

        @router.post("/preview")
        async def preview():
            try:
                return await self.engine.preview()
            except (OSError, ValueError) as e:
                raise HTTPException(409, str(e)) from e

        @router.post("/apply")
        async def apply(body: Confirm):
            try:
                return await self.engine.apply(body.token)
            except ValueError as e:
                raise HTTPException(409, str(e)) from e

        @router.post("/activate")
        async def activate(request: Request):
            # Native scheduler is paused and config backed up before takeover.
            return await self.bridge.activate(request.app.state.ctx)

        @router.post("/automatic")
        async def automatic(body: Toggle):
            if body.enabled:
                if not body.token:
                    raise HTTPException(409, "请先预览完整计划，确认后开启自动整理")
                try:
                    return await self.engine.apply(body.token, enable_automatic=True)
                except ValueError as e:
                    raise HTTPException(409, str(e)) from e
            async with self.engine.replay_lock, self.engine.lock:
                self.store.set("automatic", False)
            return {"automatic": False}

        @router.post("/automatic/preview")
        async def automatic_preview():
            try:
                return await self.engine.preview(automatic=True)
            except (OSError, ValueError) as e:
                raise HTTPException(409, str(e)) from e

        @router.post("/collections/refresh")
        async def refresh_collections():
            return await self.engine.refresh_collections()

        @router.get("/update")
        async def update_status():
            return self.updater.status()

        @router.post("/update/check")
        async def check_update():
            return await self.updater.check()

        @router.post("/update/install")
        async def install(body: UpdateRequest, request: Request):
            try:
                return await self.updater.install(body.version, request.app.state.ctx)
            except ValueError as e:
                raise HTTPException(409, str(e)) from e
            except Exception as e:
                raise HTTPException(502, "更新失败，已保留旧版；请稍后重试") from e

        return router
