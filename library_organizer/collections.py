"""Work collections from verified organizer outputs, without media-server access."""

import hashlib
import json
import os
import re
import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path, PurePosixPath

from .planner import clean, contained, fingerprint


def xml_bytes(title, members):
    root = ET.Element("Item")
    ET.SubElement(root, "LocalTitle").text = title
    # LockData=true also blocks later local XML imports, including new members.
    ET.SubElement(root, "LockData").text = "false"
    children = ET.SubElement(root, "CollectionItems")
    for path in sorted(set(members)):
        child = ET.SubElement(children, "CollectionItem")
        ET.SubElement(child, "Path").text = path
    ET.indent(root, space="  ")
    # The legacy reader only clears previous members for a non-self-closing node.
    return ET.tostring(root, encoding="utf-8", xml_declaration=True, short_empty_elements=False)


def publish_manifest(store, root, target, content):
    target = contained(root, target)
    digest = hashlib.sha256(content).hexdigest()
    record_key = "manifest:" + str(target)
    prior = store.get(record_key, {})
    existing = None
    if target.exists():
        if not target.is_file() or target.stat().st_size > 8 * 1024 * 1024:
            raise ValueError("合集文件类型或大小异常，未覆盖")
        existing = hashlib.sha256(target.read_bytes()).hexdigest()
        if existing not in {prior.get("digest"), prior.get("previous")}:
            raise ValueError("合集清单不是插件生成的版本或已被修改，未覆盖")
        if existing == digest:
            return False
    target.parent.mkdir(parents=True, exist_ok=True)
    target = contained(root, target)
    # Persist both expected versions before replacement, allowing crash recovery.
    store.set(record_key, {"digest": digest, "previous": existing})
    with tempfile.TemporaryDirectory(prefix=".ab-collection-", dir=target.parent) as temp:
        stage = Path(temp) / "collection.xml"
        with stage.open("xb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        if existing is None:
            os.link(stage, target, follow_symlinks=False)
        else:
            if hashlib.sha256(target.read_bytes()).hexdigest() != existing:
                raise ValueError("合集清单在整理期间被修改，未覆盖")
            os.replace(stage, target)
    return True


def reference_path(options, path):
    """Translate complete directory prefixes; the reader's filesystem is remote."""
    path = PurePosixPath(path)
    for mapping in sorted(options.collection_path_map, key=lambda m: len(m.local), reverse=True):
        local = PurePosixPath(mapping.local)
        if path.is_relative_to(local):
            return str(PurePosixPath(mapping.remote) / path.relative_to(local))
    return str(path)


def membership(item):
    collection = item.get("collection")
    member = item.get("album_target") or item.get("collection_target")
    kind = "music" if item.get("album_target") else (item.get("identity") or {}).get("media_type")
    if item.get("archive_root") or not item.get("target") or kind not in {"music", "tv", "movie"}:
        return None
    if kind != "music" and not member:
        # Pre-0.9 successful video records lack collection fields. Derive only
        # the existing output's container; never re-plan or move its files.
        target = Path(item["target"])
        if kind == "tv":
            if not re.fullmatch(r"Season \d{2,3}", target.parent.name):
                return None
            folder = target.parent.parent
        else:
            folder = target.parent
        member = str(folder)
        identity = item["identity"]
        base = re.sub(r" \[tmdbid-\d+\]$", "", folder.name)
        collection = {
            "key": f"{kind}:{identity['tmdb_id']}"
            if identity.get("tmdb_id")
            else f"ab:{kind}:{base}",
            "title": re.sub(r" \(\d{4}\)$", "", base),
        }
    if not collection or not member:
        return None
    return {
        "collection": collection,
        "member": member,
        "kind": kind,
        "target": item["target"],
        "rule_id": item.get("rule_id"),
    }


def update_collections(store, options, items, results):
    members = store.get("collection_members")
    if members is None:
        members = {
            key: {**value, "member": value["album"], "kind": "music"}
            for key, value in store.get("music_members", {}).items()
        }
    touched = {}
    # Durable success is mandatory for history backfill, in addition to the
    # ownership/fingerprint check below. A crash-time claim alone is insufficient.
    completed = {r["key"]: r for r in store.files() if r["state"] == "linked"}
    for item in store.get("last_items", []):
        row = completed.get(item["key"])
        if row and row["target"] == item.get("target") and (member := membership(item)):
            members[item["key"]] = member
    for item, result in zip(items, results, strict=True):
        collection = item.get("collection")
        if collection:
            touched[collection["key"]] = collection["title"]
        if result["state"] not in {"linked", "exists"}:
            continue
        previous = members.pop(item["key"], None)
        if previous:
            old = previous["collection"]
            touched[old["key"]] = old["title"]
        if member := membership(item):
            members[item["key"]] = member
    store.set("collection_members", members)
    for member in members.values():
        collection = member["collection"]
        touched[collection["key"]] = collection["title"]
    if not options.collections_root or not touched:
        return []
    if not Path(options.collections_root).is_dir():
        return [{"state": "failed", "detail": "合集目录不存在或尚未挂载，未生成清单"}]
    owned = {row["target"]: row for row in store.owned()}
    groups = {
        key: {"title": title, "music": set(), "tv": set(), "movie": set(), "has_music": False}
        for key, title in touched.items()
    }
    for key, member in members.items():
        collection = member["collection"]
        if collection["key"] not in touched:
            continue
        group = groups[collection["key"]]
        kind = member["kind"]
        group["has_music"] |= kind == "music"
        try:
            root = options.music_root if kind == "music" else options.library_root
            if not root:
                continue
            target = contained(root, member["target"])
            directory = contained(root, member["member"])
            if (
                target == directory
                or directory not in target.parents
                or directory == Path(root).resolve()
            ):
                continue
            row = owned.get(str(target))
            if row and row["key"] == key and fingerprint(target) == json.loads(row["fingerprint"]):
                group[kind].add(reference_path(options, directory))
        except ValueError, OSError:
            continue
    output = []
    for identity, group in sorted(groups.items()):
        safe_id = identity.replace(":", "-")
        native = re.fullmatch(r"ab:(tv|movie):(.+)", identity)
        if native:
            directory = f"{clean(native[2])} [ab-{native[1]}] [boxset]"
        elif re.fullmatch(r"(?:tv|movie)-\d+", safe_id):
            directory = f"{clean(group['title'])} [ab-{safe_id}] [boxset]"
        else:
            output.append({"state": "held", "detail": "作品合集身份不明确，未生成清单"})
            continue
        target = Path(options.collections_root) / directory / "collection.xml"
        paths = group["music"] | group["tv"] | group["movie"]
        # A normal series alone does not need a duplicate collection. Remember
        # its membership so a later album can join automatically.
        if not group["has_music"] and len(paths) < 2 and not store.get("manifest:" + str(target)):
            continue
        try:
            changed = publish_manifest(
                store, options.collections_root, target, xml_bytes(group["title"], paths)
            )
            output.append(
                {
                    "state": "written" if changed else "exists",
                    "target": str(target),
                    "albums": len(group["music"]),
                    "series": len(group["tv"]),
                    "movies": len(group["movie"]),
                    "detail": f"作品合集清单{'已更新' if changed else '未变化'}：{group['title']}，{len(group['tv'])} 部剧集、{len(group['movie'])} 部电影、{len(group['music'])} 个专辑；等待媒体服务器扫描",
                }
            )
        except ValueError as error:
            output.append({"state": "held", "target": str(target), "detail": str(error)})
        except OSError as error:
            output.append({"state": "failed", "target": str(target), "detail": str(error)})
    return output
