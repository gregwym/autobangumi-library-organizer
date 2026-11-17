import asyncio
import errno
import hashlib
import json
import os
import secrets
import time
from pathlib import Path

from .collections import update_collections
from .identity import from_ab
from .models import Mapping
from .planner import build, contained, destination, fingerprint
from .transfer import publish


def digest(value):
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, ensure_ascii=False).encode()
    ).hexdigest()


class Engine:
    def __init__(self, store, bridge, options):
        self.store, self.bridge, self.options = store, bridge, options
        self.lock = asyncio.Lock()
        self.replay_lock = asyncio.Lock()
        self._progress = self.store.get("progress")
        if self._progress and self._progress["state"] == "running":
            self._progress = {**self._progress, "state": "interrupted", "current": ""}

    def progress(self, rule_id=None):
        value = self._progress
        if value and (rule_id is None or value["rule_id"] in (None, rule_id)):
            return {**value, "counts": dict(value["counts"])}
        return None

    def run_items(self, items, rule_id):
        # Keep WAL open across per-file connections without holding a transaction.
        # Otherwise closing the last connection checkpoints/recreates it each time.
        # Every ownership intent still commits durably before filesystem publication.
        with self.store.db() as reader:
            reader.execute("SELECT key FROM kv LIMIT 1").fetchall()
            return self._run_items(items, rule_id)

    def _run_items(self, items, rule_id):
        self._progress = {
            "state": "running",
            "rule_id": rule_id,
            "total": len(items),
            "completed": 0,
            "current": "",
            "counts": {},
            "started_at": time.time(),
        }
        self.store.set("progress", self._progress)
        results = []
        try:
            for item in items:
                self._progress = {**self._progress, "current": item["name"]}
                result = self.link(item)
                results.append(result)
                counts = dict(self._progress["counts"])
                kind = (
                    "reused"
                    if result.get("action") == "relocated"
                    else result.get("method")
                    if result["state"] == "linked"
                    else result["state"]
                )
                counts[kind] = counts.get(kind, 0) + 1
                self._progress = {**self._progress, "completed": len(results), "counts": counts}
            self._progress = {
                **self._progress,
                "state": "completed",
                "current": "",
                "finished_at": time.time(),
            }
            return results
        except BaseException:
            self._progress = {**self._progress, "state": "interrupted"}
            raise
        finally:
            self.store.set("progress", self._progress)

    def maintenance(self):
        return self.store

    def settings_digest(self):
        return digest({"layout": 6, "options": self.options.model_dump()})

    def ownership(self, item, target):
        return next((r for r in self.store.owned(item["key"]) if r["target"] == str(target)), None)

    def valid_owned(self, item, target, ownership):
        return bool(
            ownership
            and json.loads(ownership["source_fingerprint"]) == item["fingerprint"]
            and json.loads(ownership["fingerprint"]) == fingerprint(target)
        )

    async def resolve(self, task, raw):
        if raw and raw.get("tmdb_id"):
            mapping = Mapping.model_validate(raw)
            return mapping, await self.bridge.metadata(mapping)
        return from_ab(task, raw)

    async def preview(self, rule_id=None, bound_only=False, automatic=False):
        async with self.lock:
            tasks = await self.bridge.scan(
                rule_id=rule_id, bound_only=bound_only, associated_only=automatic
            )
            if rule_id is not None:
                tasks = [t for t in tasks if t.get("rule_id") == rule_id]
            items = []
            mappings = {}
            for task in tasks:
                raw = self.store.binding(task)
                mappings[task["key"]] = raw
                try:
                    mapping, metadata = await self.resolve(task, raw)
                    entries = await asyncio.to_thread(build, task, mapping, metadata, self.options)
                except (ValueError, OSError) as error:
                    entries = [
                        {
                            "key": task["key"] + ":" + str(f["index"]),
                            "task": task["key"],
                            "source": f["source"],
                            "name": f["name"],
                            "target": None,
                            "fingerprint": f.get("fingerprint"),
                            "kind": "unknown",
                            "reason": str(error),
                            "retryable": bool(raw and raw.get("tmdb_id")),
                        }
                        for f in task["files"]
                    ]
                for entry in entries:
                    entry["rule_id"] = task.get("rule_id")
                    entry["restore_missing"] = not automatic
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
                        target = destination(self.options, i["target"])
                        if (
                            self.options.music_root
                            and Path(self.options.music_root).resolve() in target.parents
                            and not Path(self.options.music_root).is_dir()
                        ):
                            raise ValueError("音乐库根目录不存在或尚未挂载，已保留原归档")
                        if (
                            target.exists()
                            and fingerprint(target) != i["fingerprint"]
                            and not self.valid_owned(i, target, self.ownership(i, target))
                        ):
                            i["reason"] = "目标已存在且不是同一源文件"
                            i["target"] = None
                    except ValueError as e:
                        i["reason"] = str(e)
                        i["target"] = None
            blocked = {i["key"] for i in items if not i["target"]}
            for i in items:
                if i.get("video_key") in blocked:
                    i["target"] = None
                    i["reason"] = "对应视频有冲突，暂不挂载字幕或音轨"
            payload = {
                "items": items,
                "rule_id": rule_id,
                "bound_only": bound_only,
                "automatic": automatic,
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
            expected = item["fingerprint"]
            if item.get("replay_source"):
                src = destination(self.options, item["replay_source"])
                expected = item["replay_fingerprint"]
                if not self.valid_owned(item, src, self.ownership(item, src)):
                    raise ValueError("归档副本所有权或内容已变化，请重新预览")
            dst = destination(self.options, item["target"])
            if fingerprint(src) != expected:
                raise ValueError("源文件已变化，请重新预览")
            archive_changed = (
                self.ensure_archive(item["archive_root"]) if item.get("archive_root") else False
            )
            if self.options.music_root and Path(self.options.music_root).resolve() in dst.parents:
                if not Path(self.options.music_root).is_dir():
                    raise ValueError("音乐库根目录不存在或尚未挂载，已保留原归档")
            ownership = self.ownership(item, dst)
            if (
                not dst.exists()
                and ownership
                and json.loads(ownership["source_fingerprint"]) == item["fingerprint"]
                and self.store.completed(item)
                and not item.get("restore_missing", True)
            ):
                return {
                    "key": item["key"],
                    "state": "skipped",
                    "changed": False,
                    "detail": "已整理的目标被移除，自动扫描不重建；可手动预览补链",
                }
            method = ownership["method"] if ownership else None
            action = "unchanged" if dst.exists() else "created"
            if dst.exists():
                if not self.valid_owned(item, dst, ownership):
                    if fingerprint(dst) != item["fingerprint"]:
                        raise ValueError("目标已存在，未覆盖")
                    return {
                        "key": item["key"],
                        "state": "exists",
                        "method": "hardlink",
                        "changed": archive_changed,
                        "detail": "已有同源硬链，未接管其所有权",
                    }
            else:
                dst.parent.mkdir(parents=True, exist_ok=True)
                dst = destination(self.options, dst)
                # A naming/mapping edit can reuse our verified existing replica
                # on the library filesystem, instead of copying the video again.
                for old in item.get("old_targets", []):
                    try:
                        old_path = destination(self.options, old)
                    except ValueError:
                        continue
                    prior = self.ownership(item, old_path)
                    if old_path.exists() and self.valid_owned(item, old_path, prior):
                        method = prior["method"]
                        try:
                            self.store.claim(item, fingerprint(old_path), method)
                            os.link(old_path, dst, follow_symlinks=False)
                        except OSError as error:
                            if error.errno not in {errno.EXDEV, errno.EOPNOTSUPP, errno.ENOSYS}:
                                raise
                            method = publish(
                                old_path,
                                dst,
                                fingerprint(old_path),
                                lambda fp, mode: self.store.claim(item, fp, mode),
                            )
                        action = "relocated"
                        break
                else:
                    method = publish(
                        src,
                        dst,
                        expected,
                        lambda fp, mode: self.store.claim(item, fp, mode),
                    )
                if not self.valid_owned(item, dst, self.ownership(item, dst)):
                    raise ValueError("目标文件校验失败")
            detail = ""
            obsolete = []
            # Only remove links explicitly owned by this plugin, after new link verified.
            for old in item.get("old_targets", []):
                ownership = next(
                    (r for r in self.store.owned(item["key"]) if r["target"] == old), None
                )
                if not ownership:
                    continue
                try:
                    old_path = destination(self.options, old)
                except ValueError:
                    detail = "旧目录已不在配置范围，已保留"
                    continue
                if old_path.exists():
                    if fingerprint(old_path) != json.loads(ownership["fingerprint"]):
                        detail = "旧路径已改变，已保留"
                        continue
                    old_path.unlink()
                    self.prune_empty_music_directories(old_path.parent)
                obsolete.append(old)
            label = {"hardlink": "硬链", "reflink": "Reflink", "copy": "普通复制"}[method]
            if action == "relocated":
                label = "复用原副本（" + label + "）"
            detail = "；".join(filter(None, (label, detail, item.get("notice"))))
            self.store.record(item, "linked", detail, obsolete=obsolete)
            return {
                "key": item["key"],
                "state": "exists" if action == "unchanged" and not obsolete else "linked",
                "method": method,
                "action": action,
                "changed": action != "unchanged" or bool(obsolete) or archive_changed,
                "detail": detail,
            }
        except (OSError, ValueError) as e:
            # Preserve any existing ownership record; failed plans can be retried safely.
            return {"key": item["key"], "state": "failed", "detail": str(e)}

    def prune_empty_music_directories(self, directory):
        """Remove empty old music wrappers after verified file relocation only."""
        if not self.options.music_root:
            return
        root = Path(self.options.music_root).resolve()
        while root in directory.parents:
            try:
                directory = contained(root, directory)
                directory.rmdir()
            except OSError, ValueError:
                break
            directory = directory.parent

    def ensure_archive(self, root):
        root = contained(self.options.library_root, root)
        if root.name != "_resources":
            raise ValueError("归档目录无效")
        root.mkdir(parents=True, exist_ok=True)
        marker = contained(self.options.library_root, root / ".ignore")
        try:
            with marker.open("xb") as handle:
                handle.flush()
                os.fsync(handle.fileno())
        except FileExistsError:
            if not marker.is_file() or marker.stat().st_size:
                raise ValueError("归档目录已有非空 .ignore，未覆盖；请确认排除规则")
            return False
        return True

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
            tasks = await self.bridge.scan(
                rule_id=rule_id,
                bound_only=plan.get("bound_only", False),
                associated_only=plan.get("automatic", False),
            )
            if rule_id is not None:
                tasks = [t for t in tasks if t.get("rule_id") == rule_id]
            if (
                plan["tasks_digest"] != digest(tasks)
                or plan["settings_digest"] != self.settings_digest()
            ):
                raise ValueError("文件或配置已变化，请重新预览")
            for task in tasks:
                if plan["mappings"].get(task["key"]) != self.store.binding(task):
                    raise ValueError("整理映射已变化，请重新预览")
            backup = str(self.store.backup())
            # Large copies must not block AB's HTTP/event loop. Retain the lock
            # until the worker stops even if a client disconnects/cancels.
            worker = asyncio.create_task(asyncio.to_thread(self.run_items, plan["items"], rule_id))
            try:
                results = await asyncio.shield(worker)
            except asyncio.CancelledError:
                await worker
                raise
            for item, result in zip(plan["items"], results, strict=True):
                result.update(name=item["name"], source=item["source"], target=item["target"])
            self.store.set("last_results", results)
            processed = {i["key"] for i in plan["items"]}
            retained = [i for i in self.store.get("last_items", []) if i["key"] not in processed]
            self.store.set("last_items", retained + plan["items"])
            for rid in {t.get("rule_id") for t in tasks}:
                if rid is None:
                    continue
                keys = {i["key"] for i in plan["items"] if i.get("rule_id") == rid}
                previous = [
                    r
                    for r in self.store.get(f"last_results:rule:{rid}", [])
                    if r["key"] not in keys
                ]
                self.store.set(
                    f"last_results:rule:{rid}", previous + [r for r in results if r["key"] in keys]
                )
            collections = await asyncio.to_thread(
                update_collections, self.store, self.options, plan["items"], results
            )
            self.store.set("collection_results", collections)
            return {"results": results, "backup": backup, "collections": collections}

    async def tick(self):
        if not self.store.get("active", False) or not self.store.get("automatic", False):
            return
        # Retry transient failure with bounded backoff; unchanged permanent conflicts stay visible.
        now = __import__("time").time()
        if now < self.store.get("retry_at", 0):
            return
        try:
            plan = await self.preview(automatic=True)
            signature = digest({"items": plan["items"], "settings": self.settings_digest()})
            last = self.store.get("automatic_signature")
            if last == signature:
                return
            result = await self.apply(plan["token"])
            failed = any(i.get("retryable") for i in plan["items"]) or any(
                r["state"] == "failed" for r in result["results"] + result["collections"]
            )
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
