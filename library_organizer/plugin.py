from pathlib import Path

from ab_sdk import Plugin, points, provider
from ab_sdk.tasks import ScheduledTask
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from .bridge40 import Bridge40
from .engine import Engine
from .models import Binding, Confirm, Options
from .store import Store
from .updater import Updater


class Toggle(BaseModel):
    enabled: bool


class UpdateRequest(BaseModel):
    version: str = Field(pattern=r"^\d+\.\d+\.\d+$", max_length=40)


class LibraryOrganizer(Plugin[Options]):
    config_model = Options

    async def setup(self):
        self.store = Store(self.ctx.data_dir / "library.sqlite3")
        self.bridge = Bridge40(self.config, self.store)
        self.bridge.setup()
        self.engine = Engine(self.store, self.bridge, self.config)
        self.updater = Updater(Path(__file__).resolve().parents[1], self.ctx.data_dir, self.engine)

    async def teardown(self):
        self.bridge.teardown()

    @provider(points.SCHEDULED_TASK, id="organize")
    def organize_task(self):
        return ScheduledTask(run=self.engine.tick, interval=60, initial_delay=30)

    @provider(points.SCHEDULED_TASK, id="check-update")
    def update_task(self):
        return ScheduledTask(run=self.updater.check, interval=21600, initial_delay=60)

    @provider(points.API_ROUTER, id="api")
    def api(self):
        router = APIRouter()

        @router.get("/status")
        async def status():
            return {
                "active": self.store.get("active", False),
                "automatic": self.store.get("automatic", False),
                "health": self.bridge.health(),
                "error": self.store.get("error"),
                "results": self.store.get("last_results", []),
                "jellyfin": self.store.get("jellyfin_refresh"),
                "update": self.updater.status(),
            }

        @router.post("/scan")
        async def scan():
            async with self.engine.lock:
                tasks = await self.bridge.scan()
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

        @router.get("/search")
        async def search(query: str, kind: str = "tv"):
            try:
                return await self.bridge.search(query, kind)
            except ValueError as e:
                raise HTTPException(422, str(e)) from e

        @router.post("/binding")
        async def binding(body: Binding):
            async with self.engine.lock:
                task = next((t for t in self.store.tasks() if t["key"] == body.task_key), None)
                if not task:
                    raise HTTPException(404, "任务不存在，请重新扫描")
                if body.rule_id is not None and task["rule_id"] != body.rule_id:
                    raise HTTPException(409, "规则关联已变化")
                try:
                    await self.bridge.metadata(body.mapping)
                except ValueError as e:
                    raise HTTPException(422, str(e)) from e
                from .planner import VIDEO

                names = {
                    f["name"] for f in task["files"] if Path(f["name"]).suffix.lower() in VIDEO
                }
                if (
                    not set(body.mapping.episodes) <= names
                    or body.mapping.main_file
                    and body.mapping.main_file not in names
                ):
                    raise HTTPException(422, "文件覆盖映射必须使用完整源文件名")
                if body.rule_id is not None and (body.mapping.episodes or body.mapping.main_file):
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
            self.store.set("automatic", body.enabled)
            if body.enabled:
                self.store.set("retry_at", 0)
                self.store.set("automatic_signature", None)
            return {"automatic": body.enabled}

        @router.post("/jellyfin/refresh")
        async def refresh():
            return {"status": await self.bridge.refresh_jellyfin()}

        @router.post("/jellyfin/verify")
        async def verify_library():
            from .jellyfin import verify

            try:
                return await verify(self.config, self.store.get("last_items", []))
            except ValueError as e:
                raise HTTPException(422, str(e)) from e
            except Exception as e:
                raise HTTPException(502, "Jellyfin 核对失败，请检查连接设置") from e

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
