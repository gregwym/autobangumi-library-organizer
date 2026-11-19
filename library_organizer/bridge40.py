"""All private AB 4.0 imports are isolated here; see docs/UPSTREAM_API.md."""

import json
import os
import shutil
import time
from pathlib import Path
from urllib.parse import urlencode

from .association import Associations
from .audit import Audit
from .planner import VIDEO, contained, destination, fingerprint, task_key
from .resources import MUSIC


class Bridge40:
    def __init__(self, options, store, audit=None):
        self.audit = audit or Audit()
        self.options, self.store = options, store
        self.original = None

    def setup(self):
        from module.conf import VERSION
        from module.manager.renamer import Renamer

        if VERSION != "4.0.0":
            raise ValueError("此适配层仅支持 AB 4.0.0")
        self.remove_legacy_media_server_options()
        self.original = Renamer.rename
        bridge = self

        async def guarded(renamer):
            if bridge.store.get("active", False):
                return []
            return await bridge.original(renamer)

        self.wrapper = guarded
        Renamer.rename = guarded

    def remove_legacy_media_server_options(self):
        from module.conf import settings
        from module.conf.config import CONFIG_PATH

        previous = settings.plugins.options.get("library-organizer", {})
        legacy = {"jellyfin_url", "jellyfin_api_key", "jellyfin_library_root"}
        if not legacy.intersection(previous):
            return
        cleaned = {key: value for key, value in previous.items() if key not in legacy}
        backup = self.store.backup()
        if CONFIG_PATH.exists():
            shutil.copy2(CONFIG_PATH, backup / "config.json")
            os.chmod(backup / "config.json", 0o600)
        data = settings.model_dump()
        data["plugins"]["options"]["library-organizer"] = cleaned
        temporary = CONFIG_PATH.with_name(CONFIG_PATH.name + ".organizer.tmp")
        with temporary.open("w") as handle:
            os.chmod(temporary, 0o600)
            json.dump(data, handle, ensure_ascii=False, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, CONFIG_PATH)
        settings.plugins.options["library-organizer"] = cleaned

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

    @staticmethod
    def rule_data(rule):
        # Match AB's gen_save_path: invalid season offsets fall back to the
        # original season; ordinary episodes must not become Season 00.
        season = rule.season + rule.season_offset
        if season < (0 if rule.episode_type == "special" else 1):
            season = rule.season
        return {
            "id": rule.id,
            "title": rule.official_title,
            "year": rule.year or "",
            "season": season,
            "episode_offset": rule.episode_offset,
            "episode_type": rule.episode_type,
            "media_type": "movie" if rule.episode_type == "movie" else "tv",
        }

    async def scan(
        self, rule_id=None, bound_only=False, discover_unassigned=False, associated_only=False
    ):
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
        seen = set()
        for instance in downloader_ids():
            async with DownloadClient(instance) as client:
                infos = await client.get_torrent_info(
                    category=None, status_filter="completed", tag=None
                )
                for info in infos:
                    seen.add(task_key(instance, str(info["hash"])))
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
                    if associated_only and matched_rule is None:
                        continue
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
                        "completed_at": float(info.get("completion_on") or 0),
                        "name": info["name"],
                        "rule_id": rid,
                        "association": association,
                        "ab_metadata": self.rule_data(matched_rule) if matched_rule else {},
                        "rule_mapping": {
                            k: self.rule_data(matched_rule)[k] for k in ("season", "episode_offset")
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
        if self.options.music_root:
            tasks.extend(
                await asyncio.to_thread(
                    self.archived_music,
                    seen,
                    associations,
                    assignments,
                    bindings,
                    rule_id,
                    bound_only,
                )
            )
        self.audit.emit(
            "download.scan",
            level=10,
            tasks=len(tasks),
            files=sum(len(t["files"]) for t in tasks),
            rule_id=rule_id,
            reads=["AB history", "downloader completed tasks/files", "source file metadata"],
            writes=["plugin task cache"],
        )
        return tasks

    def archived_music(self, seen, associations, assignments, bindings, rule_id, bound_only=False):
        """Replay known archived music only, even after its qB task/source was removed."""
        owned = {}
        for row in self.store.owned():
            owned.setdefault(row["key"], []).append(row)
        recovered = []
        for cached in self.store.tasks():
            key = cached["key"]
            if key in seen:
                continue
            rid, evidence = associations.resolve(
                cached.get("instance", "default"), cached.get("task_id", ""), assignments.get(key)
            )
            if rid is None or rule_id is not None and rid != rule_id:
                continue
            if bound_only and key not in bindings and f"rule:{rid}" not in bindings:
                continue
            rule = associations.rules.get(rid)
            files = []
            for source in cached.get("files", []):
                path = Path(source["name"])
                if path.suffix.lower() not in MUSIC | {".cue", ".lrc", ".elrc"} and not (
                    path.stem.lower() in {"cover", "folder"}
                    and path.suffix.lower() in {".jpg", ".jpeg", ".png", ".webp"}
                ):
                    continue
                for row in owned.get(key + ":" + str(source["index"]), []):
                    try:
                        replica = destination(self.options, row["target"])
                        # Only already archived/music paths; never use a linked TV audio sidecar.
                        if (
                            "_resources" not in replica.parts
                            and Path(self.options.music_root).resolve() not in replica.parents
                        ):
                            continue
                        if json.loads(row["source_fingerprint"]) != source.get(
                            "fingerprint"
                        ) or fingerprint(replica) != json.loads(row["fingerprint"]):
                            continue
                        files.append(
                            {
                                **source,
                                "progress": 1,
                                "replay_source": str(replica),
                                "replay_fingerprint": fingerprint(replica),
                            }
                        )
                        break
                    except OSError, ValueError:
                        continue
            if any(Path(f["name"]).suffix.lower() in MUSIC for f in files):
                recovered.append(
                    {
                        **cached,
                        "files": files,
                        "reference_videos": [
                            f["name"]
                            for f in cached.get("files", [])
                            if Path(f["name"]).suffix.lower() in VIDEO
                        ],
                        "rule_id": rid,
                        "association": evidence,
                        "ab_metadata": self.rule_data(rule) if rule else {},
                        "rule_mapping": {
                            k: self.rule_data(rule)[k] for k in ("season", "episode_offset")
                        }
                        if rule
                        else {},
                    }
                )
        return recovered

    async def notify_inbox(self, event):
        # AB 4.0 has no acknowledged, inbox-only SDK method yet.
        from module.notification.inbox import record_event

        return await record_event(event)

    async def tmdb(self, path, params=None):
        from module.network import RequestContent
        from module.parser.analyser.tmdb_parser import _api_key, _tmdb_url

        params = {"api_key": _api_key(), "language": "zh-CN", **(params or {})}
        url = _tmdb_url() + "/3/" + path + "?" + urlencode(params)
        self.audit.emit("metadata.read", level=10, endpoint=path)
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
        key = f"tmdb:{mapping.media_type}:{mapping.tmdb_id}:{mapping.season}:specials={bool(mapping.specials)}"
        cached = self.store.get(key)
        if cached and time.time() - cached["at"] < 3600:
            self.audit.emit("metadata.cache.read", level=10, key=key)
            return cached["data"]
        data = await self.tmdb(f"{mapping.media_type}/{mapping.tmdb_id}")
        episodes = []
        specials = []
        if mapping.media_type == "tv":
            season = await self.tmdb(f"tv/{mapping.tmdb_id}/season/{mapping.season}")
            episodes = [e["episode_number"] for e in season.get("episodes", [])]
            if not episodes:
                raise ValueError("TMDB 中未找到该季集数")
            if mapping.specials:
                season_zero = await self.tmdb(f"tv/{mapping.tmdb_id}/season/0")
                specials = [e["episode_number"] for e in season_zero.get("episodes", [])]
        result = {
            "title": data.get("name") or data.get("title"),
            "year": (data.get("first_air_date") or data.get("release_date") or "")[:4],
            "episodes": episodes,
            "specials": specials,
        }
        if not result["title"]:
            raise ValueError("TMDB 条目没有标题")
        self.store.set(key, {"at": time.time(), "data": result})
        self.audit.emit("metadata.cache.updated", level=10, key=key)
        return result

    async def rule(self, rule_id):
        from module.database import Database
        from module.models import Bangumi

        async with Database() as db:
            rule = await db.session.get(Bangumi, rule_id)
            if rule is None or rule.deleted:
                raise ValueError("番剧不存在或已删除")
            return self.rule_data(rule)

    async def legacy_layout(self):
        """Read only the native hardlink plugin's layout and migration evidence."""
        from module.conf import settings
        from module.database import Database
        from module.models.plugin_kv import PluginKV
        from sqlmodel import select

        options = settings.plugins.options.get("hardlink", {})
        async with Database() as db:
            rows = (
                (
                    await db.session.execute(
                        select(PluginKV).where(
                            PluginKV.plugin_id == "hardlink", PluginKV.key.startswith("link:")
                        )
                    )
                )
                .scalars()
                .all()
            )
        records = {}
        for row in rows:
            try:
                records[row.key.removeprefix("link:")] = json.loads(row.value)
            except ValueError, TypeError:
                continue
        return {
            "source_root": options.get("source_root", self.options.source_root),
            "library_root": options.get("library_root", self.options.library_root),
            "records": records,
        }
