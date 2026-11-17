"""File-only music collections. No media-server credentials, IDs or network calls."""

import hashlib
import json
import os
import re
import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path

from .planner import clean, contained, fingerprint


def xml_bytes(title, albums):
    root = ET.Element("Item")
    ET.SubElement(root, "LocalTitle").text = title
    # LockData=true also blocks later local XML imports, including new members.
    ET.SubElement(root, "LockData").text = "false"
    children = ET.SubElement(root, "CollectionItems")
    for album in sorted(set(albums)):
        child = ET.SubElement(children, "CollectionItem")
        ET.SubElement(child, "Path").text = album
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


def update_collections(store, options, items, results):
    members = store.get("music_members", {})
    touched = {}
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
        if collection and item.get("album_target") and not item.get("archive_root"):
            members[item["key"]] = {
                "collection": collection,
                "album": item["album_target"],
                "target": item["target"],
                "rule_id": item.get("rule_id"),
            }
    store.set("music_members", members)
    if not options.collections_root or not touched:
        return []
    if not Path(options.collections_root).is_dir():
        return [{"state": "failed", "detail": "合集目录不存在或尚未挂载，未生成清单"}]
    owned = {row["target"]: row for row in store.owned()}
    groups = {key: {"title": title, "albums": set()} for key, title in touched.items()}
    for key, member in members.items():
        collection = member["collection"]
        if collection["key"] not in touched:
            continue
        group = groups.setdefault(
            collection["key"], {"title": collection["title"], "albums": set()}
        )
        try:
            target = contained(options.music_root, member["target"])
            album = contained(options.music_root, member["album"])
            row = owned.get(str(target))
            if row and row["key"] == key and fingerprint(target) == json.loads(row["fingerprint"]):
                group["albums"].add(str(album))
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
            output.append({"state": "held", "detail": "音乐合集身份不明确，未生成清单"})
            continue
        target = Path(options.collections_root) / directory / "collection.xml"
        try:
            changed = publish_manifest(
                store, options.collections_root, target, xml_bytes(group["title"], group["albums"])
            )
            output.append(
                {
                    "state": "written" if changed else "exists",
                    "target": str(target),
                    "albums": len(group["albums"]),
                    "detail": f"音乐合集清单{'已更新' if changed else '未变化'}：{group['title']}，{len(group['albums'])} 个专辑；等待媒体服务器扫描",
                }
            )
        except ValueError as error:
            output.append({"state": "held", "target": str(target), "detail": str(error)})
        except OSError as error:
            output.append({"state": "failed", "target": str(target), "detail": str(error)})
    return output
