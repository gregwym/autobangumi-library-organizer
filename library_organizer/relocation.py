"""Reconcile music already moved outside AB. Never writes media files."""

import hashlib
import json
import secrets
import time
from pathlib import Path, PurePosixPath

from .collections import update_collections
from .planner import contained, fingerprint

TABLES = ("owned", "transfers", "files", "tasks", "bindings", "kv")


def snapshot(connection):
    return {
        table: [
            dict(row)
            for row in connection.execute(f"SELECT * FROM {table} ORDER BY 1")
            if table != "kv" or row["key"] not in {"progress", "music_relocation"}
        ]
        for table in TABLES
    }


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def checked_hash(root, path, expected=None):
    path = contained(root, path)
    before = fingerprint(path)
    if expected is not None and before != expected:
        raise ValueError("文件身份已变化，请重新预览")
    with path.open("rb") as handle:
        value = hashlib.file_digest(handle, "sha256").hexdigest()
    if fingerprint(path) != before:
        raise ValueError("文件在校验期间发生变化")
    return value, before


def inspect(store, options, old_root, report):
    old = PurePosixPath(old_root)
    new = Path(options.music_root)
    if (
        not old.is_absolute()
        or old == PurePosixPath("/")
        or ".." in old.parts
        or any(c in old_root for c in "\\\x00\r\n")
        or not options.music_root
        or not new.is_dir()
        or new.is_symlink()
    ):
        raise ValueError("先在插件选项保存已挂载的新音乐库路径，再填写原音乐库的绝对路径")
    old_root, new_root = str(old), str(new.resolve())
    target_root = PurePosixPath(new_root)
    if old.is_relative_to(target_root) or target_root.is_relative_to(old):
        raise ValueError("新旧音乐目录不能相同或重叠")
    for protected in (options.source_root, *options.video_roots, options.collections_root):
        if protected and (
            old.is_relative_to(protected) or PurePosixPath(protected).is_relative_to(old)
        ):
            raise ValueError("原音乐目录不能与当前下载、视频或合集目录重叠")
    with store.db() as connection:
        state = snapshot(connection)
    history = {row["key"]: row for row in state["files"]}
    transfers = {row["target"]: row for row in state["transfers"]}
    owners = {row["target"]: row for row in state["owned"]}
    selected = [row for row in state["owned"] if PurePosixPath(row["target"]).is_relative_to(old)]
    if not selected:
        raise ValueError("原目录下没有插件管理的音乐记录，未更新任何记录")
    items = []
    for index, row in enumerate(selected):
        target = str(target_root / PurePosixPath(row["target"]).relative_to(old))
        item = {"key": row["key"], "old_target": row["target"], "target": target}
        report(index, len(selected), target)
        try:
            saved = history.get(row["key"])
            transfer = transfers.get(row["target"])
            if not saved or saved["state"] != "linked" or saved["target"] != row["target"]:
                raise ValueError("缺少对应的成功整理记录")
            if not transfer or json.loads(transfer["source_fingerprint"]) != json.loads(
                saved["fingerprint"]
            ):
                raise ValueError("原下载文件的身份记录不完整")
            if target in owners:
                raise ValueError("新路径已有插件归属记录")
            item["source"] = saved["source"]
            source_hash, _ = checked_hash(
                options.source_root, saved["source"], json.loads(saved["fingerprint"])
            )
            target_hash, target_fp = checked_hash(new_root, target)
            if target_fp[2] != json.loads(row["fingerprint"])[2] or source_hash != target_hash:
                raise ValueError("新位置的文件与原下载内容不一致")
            item.update(
                state="verified",
                detail="SHA-256 一致；仅更新记录，不移动或复制文件",
                fingerprint=target_fp,
            )
        except (OSError, ValueError) as error:
            item.update(state="held", detail=str(error))
        items.append(item)
    report(len(selected), len(selected), "")
    return {
        "kind": "music-relocation",
        "old_root": old_root,
        "new_root": new_root,
        "settings": options.model_dump(),
        "state_digest": digest(state),
        "items": items,
        "ready": all(item["state"] == "verified" for item in items),
    }


def preview(store, options, old_root, report):
    plan = inspect(store, options, old_root, report)
    token = secrets.token_urlsafe(24)
    store.plan(token, plan)
    return {**plan, "token": token}


def rebase(value, old, new, fingerprints):
    if isinstance(value, str):
        if value == old or value.startswith(old + "/"):
            return new + value[len(old) :]
        return value
    if isinstance(value, list):
        return [rebase(item, old, new, fingerprints) for item in value]
    if isinstance(value, dict):
        result = {
            rebase(key, old, new, fingerprints): rebase(item, old, new, fingerprints)
            for key, item in value.items()
        }
        for path_key, fp_key in (
            ("path", "fingerprint"),
            ("source", "fingerprint"),
            ("replay_source", "replay_fingerprint"),
        ):
            if value.get(path_key) in fingerprints and fp_key in value:
                result[fp_key] = fingerprints[value[path_key]]
        return result
    return value


def apply(store, options, token, report):
    plan = store.take_plan(token)
    if plan.get("kind") != "music-relocation" or plan.get("settings") != options.model_dump():
        raise ValueError("目录配置已变化，请重新预览")
    current = inspect(store, options, plan["old_root"], report)
    if not current["ready"] or current != {
        key: value for key, value in plan.items() if key != "token"
    }:
        raise ValueError("记录或文件已变化，请重新预览；尚未更新任何记录")
    backup = str(store.backup())
    old, new = plan["old_root"], plan["new_root"]
    fingerprints = {item["old_target"]: item["fingerprint"] for item in current["items"]}
    with store.db() as connection:
        connection.execute("BEGIN IMMEDIATE")
        state = snapshot(connection)
        if digest(state) != plan["state_digest"]:
            raise ValueError("记录已变化，请重新预览")
        for row in state["owned"]:
            if row["target"] in fingerprints:
                connection.execute(
                    "UPDATE owned SET target=?,fingerprint=? WHERE target=?",
                    (
                        rebase(row["target"], old, new, {}),
                        json.dumps(fingerprints[row["target"]]),
                        row["target"],
                    ),
                )
        for row in state["transfers"]:
            connection.execute(
                "UPDATE transfers SET target=? WHERE target=?",
                (rebase(row["target"], old, new, {}), row["target"]),
            )
        for row in state["files"]:
            # The source/download fingerprint stays unchanged.
            connection.execute(
                "UPDATE files SET target=? WHERE key=?",
                (rebase(row["target"], old, new, {}), row["key"]),
            )
        for table, column in (("tasks", "payload"), ("bindings", "payload"), ("kv", "value")):
            for row in state[table]:
                value = rebase(json.loads(row[column]), old, new, fingerprints)
                connection.execute(
                    f"UPDATE {table} SET {column}=? WHERE key=?", (json.dumps(value), row["key"])
                )
        connection.execute("DELETE FROM plans")
        connection.execute("DELETE FROM kv WHERE key IN ('automatic_signature','retry_at')")
        connection.execute(
            "INSERT OR REPLACE INTO kv VALUES ('music_relocation',?)",
            (
                json.dumps(
                    {
                        "old_root": old,
                        "new_root": new,
                        "files": len(current["items"]),
                        "backup": backup,
                        "at": time.time(),
                    }
                ),
            ),
        )
    collections = update_collections(store, options, [], [])
    store.set("collection_results", collections)
    return {
        "files": len(current["items"]),
        "backup": backup,
        "collections": collections,
        "automatic": False,
    }
