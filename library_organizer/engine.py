import asyncio
import errno
import hashlib
import json
import os
import secrets

from .models import Mapping
from .planner import build, contained, fingerprint


def digest(value):
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, ensure_ascii=False).encode()
    ).hexdigest()


class Engine:
    def __init__(self, store, bridge, options):
        self.store, self.bridge, self.options = store, bridge, options
        self.lock = asyncio.Lock()
        self.replay_lock = asyncio.Lock()

    def maintenance(self):
        return self.store

    def settings_digest(self):
        return digest(self.options.model_dump(exclude={"jellyfin_api_key"}))

    async def preview(self, rule_id=None):
        async with self.lock:
            tasks = await self.bridge.scan()
            if rule_id is not None:
                tasks = [t for t in tasks if t.get("rule_id") == rule_id]
            items = []
            mappings = {}
            for task in tasks:
                raw = self.store.binding(task)
                mappings[task["key"]] = raw
                if raw:
                    mapping = Mapping.model_validate(raw)
                    metadata = await self.bridge.metadata(mapping)
                    entries = build(task, mapping, metadata, self.options)
                else:
                    entries = [
                        {
                            "key": task["key"] + ":" + str(f["index"]),
                            "task": task["key"],
                            "source": f["source"],
                            "name": f["name"],
                            "target": None,
                            "fingerprint": f.get("fingerprint"),
                            "kind": "unknown",
                            "reason": "请先绑定 TMDB 条目",
                        }
                        for f in task["files"]
                    ]
                for entry in entries:
                    entry["rule_id"] = task.get("rule_id")
                items.extend(entries)
            # Cross-task target collisions are never decided by iteration order.
            by_target = {}
            for item in items:
                if item["target"]:
                    by_target.setdefault(item["target"], []).append(item)
            for group in by_target.values():
                if len(group) > 1:
                    for i in group:
                        i["target"] = None
                        i["reason"] = "不同任务目标重名；请开启版本后缀或修改映射"
            for i in items:
                i["old_targets"] = [
                    r["target"] for r in self.store.owned(i["key"]) if r["target"] != i["target"]
                ]
                if i["target"]:
                    try:
                        target = contained(self.options.library_root, i["target"])
                        if target.exists() and fingerprint(target) != i["fingerprint"]:
                            i["reason"] = "目标已存在且不是同一源文件"
                            i["target"] = None
                    except ValueError as e:
                        i["reason"] = str(e)
                        i["target"] = None
            payload = {
                "items": items,
                "rule_id": rule_id,
                "tasks_digest": digest(tasks),
                "settings_digest": self.settings_digest(),
                "mappings": mappings,
            }
            token = secrets.token_urlsafe(24)
            self.store.plan(token, payload)
            return {
                "token": token,
                "items": items,
                "linkable": sum(bool(i["target"]) for i in items),
                "held": sum(not i["target"] for i in items),
            }

    def link(self, item):
        if not item["target"]:
            # Keep prior ownership when a changed mapping is ambiguous or blocked.
            return {"key": item["key"], "state": "held", "detail": item["reason"]}
        try:
            src = contained(self.options.source_root, item["source"])
            dst = contained(self.options.library_root, item["target"])
            if fingerprint(src) != item["fingerprint"]:
                raise ValueError("源文件已变化，请重新预览")
            ownership = next(
                (r for r in self.store.owned(item["key"]) if r["target"] == str(dst)), None
            )
            owned = bool(ownership and json.loads(ownership["fingerprint"]) == item["fingerprint"])
            if dst.exists():
                if fingerprint(dst) != item["fingerprint"]:
                    raise ValueError("目标已存在，未覆盖")
                if not owned:
                    return {
                        "key": item["key"],
                        "state": "exists",
                        "detail": "已有同源硬链，未接管其所有权",
                    }
            else:
                dst.parent.mkdir(parents=True, exist_ok=True)
                dst = contained(self.options.library_root, dst)
                self.store.claim(item)  # durable intent before filesystem mutation
                self.store.record(item, "linking")
                os.link(src, dst, follow_symlinks=False)
                if fingerprint(dst) != item["fingerprint"]:
                    raise ValueError("硬链校验失败")
            detail = ""
            # Only remove links explicitly owned by this plugin, after new link verified.
            for old in item.get("old_targets", []):
                ownership = next(
                    (r for r in self.store.owned(item["key"]) if r["target"] == old), None
                )
                if not ownership:
                    continue
                old_path = contained(self.options.library_root, old)
                if old_path.exists():
                    if fingerprint(old_path) != json.loads(ownership["fingerprint"]):
                        detail = "旧路径已改变，已保留"
                        continue
                    old_path.unlink()
                self.store.forget(old)
            self.store.record(item, "linked", detail)
            return {"key": item["key"], "state": "linked", "detail": detail}
        except (OSError, ValueError) as e:
            reason = (
                "下载与媒体库不支持硬链（跨文件系统）；未复制文件"
                if isinstance(e, OSError) and e.errno == errno.EXDEV
                else str(e)
            )
            # Preserve any existing ownership record; failed plans can be retried safely.
            return {"key": item["key"], "state": "failed", "detail": reason}

    async def apply(self, token, rule_id=None):
        async with self.replay_lock, self.lock:
            if not self.store.get("active", False):
                raise ValueError("请先启用媒体库接管")
            health = self.bridge.health()
            if health["native_rename"] or health["native_hardlink"]:
                raise ValueError("原生整理或硬链仍启用，已停止以避免并发修改")
            plan = self.store.take_plan(token)
            if plan.get("rule_id") != rule_id:
                raise ValueError("预览不属于当前番剧，请重新预览")
            tasks = await self.bridge.scan()
            if rule_id is not None:
                tasks = [t for t in tasks if t.get("rule_id") == rule_id]
            if (
                plan["tasks_digest"] != digest(tasks)
                or plan["settings_digest"] != self.settings_digest()
            ):
                raise ValueError("文件或配置已变化，请重新预览")
            for task in tasks:
                if plan["mappings"].get(task["key"]) != self.store.binding(task):
                    raise ValueError("TMDB 映射已变化，请重新预览")
            backup = str(self.store.backup())
            results = [self.link(i) for i in plan["items"]]
            self.store.set("last_results", results)
            retained = [
                i
                for i in self.store.get("last_items", [])
                if rule_id is not None and i.get("rule_id") != rule_id
            ]
            self.store.set("last_items", retained + plan["items"])
            for rid in {t.get("rule_id") for t in tasks}:
                if rid is None:
                    continue
                keys = {i["key"] for i in plan["items"] if i.get("rule_id") == rid}
                self.store.set(f"last_results:rule:{rid}", [r for r in results if r["key"] in keys])
            refresh = (
                await self.bridge.refresh_jellyfin()
                if any(r["state"] == "linked" for r in results)
                else "无新增链接"
            )
            self.store.set("jellyfin_refresh", refresh)
            return {"results": results, "backup": backup, "jellyfin": refresh}

    async def tick(self):
        if not self.store.get("active", False) or not self.store.get("automatic", False):
            return
        # Retry transient failure with bounded backoff; unchanged permanent conflicts stay visible.
        now = __import__("time").time()
        if now < self.store.get("retry_at", 0):
            return
        try:
            plan = await self.preview()
            signature = digest(plan["items"])
            last = self.store.get("automatic_signature")
            if last == signature:
                return
            result = await self.apply(plan["token"])
            failed = any(r["state"] == "failed" for r in result["results"])
            if failed:
                attempts = min(self.store.get("attempts", 0) + 1, 8)
                self.store.set("attempts", attempts)
                self.store.set("retry_at", now + min(3600, 30 * 2**attempts))
            else:
                self.store.set("attempts", 0)
                self.store.set("automatic_signature", signature)
            self.store.set("error", None)
        except Exception as e:
            self.store.set("error", str(e))
            self.store.set("retry_at", now + 300)
