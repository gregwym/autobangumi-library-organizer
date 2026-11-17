"""Resolve AB entry ownership from stored torrent identity, never title similarity."""

import base64
import re
from urllib.parse import parse_qs, urlsplit


def normalize_hash(value):
    value = (value or "").strip().lower()
    return value if re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", value) else None


def record_hash(record):
    if record.qb_hash:
        return normalize_hash(record.qb_hash), "ab_hash"
    try:
        url = urlsplit(record.url)
        if url.scheme == "magnet":
            hashes = set()
            for xt in parse_qs(url.query).get("xt", []):
                if not xt.lower().startswith("urn:btih:"):
                    continue
                value = xt[9:]
                if re.fullmatch(r"[A-Za-z2-7]{32}", value):
                    value = base64.b32decode(value.upper()).hex()
                value = normalize_hash(value)
                if value:
                    hashes.add(value)
            return (next(iter(hashes)) if len(hashes) == 1 else None), "ab_magnet"
        # This is the observed Mikan download URL contract, not a search for an
        # arbitrary hash-looking token in a URL, access token, or title.
        if url.scheme in {"http", "https"} and url.hostname == "mikanani.me":
            match = re.fullmatch(r"/Download/\d{8}/([0-9a-fA-F]{40})\.torrent", url.path)
            if match:
                return match[1].lower(), "ab_mikan_url"
    except ValueError, TypeError:
        pass
    return None, "unresolved"


class Associations:
    def __init__(self, records, rules):
        self.rules = {r.id: r for r in rules}
        self.index = {}
        for record in records:
            identity, source = record_hash(record)
            if identity:
                self.index.setdefault((record.downloader_id or "default", identity), []).append(
                    (record.bangumi_id, record.id, source)
                )

    def resolve(self, instance, task_hash, assigned=None):
        rows = self.index.get((instance, normalize_hash(task_hash)), [])
        owners = {r[0] for r in rows if r[0] is not None}
        evidence = {"record_ids": sorted(r[1] for r in rows), "owners": sorted(owners)}
        if owners:
            if len(owners) != 1:
                return None, {
                    **evidence,
                    "source": "conflict",
                    "reason": "AB 多个 entry 关联同一种子，请先修正归属",
                    "assignable": False,
                }
            rid = next(iter(owners))
            rule = self.rules.get(rid)
            if rule is None or rule.downloader_id and rule.downloader_id != instance:
                return None, {
                    **evidence,
                    "source": "conflict",
                    "reason": "AB 原 entry 已删除或下载器关联不一致",
                    "assignable": False,
                }
            return rid, {
                **evidence,
                "source": rows[0][2],
                "reason": "沿用 AB 种子记录归属",
                "assignable": False,
            }
        if assigned in self.rules:
            rule = self.rules[assigned]
            if not rule.downloader_id or rule.downloader_id == instance:
                return assigned, {
                    **evidence,
                    "source": "manual",
                    "reason": "用户明确关联",
                    "assignable": True,
                }
        return None, {
            **evidence,
            "source": "unresolved",
            "reason": "没有可核对的 AB 种子关联，请确认所属 entry",
            "assignable": True,
        }
