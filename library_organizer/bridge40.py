"""All private AB 4.0 imports are isolated here; see docs/UPSTREAM_API.md."""

import json
import os
import shutil
import time
from pathlib import Path
from urllib.parse import urlencode

from .association import Associations
from .planner import contained, fingerprint, task_key


class Bridge40:
    def __init__(self, options, store):
        self.options, self.store = options, store
        self.original = None

    def setup(self):
        from module.conf import VERSION
        from module.manager.renamer import Renamer

        if VERSION != "4.0.0":
            raise ValueError("此适配层仅支持 AB 4.0.0")
        self.original = Renamer.rename
        bridge = self

        async def guarded(renamer):
            if bridge.store.get("active", False):
                return []
            return await bridge.original(renamer)

        self.wrapper = guarded
        Renamer.rename = guarded

    def teardown(self):
        from module.manager.renamer import Renamer

        if self.original and Renamer.rename is self.wrapper:
            Renamer.rename = self.original

    def health(self):
        from module.conf import settings

        return {
            "native_rename": settings.bangumi_manage.enable,
            "native_hardlink": settings.plugins.enabled.get("hardlink", False),
        }

    async def activate(self, context):
        from module.conf.config import CONFIG_PATH

        # Stop existing rename workers before ownership changes; persisted native disable
        # ensures unloading/crashing this plugin doesn't resume source renaming.
        running = context.is_running
        if running:
            await context.stop()
        try:
            backup = self.store.backup()
            shutil.copy2(CONFIG_PATH, backup / "config.json")
            data = json.loads(CONFIG_PATH.read_text())
            data.setdefault("bangumi_manage", {})["enable"] = False
            data.setdefault("plugins", {}).setdefault("enabled", {})["hardlink"] = False
            temp = CONFIG_PATH.with_name(CONFIG_PATH.name + ".organizer.tmp")
            temp.write_text(json.dumps(data, ensure_ascii=False, indent=2))
            os.replace(temp, CONFIG_PATH)
            self.store.set("active", True)
            await context.reload_settings()
            return {"active": True, "backup": str(backup)}
        finally:
            if running and not context.is_running:
                await context.start_tasks()

    def local_path(self, instance, path):
        remote = Path(path)
        maps = sorted(
            (m for m in self.options.path_map if m.downloader == instance),
            key=lambda m: len(m.remote),
            reverse=True,
        )
        for m in maps:
            try:
                relative = remote.relative_to(Path(m.remote))
            except ValueError:
                continue
            remote = Path(m.local) / relative
            break
        return (
            remote.resolve()
            if remote.resolve() == Path(self.options.source_root).resolve()
            else contained(self.options.source_root, remote)
        )

    async def associations(self):
        from module.database import Database
        from module.models import Bangumi, Torrent
        from sqlmodel import select

        async with Database() as db:
            rules = (
                (await db.session.execute(select(Bangumi).where(Bangumi.deleted.is_(False))))
                .scalars()
                .all()
            )
            records = (await db.session.execute(select(Torrent))).scalars().all()
        return Associations(records, rules)

    async def reconcile_history(self):
        import asyncio

        associations = await self.associations()
        await asyncio.to_thread(self.store.reconcile_history, associations)

    async def scan(self, rule_id=None, bound_only=False, discover_unassigned=False):
        import asyncio

        if not Path(self.options.source_root).is_dir():
            raise ValueError("下载根目录不存在或尚未挂载，请先检查插件路径配置")
        from module.downloader import DownloadClient
        from module.downloader.download_client import downloader_ids

        associations = await self.associations()
        rules = list(associations.rules.values())
        assignments, bindings = await asyncio.to_thread(self.store.scan_state)
        tasks = []
        saved = []
        for instance in downloader_ids():
            async with DownloadClient(instance) as client:
                infos = await client.get_torrent_info(
                    category=None, status_filter="completed", tag=None
                )
                for info in infos:
                    if float(info.get("progress", 0)) < 1:
                        continue
                    try:
                        base = self.local_path(instance, info["save_path"])
                    except ValueError:
                        continue
                    key = task_key(instance, str(info["hash"]))
                    rid, association = associations.resolve(
                        instance, str(info["hash"]), assignments.get(key)
                    )
                    if rule_id is not None and rid != rule_id:
                        if not (discover_unassigned and rid is None):
                            continue
                    matched_rule = next((r for r in rules if r.id == rid), None)
                    if bound_only and (
                        rid is None or key not in bindings and f"rule:{rid}" not in bindings
                    ):
                        continue
                    files = []
                    # Unknown tasks are offered by identity only in a single-entry UI.
                    # Do not inspect another entry's files just to populate a selector.
                    inspect_files = rule_id is None or rid == rule_id
                    listing = await client.get_torrent_files(info["hash"]) if inspect_files else []
                    for f in listing:
                        try:
                            src = contained(self.options.source_root, base / f["name"])
                            fp = (
                                fingerprint(src)
                                if src.is_file() and src.stat().st_size == f["size"]
                                else None
                            )
                        except ValueError, OSError:
                            continue
                        files.append(
                            {
                                "name": f["name"],
                                "index": f["index"],
                                "source": str(src),
                                "size": f["size"],
                                "progress": f.get("progress", 0),
                                "fingerprint": fp,
                            }
                        )
                    task = {
                        "key": key,
                        "instance": instance,
                        "task_id": info["hash"],
                        "name": info["name"],
                        "rule_id": rid,
                        "association": association,
                        "rule_mapping": {
                            "season": max(0, matched_rule.season + matched_rule.season_offset),
                            "episode_offset": matched_rule.episode_offset,
                        }
                        if matched_rule
                        else {},
                        "suggested_title": next(
                            (r.official_title for r in rules if r.id == rid), info["name"]
                        ),
                        "files": files,
                    }
                    tasks.append(task)
                    if inspect_files:
                        saved.append(task)
        await asyncio.to_thread(self.store.save_tasks, saved)
        return tasks

    async def tmdb(self, path, params=None):
        from module.network import RequestContent
        from module.parser.analyser.tmdb_parser import _api_key, _tmdb_url

        params = {"api_key": _api_key(), "language": "zh-CN", **(params or {})}
        url = _tmdb_url() + "/3/" + path + "?" + urlencode(params)
        async with RequestContent() as req:
            result = await req.get_json(url)
        if not isinstance(result, dict) or result.get("success") is False:
            raise ValueError("TMDB 请求失败，请稍后重试")
        return result

    async def search(self, query, kind):
        if kind not in {"tv", "movie"}:
            raise ValueError("媒体类型无效")
        data = await self.tmdb("search/" + kind, {"query": query})
        return [
            {
                "id": r["id"],
                "title": r.get("name") or r.get("title"),
                "year": (r.get("first_air_date") or r.get("release_date") or "")[:4],
            }
            for r in data.get("results", [])[:20]
        ]

    async def metadata(self, mapping):
        key = f"tmdb:{mapping.media_type}:{mapping.tmdb_id}:{mapping.season}"
        cached = self.store.get(key)
        if cached and time.time() - cached["at"] < 3600:
            return cached["data"]
        data = await self.tmdb(f"{mapping.media_type}/{mapping.tmdb_id}")
        episodes = []
        if mapping.media_type == "tv":
            season = await self.tmdb(f"tv/{mapping.tmdb_id}/season/{mapping.season}")
            episodes = [e["episode_number"] for e in season.get("episodes", [])]
            if not episodes:
                raise ValueError("TMDB 中未找到该季集数")
        result = {
            "title": data.get("name") or data.get("title"),
            "year": (data.get("first_air_date") or data.get("release_date") or "")[:4],
            "episodes": episodes,
        }
        if not result["title"]:
            raise ValueError("TMDB 条目没有标题")
        self.store.set(key, {"at": time.time(), "data": result})
        return result

    async def refresh_jellyfin(self):
        import httpx

        from .jellyfin import auth_headers

        if not self.options.jellyfin_url or not self.options.jellyfin_api_key:
            return "未配置"
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                r = await client.post(
                    self.options.jellyfin_url.rstrip("/") + "/Library/Refresh",
                    headers=auth_headers(self.options.jellyfin_api_key),
                )
                r.raise_for_status()
            return "已请求扫描（不代表识别验证完成）"
        except Exception:
            return "扫描请求失败；硬链已保留，可重试"

    async def rule(self, rule_id):
        from module.database import Database
        from module.models import Bangumi

        async with Database() as db:
            rule = await db.session.get(Bangumi, rule_id)
            if rule is None or rule.deleted:
                raise ValueError("番剧不存在或已删除")
            return {
                "id": rule.id,
                "title": rule.official_title,
                "season": max(0, rule.season + rule.season_offset),
                "episode_offset": rule.episode_offset,
                "media_type": "movie" if rule.episode_type == "movie" else "tv",
            }
