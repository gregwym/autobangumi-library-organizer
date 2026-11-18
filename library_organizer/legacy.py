"""Read-only discovery of native AB outputs; adoption requires a reviewed plan."""

import hashlib
import os
from pathlib import Path

from .planner import contained, destination, exists_checked, fingerprint


def content_hash(path, expected, store):
    key = "content:" + hashlib.sha256(str(expected).encode()).hexdigest()
    cached = store.get(key)
    if fingerprint(path) != expected:
        raise ValueError("文件已变化，请重新预览")
    if cached:
        return cached
    with path.open("rb") as handle:
        value = hashlib.file_digest(handle, "sha256").hexdigest()
    if fingerprint(path) != expected:
        raise ValueError("文件在核验期间变化，请重新预览")
    store.set(key, value)
    return value


def work_candidates(item, options):
    """Narrow fallback for renamed videos/sidecars in this work, never a library-wide search."""
    if not item.get("identity") and not item.get("video_key"):
        return
    raw = item.get("work_root") or item.get("collection_target")
    if not raw:
        return
    root = destination(options, raw)
    if not exists_checked(root):
        return
    if not root.is_dir():
        raise ValueError("作品目录不是有效目录，无法核验已有文件")

    def fail(error):
        raise error

    suffix = Path(item["source"]).suffix.lower()
    for parent, directories, files in os.walk(root, followlinks=False, onerror=fail):
        directories[:] = sorted(
            name for name in directories if not (Path(parent) / name).is_symlink()
        )
        for name in sorted(files):
            path = Path(parent) / name
            if path.suffix.lower() != suffix or path.is_symlink():
                continue
            path = destination(options, path)
            if path.is_file() and path.stat().st_size == item["fingerprint"][2]:
                yield str(path)


def discover(item, options, layout, store):
    """Prove identity before adoption; filenames only narrow the search scope."""
    source = contained(options.source_root, item["source"])
    expected = item["fingerprint"]
    if fingerprint(source) != expected:
        raise ValueError("源文件已变化，请重新预览")
    candidates = {item["target"]: None}
    try:
        relative = source.relative_to(Path(layout.get("source_root", options.source_root)))
        mirror = Path(layout.get("library_root", options.library_root)) / relative
        candidates[str(mirror)] = None
    except ValueError:
        pass
    for path, record in layout.get("records", {}).items():
        if isinstance(record, list) and len(record) == 2 and record[0] == expected:
            candidates[path] = record
    owners = {row["target"]: row["key"] for row in store.owned()}

    def verify(raw, record):
        try:
            path = destination(options, raw)
            if not exists_checked(path):
                return None
            fp = fingerprint(path)
            if not path.is_file() or fp[2] != expected[2]:
                return None
            # Another task's ownership must never be adopted or cleaned up.
            if owners.get(str(path), item["key"]) != item["key"]:
                return None
            if fp == expected:
                proof, method = "同源硬链", "hardlink"
            elif record == [expected, fp]:
                proof, method = "AB 原生整理记录与文件身份一致", "existing"
            elif content_hash(path, fp, store) == content_hash(source, expected, store):
                proof, method = "完整 SHA-256 内容一致", "existing"
            else:
                return None
            return {"path": str(path), "fingerprint": fp, "proof": proof, "method": method}
        except OSError as error:
            raise ValueError("已有文件核验失败，未安排重新复制：" + str(error)) from error

    found = [result for raw, record in candidates.items() if (result := verify(raw, record))]
    if not found:
        # Native source/target names can diverge after a manual season correction.
        # Never infer equivalence from season, episode, size or mtime alone.
        for raw in work_candidates(item, options):
            if raw not in candidates and (result := verify(raw, None)):
                found.append(result)
    # Dedupe candidates mapped to the same absolute path, but keep distinct aliases
    # visible: choosing which old alias to remove requires an explicit decision.
    return list({r["path"]: r for r in found}.values())


def removed_native(item, options, layout):
    for raw, record in layout.get("records", {}).items():
        if isinstance(record, list) and len(record) == 2 and record[0] == item["fingerprint"]:
            try:
                if not exists_checked(destination(options, raw)):
                    return True
            except ValueError:
                continue
    return False
