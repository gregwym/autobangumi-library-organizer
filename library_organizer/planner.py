"""Pure planning using AB metadata by default, with optional TMDB corrections."""

import hashlib
import re
import unicodedata
from pathlib import Path, PurePosixPath

from .resources import AUDIO, MUSIC, cue_images, music_directory, music_notice, music_tags

VIDEO = {".mkv", ".mp4", ".avi", ".m4v", ".ts", ".webm", ".mov"}
SUBS = {".ass", ".ssa", ".srt", ".vtt", ".sup", ".sub", ".idx"}
MAIN_LABELS = {"movie", "feature film", "main feature", "正片", "本編", "本篇"}
BONUS_LABELS = {"mv", "music video", "bonus", "extra", "featurette", "特典", "映像特典", "花絮"}
EXTRAS = [
    (r"(?i)\b(?:NCOP|NCED|OP|ED)(?:\d+)?\b", "extras"),
    (r"(?i)\b(?:PV|CM|trailer|teaser|preview)\s*\d*\b", "trailers"),
    (r"(?i)\b(?:interview)\w*\b", "interviews"),
    (r"(?i)\b(?:making|behind.the.scenes)\w*\b", "behind the scenes"),
    (r"(?i)\b(?:unused|deleted)\w*\b", "deleted scenes"),
    (r"(?i)\b(?:menu|april.fool|event|OVA|OAD|SP|special|extra.episode)\w*\b", "extras"),
]


def clean(s):
    return (
        re.sub(r"\s+", " ", re.sub(r'[<>:"/\\|?*\x00-\x1f]', " ", str(s))).strip().rstrip(".")[:180]
        or "Unknown"
    )


def fingerprint(path):
    s = path.stat()
    return [s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns]


def exists_checked(path):
    """Only ENOENT means absent; Python 3.14 Path.exists hides all OSErrors."""
    try:
        Path(path).stat()
    except FileNotFoundError:
        return False
    return True


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


def episode(name, *titles):
    # No unadorned trailing number: "Hibike! Euphonium 3" is not episode 3.
    stem = Path(name).stem
    stem = re.sub(
        r"(?i)(?:\.(?:chs|cht|zh-hans|zh-hant|zh-cn|zh-tw|jpn|eng|ja|en|forced|default))+$",
        "",
        stem,
    )
    # In "Show 2 - 01", only discard the 2 when the release, containing
    # directory or catalog independently identifies "Show 2" as a title.
    numbered = re.match(
        r"^(?:[\[【][^\]】]+[\]】]\s*)*([^\[【]*?[^\s\[【~–—-]\s+\d{1,4})(?= - \d)", stem
    )
    if numbered:
        title = re.escape(numbered[1].strip())
        if any(
            re.search(rf"(?<!\w){title}(?=\s*(?:$|[\[【(（]))", hint, re.I)
            for hint in (*titles, Path(name).parent.name)
        ):
            stem = stem[: numbered.start(1)] + stem[numbered.end() :]
    if re.search(
        r"(?i)(?:E\d{1,4}(?:\s*[-~–—至]?\s*E|\s*[-~–—至]\s*)\d{1,4}"
        r"|(?<![a-z0-9])\d{1,4}\s*[-~–—至]\s*\d{1,4})",
        stem,
    ):
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
    if suffix in AUDIO:
        return "audio"
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


def movie_file_role(name):
    """Read explicit role labels from the basename of an ambiguous movie candidate."""
    stem = unicodedata.normalize("NFKC", Path(name).stem).casefold()
    stem = stem.replace("【", "[").replace("】", "]")
    # Leading tags normally identify the release group. Only use the final
    # descriptor and trailing tags, so words in a film title aren't role labels.
    stem = re.sub(r"^(?:\s*\[[^\[\]]+\]\s*)+", "", stem)
    tags = re.search(r"(?:\s*\[[^\[\]]+\])+\s*$", stem)
    fields = []
    if tags:
        fields = re.findall(r"\[([^\[\]]+)\]", tags[0])
        stem = stem[: tags.start()]
    description = re.split(r"\s+[-–—]\s+", stem)[-1]
    fields.append(description)
    roles = set()
    for field in fields:
        label = re.sub(r"\s+v\d+$", "", field.strip()).strip()
        if label in MAIN_LABELS:
            roles.add("main")
        elif label in BONUS_LABELS or label.endswith((" mv", " music video")):
            roles.add("extra")
    return roles.pop() if len(roles) == 1 else None


def conflicting_claims(items):
    """A new inference cannot block a unique target from the ordinary plan."""
    if len(items) < 2:
        return []
    established = [item for item in items if not item.get("inferred_movie_role")]
    if len(established) == 1:
        return [item for item in items if item is not established[0]]
    return items


def destination(options, path):
    """Only configured roots are eligible, including when revisiting an old owned path."""
    for root in (options.library_root, options.music_root):
        if root and Path(root).resolve() in Path(path).absolute().parents:
            return contained(root, path)
    raise ValueError("目标不在当前配置的媒体库目录内")


def sidecar_candidates(name, videos, source_episodes, *, titles=(), audio=False):
    """Exact relative stem first; episode fallback only for unique main videos."""
    path, stem = PurePosixPath(name), PurePosixPath(name).stem
    exact = [
        n
        for n in videos
        if path.parent == PurePosixPath(n).parent
        and (stem == PurePosixPath(n).stem or stem.startswith(PurePosixPath(n).stem + "."))
    ]
    if exact:
        # A longer video stem is more specific (e.g. episode vs episode.commentary).
        length = max(len(PurePosixPath(n).stem) for n in exact)
        return [n for n in exact if len(PurePosixPath(n).stem) == length]
    if audio:
        return []  # Track numbers never associate album tracks with TV episodes.
    exact = [n for n in videos if stem == Path(n).stem or stem.startswith(Path(n).stem + ".")]
    if exact:
        return exact
    ep = episode(name, *titles)
    group = version(name, "")
    return [
        n
        for n in videos
        if classify(n) == "episode"
        and ep is not None
        and ep in {episode(n, *titles), source_episodes.get(n)}
        and (not group or not version(n, "") or group == version(n, ""))
    ]


def sidecar_tail(name, original):
    """Keep track labels so two subtitle/audio tracks do not silently collapse."""
    stem, video = Path(name).stem, Path(original).stem
    tail = stem[len(video) :] if stem.startswith(video) else stem
    lang = "und"
    for regex, value in [
        (r"(?i)chs|zh-hans|zh-cn|简|(?:^|\.)sc(?:$|\.)", "zh-Hans"),
        (r"(?i)cht|zh-hant|zh-tw|繁|(?:^|\.)tc(?:$|\.)", "zh-Hant"),
        (r"(?i)(?:^|[. _-])(?:jpn|ja)(?:$|[. _-])", "ja"),
        (r"(?i)(?:^|[. _-])(?:eng|en)(?:$|[. _-])", "en"),
    ]:
        if re.search(regex, tail):
            lang = value
            break
    qualifiers = []
    for token in tail.strip(".").split(".") if stem.startswith(video) else []:
        if token and not re.fullmatch(
            r"(?i)chs|cht|sc|tc|zh-hans|zh-hant|zh-cn|zh-tw|jpn|ja|eng|en|und", token
        ):
            qualifiers.append(clean(token))
    if not qualifiers and "forced" in name.lower():
        qualifiers.append("forced")
    return "." + ".".join([lang, *qualifiers]) + Path(name).suffix.lower()


def version(name, task_name):
    m = re.match(r"^[\[【]([^\]】]+)[\]】]", Path(name).name) or re.match(
        r"^[\[【]([^\]】]+)[\]】]", task_name
    )
    return clean(m[1]) if m else ""


def task_key(instance, task_id):
    return hashlib.sha256((instance + "\0" + task_id).encode()).hexdigest()[:32]


def build(task, mapping, metadata, options):
    title = clean(metadata["title"])
    year = metadata.get("year", "")
    base = clean(f"{title} ({year})" if year else title)
    if mapping.tmdb_id:
        base += f" [tmdbid-{mapping.tmdb_id}]"
    collection = {
        "key": f"{mapping.media_type}:{mapping.tmdb_id}"
        if mapping.tmdb_id
        else f"ab:{mapping.media_type}:{base}",
        "title": title,
    }
    root = Path(options.library_root)
    folder = (
        root
        / (options.tv_subdirectory if mapping.media_type == "tv" else options.movie_subdirectory)
        / base
    )
    entries = []
    videos = {}
    known_videos = [
        f["name"] for f in task["files"] if Path(f["name"]).suffix.lower() in VIDEO
    ] + task.get("reference_videos", [])
    extras_folder = (
        folder / f"Season {mapping.season:02d}" if mapping.media_type == "tv" else folder
    )
    archive_folder = folder / "_resources" / task["key"][:8]
    cue_files, uncertain_cues = cue_images(task["files"], options)

    def archive(item, notice=""):
        item["target"] = str(
            archive_folder / Path(*[clean(p) for p in PurePosixPath(item["name"]).parts])
        )
        item["archive_root"] = str(folder / "_resources")
        item["notice"] = notice or "按原目录结构归档，不参与 Jellyfin 扫描"

    media = [f for f in task["files"] if classify(f["name"]) == "episode"]
    main = mapping.main_file
    inferred = False
    titles = (task["name"], metadata["title"])
    release_episode = episode(task["name"], metadata["title"]) if len(media) == 1 else None
    source_episodes = {name: episode(name, *titles) for name in known_videos}
    # Count distinct main episodes in the complete file list, including files
    # not selected in the downloader. Extras or two encodes of E01 are not a pack.
    episodes = {
        mapping.episodes.get(f["name"], source_episodes[f["name"]])
        for f in media
        if f["name"] not in mapping.specials
    }
    pack = mapping.media_type == "tv" and len(episodes - {None}) > 1
    if mapping.media_type == "movie" and not main and len(media) == 1:
        main = media[0]["name"]
    elif mapping.media_type == "movie" and not main and mapping.tmdb_id is None and media:
        # AB chooses the largest movie candidate. Require an unambiguous size
        # rather than let downloader iteration order decide equal-size ties.
        largest = max(f.get("size", 0) for f in media)
        candidates = [f for f in media if f.get("size", 0) == largest]
        if largest > 0 and len(candidates) == 1:
            main = candidates[0]["name"]
    elif (
        mapping.media_type == "movie"
        and not main
        and mapping.tmdb_id
        and len(media) > 1
        and mapping.scope == "all"
    ):
        # Anything unexplained remains a candidate, including unselected files.
        # Use exactly the same planning path as an explicit main_file selection.
        roles = [(f, movie_file_role(f["name"])) for f in media]
        candidates = [(f, role) for f, role in roles if role != "extra"]
        if len(candidates) == 1 and candidates[0][1] == "main":
            feature = candidates[0][0]
            if feature.get("progress", 0) >= 1 and feature.get("fingerprint"):
                main = feature["name"]
                inferred = True
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
            "work_root": str(folder),
        }
        if f.get("replay_source"):
            item.update(
                replay_source=f["replay_source"], replay_fingerprint=f["replay_fingerprint"]
            )
        if f.get("progress", 0) < 1 or not f.get("fingerprint"):
            item["reason"] = "文件未完成或本地文件不可用"
            entries.append(item)
            continue
        suffix = Path(name).suffix.lower()
        v = version(name, task["name"]) if options.version_suffix else ""
        edition = f"{v} 合集".strip() if pack and options.version_suffix else v
        label = " - " + edition if edition else ""
        values = dict(
            title=title,
            year=year,
            tmdb_id=mapping.tmdb_id or 0,
            season=mapping.season,
            episode=0,
            group=v,
            version=label,
            base=base,
        )
        target = None
        if suffix in VIDEO and (
            name in mapping.episodes
            or name in mapping.specials
            or kind == "episode"
            or name == main
        ):
            if mapping.media_type == "movie":
                if inferred and kind == "episode":
                    item["inferred_movie_role"] = True
                if name == main:
                    target = folder / (clean(options.movie_template.format(**values)) + suffix)
                    item["identity"] = {"media_type": "movie", "tmdb_id": mapping.tmdb_id}
                elif main:
                    target = folder / "extras" / clean(Path(name).name)
                else:
                    item["reason"] = "电影含多个候选正片，请指定 main_file"
            else:
                special = name in mapping.specials
                target_season = 0 if special else mapping.season
                ep = mapping.specials[name] if special else mapping.episodes.get(name)
                if ep is None:
                    ep = (
                        release_episode
                        if release_episode is not None and kind == "episode"
                        else source_episodes.get(name)
                    )
                    source_episodes[name] = ep
                    if (
                        ep is not None
                        and mapping.episode_offset
                        and release_episode is None
                        and re.search(r"(?i)S\d{1,3}E\d{1,4}", Path(name).stem)
                    ):
                        item["reason"] = "无法确认旧文件是否已应用集数偏移，请逐文件确认目标集号"
                        ep = None
                    if ep is not None:
                        adjusted = ep + mapping.episode_offset
                        # Same zero/non-positive safeguards as AB's native renamer.
                        if ep != 0 and adjusted > 0:
                            ep = adjusted
                if (
                    ep is None
                    or mapping.tmdb_id
                    and ep not in metadata.get("specials" if special else "episodes", [])
                ):
                    item["reason"] = item["reason"] or (
                        "无法对应 TMDB 集数，请在文件映射中确认"
                        if mapping.tmdb_id
                        else "无法确定集号，请在文件映射中确认"
                    )
                else:
                    target = (
                        folder
                        / f"Season {target_season:02d}"
                        / (
                            clean(
                                options.episode_template.format(
                                    **{**values, "episode": ep, "season": target_season}
                                )
                            )
                            + suffix
                        )
                    )
                    item["identity"] = {
                        "media_type": "tv",
                        "tmdb_id": mapping.tmdb_id,
                        "season": target_season,
                        "episode": ep,
                    }
        elif kind in {"subtitle", "audio"}:
            pass  # resolved after its corresponding video
        else:
            # Preserve every ancillary file under a source-specific directory; release NFOs
            # never become episode/movie metadata that could override TMDB identities.
            if kind == "resource":
                archive(item)
            else:
                target = extras_folder / kind / clean(Path(name).name)
        if target:
            item["target"] = str(target)
            if item["identity"]:
                # Link the Series/Movie directory, never individual episodes or extras.
                item["collection"] = collection
                item["collection_target"] = str(folder)
        entries.append(item)
        if suffix in VIDEO and target:
            videos[name] = str(target)
    for item in entries:
        if item["kind"] not in {"subtitle", "audio"} or item["reason"]:
            continue
        name = item["name"]
        audio = item["kind"] == "audio"
        if audio and not music_directory(name):
            matches = sidecar_candidates(name, known_videos, source_episodes, audio=True)
            if matches and any(n not in videos for n in matches):
                archive(item, "对应视频尚未就绪，音轨先按原目录归档")
                continue
        candidates = (
            []
            if audio and music_directory(name)
            else sidecar_candidates(name, videos, source_episodes, titles=titles, audio=audio)
        )
        if audio and not candidates:
            if not music_directory(name) and (
                episode(name) is not None
                or re.search(r"(?i)commentary|comment|评论|評論|音轨|音軌", Path(name).stem)
            ):
                archive(item, "疑似外挂音轨但无法唯一对应视频，已归档待确认")
            elif name in cue_files or str(PurePosixPath(name).parent) in uncertain_cues:
                archive(item, "整轨音频或 CUE 内容不明确；先归档，不自动切轨")
            elif Path(name).suffix.lower() not in MUSIC:
                archive(item, "独立音轨用途或音乐格式不明确，保留原目录归档")
            elif not options.music_root:
                item["kind"] = "music"
                archive(item, "尚未配置音乐库；先归档，配置后自动补整理")
            else:
                item["kind"] = "music"
                item["target"] = str(
                    Path(options.music_root)
                    / base
                    / Path(*[clean(p) for p in PurePosixPath(name).parts])
                )
                read_path = (
                    destination(options, item["replay_source"])
                    if item.get("replay_source")
                    else contained(options.source_root, item["source"])
                )
                item["notice"] = music_notice(read_path)
                item["music_tags"] = {
                    key: value
                    for key, value in (music_tags(read_path) or {}).items()
                    if key in {"ALBUM", "ALBUMARTIST", "DISCNUMBER", "DISCTOTAL", "TOTALDISCS"}
                }
            continue
        if len(candidates) != 1:
            archive(
                item,
                "字幕无法唯一对应视频，已归档待确认"
                if not audio
                else "音轨无法唯一对应视频，已归档待确认",
            )
            continue
        original = candidates[0]
        dst = Path(videos[original])
        item["target"] = str(dst.with_suffix("")) + sidecar_tail(name, original)
        video = next(i for i in entries if i["name"] == original)
        item["video_key"] = video["key"]
        if video.get("inferred_movie_role"):
            item["inferred_movie_role"] = True
    # A multi-disc set can use catalog numbers rather than Disc folders. Normalize
    # only sibling directories whose embedded album/artist/disc tags agree.
    music_entries = [
        i for i in entries if i["kind"] == "music" and i["target"] and not i.get("archive_root")
    ]
    disc_groups = {}
    parents = {}
    for item in music_entries:
        parent = Path(item["target"]).parent
        parents.setdefault(parent, []).append(item)
    for parent, tracks in parents.items():
        identities = set()
        for item in tracks:
            tags = item.get("music_tags", {})
            disc = tags.get("DISCNUMBER", "").split("/")[0]
            total = tags.get("DISCTOTAL", tags.get("TOTALDISCS", ""))
            if (
                tags.get("ALBUM")
                and tags.get("ALBUMARTIST")
                and disc.isdecimal()
                and total.isdecimal()
                and 1 <= int(disc) <= int(total)
                and int(total) > 1
            ):
                identities.add((tags["ALBUM"], tags["ALBUMARTIST"], int(disc), int(total)))
            else:
                identities.add(None)
        if len(identities) == 1 and None not in identities:
            album, artist, disc, total = next(iter(identities))
            disc_groups.setdefault((parent.parent, album, artist, total), []).append((parent, disc))
    normalized = {}
    for (album_parent, _, _, total), discs in disc_groups.items():
        numbers = [disc for _, disc in discs]
        if len(discs) > 1 and len(set(numbers)) == len(numbers):
            normalized.update(
                {
                    parent: (album_parent / f"Disc {disc:02d}", album_parent)
                    for parent, disc in discs
                }
            )
    for item in music_entries:
        target = Path(item["target"])
        parent = target.parent
        if parent in normalized:
            directory, album = normalized[parent]
            item["target"] = str(directory / target.name)
        else:
            album = (
                parent.parent if re.fullmatch(r"(?i)(?:disc|cd)[ _-]*\d+", parent.name) else parent
            )
        item["album_target"] = str(album)
        item["collection"] = collection
        item["collection_target"] = str(album)
        item.pop("music_tags", None)
    # Only explicit cover/lyrics files accompany music; booklet scans stay archived.
    music = {
        i["name"]: i
        for i in entries
        if i["kind"] == "music" and i["target"] and not i.get("archive_root")
    }
    for item in entries:
        if item["kind"] != "resource" or item["reason"]:
            continue
        path = PurePosixPath(item["name"])
        tracks = [n for n in music if PurePosixPath(n).parent == path.parent]
        cover = path.stem.lower() in {"cover", "folder"} and path.suffix.lower() in {
            ".jpg",
            ".jpeg",
            ".png",
            ".webp",
        }
        lyrics = path.suffix.lower() in {".lrc", ".elrc"} and any(
            PurePosixPath(n).stem == path.stem for n in tracks
        )
        if cover and not tracks:
            tracks = [
                n
                for n in music
                if PurePosixPath(n).parent.parent == path.parent
                and re.fullmatch(r"(?i)(?:disc|cd)[ _-]*\d+", PurePosixPath(n).parent.name)
            ]
        if tracks and (cover or lyrics):
            album = Path(music[tracks[0]]["target"]).parent
            if PurePosixPath(tracks[0]).parent != path.parent:
                album = album.parent
            item["target"] = str(album / clean(path.name))
            item.pop("archive_root", None)
            item["notice"] = "随专辑整理的封面或歌词；其他扫描图保持归档"
    # Classify against the full task first: external audio must not become music.
    # Omitted video/sidecar rows keep their existing ownership and history untouched.
    if mapping.scope == "resources":
        entries = [i for i in entries if i["kind"] in {"music", "resource"}]
    targets = {}
    for item in entries:
        if item["target"]:
            targets.setdefault(item["target"], []).append(item)
    for group in targets.values():
        for item in conflicting_claims(group):
            item["reason"] = "多个源文件指向同一目标，请确认映射"
            item["target"] = None
    blocked = {i["key"] for i in entries if not i["target"]}
    for item in entries:
        if item.get("video_key") in blocked:
            item["target"] = None
            item["reason"] = "对应视频有冲突，暂不挂载字幕或音轨"
    return entries
