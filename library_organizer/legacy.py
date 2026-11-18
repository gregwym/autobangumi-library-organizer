"""Read-only discovery of native AB outputs; adoption requires a reviewed plan."""

import hashlib
from pathlib import Path

from .planner import contained, destination, fingerprint


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


def discover(item, options, layout, store):
    """Only exact native path rules/record identities, never title/episode guessing."""
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
    found = []
    for raw, record in candidates.items():
        try:
            path = destination(options, raw)
            if not path.exists():
                continue
            fp = fingerprint(path)
            if not path.is_file() or fp[2] != expected[2]:
                continue
            # Another task's ownership must never be adopted or cleaned up.
            if any(r["key"] != item["key"] for r in store.owned() if r["target"] == str(path)):
                continue
            if fp == expected:
                proof, method = "同源硬链", "hardlink"
            elif record == [expected, fp]:
                proof, method = "AB 原生整理记录与文件身份一致", "existing"
            elif content_hash(path, fp, store) == content_hash(source, expected, store):
                proof, method = "完整 SHA-256 内容一致", "existing"
            else:
                continue
            found.append({"path": str(path), "fingerprint": fp, "proof": proof, "method": method})
        except OSError as error:
            raise ValueError("已有文件核验失败，未安排重新复制：" + str(error)) from error
    # Dedupe candidates mapped to the same absolute path, but keep distinct aliases
    # visible: choosing which old alias to remove requires an explicit decision.
    return list({r["path"]: r for r in found}.values())


def removed_native(item, options, layout):
    for raw, record in layout.get("records", {}).items():
        if isinstance(record, list) and len(record) == 2 and record[0] == item["fingerprint"]:
            try:
                if not destination(options, raw).exists():
                    return True
            except ValueError:
                continue
    return False
