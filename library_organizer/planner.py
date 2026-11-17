"""Pure, conservative planning; TMDB supplies identities, never a guessed season."""

import hashlib
import re
from pathlib import Path, PurePosixPath

VIDEO = {".mkv", ".mp4", ".avi", ".m4v", ".ts", ".webm", ".mov"}
SUBS = {".ass", ".ssa", ".srt", ".vtt", ".sup", ".sub", ".idx"}
EXTRAS = [
    (r"(?i)\b(?:NCOP|NCED|OP|ED)(?:\d+)?\b", "extras"),
    (r"(?i)\b(?:PV|CM|trailer|teaser|preview)\s*\d*\b", "trailers"),
    (
        r"(?i)\b(?:making|behind.the.scenes|interview|menu|unused|april.fool)\w*\b",
        "behind the scenes",
    ),
]


def clean(s):
    return (
        re.sub(r"\s+", " ", re.sub(r'[<>:"/\\|?*\x00-\x1f]', " ", str(s))).strip().rstrip(".")[:180]
        or "Unknown"
    )


def fingerprint(path):
    s = path.stat()
    return [s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns]


def contained(root, path):
    root = Path(root).resolve()
    path = Path(path)
    if not path.is_absolute():
        path = root / path
    # Resolve parent and final symlinks before any use; reject all source/target symlinks.
    if path.is_symlink() or any(
        p.is_symlink() for p in path.parents if p != root and root in p.parents
    ):
        raise ValueError("不处理符号链接路径")
    resolved = path.resolve()
    if resolved == root or root not in resolved.parents:
        raise ValueError("路径超出配置目录")
    return resolved


def episode(name):
    # No unadorned trailing number: "Hibike! Euphonium 3" is not episode 3.
    stem = Path(name).stem
    if re.search(r"(?i)(?:E\d{1,4}(?:[ -]*E|[-~])\d{1,4}|[\[ ]\d{1,4}\s*[-~]\s*\d{1,4})", stem):
        return None
    for pattern in [
        r"(?i)S\d{1,3}E(\d{1,4})(?![\d.])",
        r"(?i)\bEP?(\d{1,4})(?![\d.])",
        r"\[(\d{1,4})(?:v\d+)?(?:\s*END)?\]",
        r" - (\d{1,4})(?:v\d+)?(?=\s|\[|$)",
        r"第?(\d{1,4})[话話集]",
    ]:
        m = re.search(pattern, stem)
        if m:
            return int(m[1])
    return None


def classify(name):
    suffix = Path(name).suffix.lower()
    if suffix in SUBS:
        return "subtitle"
    if suffix not in VIDEO:
        return "resource"
    for pattern, kind in EXTRAS:
        if re.search(pattern, Path(name).stem):
            return kind
    if any(
        p.lower() in {"sps", "specials", "extras", "bonus"} for p in PurePosixPath(name).parts[:-1]
    ):
        return "extras"
    return "episode"


def version(name, task_name):
    text = name + " " + task_name
    parts = []
    m = re.match(r"^[\[【]([^\]】]+)[\]】]", Path(name).name) or re.match(
        r"^[\[【]([^\]】]+)[\]】]", task_name
    )
    if m:
        parts.append(clean(m[1]))
    for pat in [
        r"(?i)\b(?:2160p|1080p|720p|480p)\b",
        r"(?i)\b(?:HEVC|AVC|x265|x264|AV1)\b",
        r"(?i)\b(?:CHS(?:&JPN)?|CHT(?:&JPN)?)\b",
        r"简繁|简日|繁日|简体|繁体",
        r"(?i)(?:\d|\b)v([1-9]\d*)\b",
    ]:
        m = re.search(pat, text)
        if m:
            if not m.lastindex or int(m[1]) > 1:
                parts.append("v" + m[1] if m.lastindex else m[0])
    return clean(" ".join(parts)) if parts else ""


def task_key(instance, task_id):
    return hashlib.sha256((instance + "\0" + task_id).encode()).hexdigest()[:32]


def build(task, mapping, metadata, options):
    title = clean(metadata["title"])
    year = metadata.get("year", "")
    base = clean(f"{title} ({year})" if year else title) + f" [tmdbid-{mapping.tmdb_id}]"
    root = Path(options.library_root)
    folder = root / ("Shows" if mapping.media_type == "tv" else "Movies") / base
    entries = []
    videos = {}
    media = [f for f in task["files"] if classify(f["name"]) == "episode"]
    main = mapping.main_file
    if mapping.media_type == "movie" and not main and len(media) == 1:
        main = media[0]["name"]
    for f in task["files"]:
        name = f["name"]
        kind = classify(name)
        item = {
            "key": task["key"] + ":" + str(f["index"]),
            "source": f["source"],
            "fingerprint": f.get("fingerprint"),
            "name": name,
            "kind": kind,
            "target": None,
            "reason": "",
            "task": task["key"],
            "identity": None,
        }
        if f.get("progress", 0) < 1 or not f.get("fingerprint"):
            item["reason"] = "文件未完成或本地文件不可用"
            entries.append(item)
            continue
        suffix = Path(name).suffix.lower()
        v = version(name, task["name"]) if options.version_suffix else ""
        # Stable task discriminator prevents equal edition labels from overwriting one another.
        label = (" - " + v if v else "") + (
            " [" + task["key"][:8] + "]" if options.version_suffix else ""
        )
        target = None
        if suffix in VIDEO and (name in mapping.episodes or kind == "episode" or name == main):
            if mapping.media_type == "movie":
                if name == main:
                    target = folder / (base + label + suffix)
                    item["identity"] = {"media_type": "movie", "tmdb_id": mapping.tmdb_id}
                elif main:
                    target = folder / "extras" / clean(Path(name).name)
                else:
                    item["reason"] = "电影含多个候选正片，请指定 main_file"
            else:
                ep = mapping.episodes.get(name)
                if ep is None:
                    ep = episode(name)
                    if ep is not None:
                        ep += mapping.episode_offset
                if ep is None or ep not in metadata["episodes"]:
                    item["reason"] = "无法对应 TMDB 集数，请在文件映射中确认"
                else:
                    target = (
                        folder
                        / f"Season {mapping.season:02d}"
                        / (f"{title} S{mapping.season:02d}E{ep:02d}" + label + suffix)
                    )
                    item["identity"] = {
                        "media_type": "tv",
                        "tmdb_id": mapping.tmdb_id,
                        "season": mapping.season,
                        "episode": ep,
                    }
        elif kind == "subtitle":
            pass  # resolved after its corresponding video
        else:
            # Preserve every ancillary file under a source-specific directory; release NFOs
            # never become episode/movie metadata that could override TMDB identities.
            category = kind if kind != "resource" else "_resources"
            rel = PurePosixPath(name)
            target = (
                folder / category / task["key"][:8] / Path(*[clean(p) for p in rel.parts])
                if kind == "resource"
                else folder
                / category
                / (clean(Path(name).stem) + " [" + task["key"][:8] + "]" + suffix)
            )
        if target:
            item["target"] = str(target)
        entries.append(item)
        if suffix in VIDEO and target:
            videos[name] = str(target)
    for item in entries:
        if item["kind"] != "subtitle" or item["reason"]:
            continue
        name = item["name"]
        stem = Path(name).stem
        candidates = [
            n for n in videos if stem == Path(n).stem or stem.startswith(Path(n).stem + ".")
        ]
        if not candidates:
            ep = episode(name)
            candidates = [n for n in videos if ep is not None and episode(n) == ep]
        if len(candidates) != 1:
            item["reason"] = "字幕无法唯一对应视频"
            continue
        original = candidates[0]
        dst = Path(videos[original])
        tail = stem[len(Path(original).stem) :] if stem.startswith(Path(original).stem) else ""
        langs = []
        for regex, lang in [
            (r"(?i)chs|sc\b|zh-cn|简", "zh-Hans"),
            (r"(?i)cht|tc\b|zh-tw|繁", "zh-Hant"),
            (r"(?i)jpn|ja\b", "ja"),
            (r"(?i)eng|en\b", "en"),
        ]:
            if re.search(regex, tail or name):
                langs.append(lang)
        lang = langs[0] if len(langs) == 1 else "und"
        flag = ".forced" if "forced" in name.lower() else ""
        item["target"] = str(dst.with_suffix("")) + "." + lang + flag + Path(name).suffix.lower()
    targets = {}
    for item in entries:
        if item["target"]:
            targets.setdefault(item["target"], []).append(item)
    for items in targets.values():
        if len(items) > 1:
            for item in items:
                item["reason"] = "多个源文件指向同一目标，请确认映射"
                item["target"] = None
    return entries
