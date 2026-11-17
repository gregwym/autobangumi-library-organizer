"""Conservative ancillary classification. Never change tags or split/remux sources."""

import re
from pathlib import Path, PurePosixPath

MUSIC = {".flac", ".mp3", ".m4a", ".aac", ".ogg", ".opus", ".wav", ".aif", ".aiff", ".wma"}
AUDIO = MUSIC | {".mka", ".ac3", ".eac3", ".dts", ".ape", ".wv", ".dsf", ".dff"}


def music_directory(name):
    return any(
        re.search(r"(?i)^(?:CDs?|OST|soundtracks?|music|albums?|drama)(?:$|[ _-])", part)
        for part in PurePosixPath(name).parts[:-1]
    )


def cue_images(files, options):
    """Hold audio referenced by multiple CUE tracks; malformed CUEs hold siblings."""
    from .planner import contained, destination

    images, uncertain = set(), set()
    for f in files:
        name = PurePosixPath(f["name"])
        if name.suffix.lower() != ".cue":
            continue
        try:
            if f.get("progress", 0) < 1 or not f.get("fingerprint"):
                raise ValueError("unfinished CUE")
            path = (
                destination(options, f["replay_source"])
                if f.get("replay_source")
                else contained(options.source_root, f["source"])
            )
            with path.open("rb") as handle:
                raw = handle.read(262145)
            if len(raw) > 262144:
                raise ValueError("CUE too large")
            content = raw.decode("utf-8-sig")
            current, tracks = None, {}
            for line in content.splitlines():
                match = re.match(r'\s*FILE\s+(?:"([^"]+)"|(\S+))\s+\S+', line, re.I)
                if match:
                    current = str(name.parent / (match[1] or match[2]))
                    tracks.setdefault(current, 0)
                elif re.match(r"\s*TRACK\s+\d+\s+AUDIO", line, re.I):
                    if current is None:
                        raise ValueError("missing FILE")
                    tracks[current] += 1
            if not tracks or not any(tracks.values()):
                raise ValueError("unknown CUE structure")
            images.update(n for n, count in tracks.items() if count > 1)
        except OSError, ValueError, UnicodeError:
            uncertain.add(str(name.parent))
    return images, uncertain


def music_tags(path):
    """Read bounded FLAC Vorbis comments; unsupported metadata stays explicitly unknown."""
    if Path(path).suffix.lower() != ".flac":
        return None
    try:
        with open(path, "rb") as handle:
            if handle.read(4) != b"fLaC":
                raise ValueError("not FLAC")
            budget = 1024 * 1024
            tags = {}
            for _ in range(128):
                header = handle.read(4)
                if len(header) != 4:
                    raise ValueError("short metadata")
                size = int.from_bytes(header[1:], "big")
                budget -= size + 4
                if budget < 0:
                    raise ValueError("metadata limit")
                if header[0] & 127 == 4:
                    block = handle.read(size)
                    if len(block) != size:
                        raise ValueError("short comments")
                    offset = 0

                    def integer():
                        nonlocal offset
                        if offset + 4 > len(block):
                            raise ValueError("short integer")
                        value = int.from_bytes(block[offset : offset + 4], "little")
                        offset += 4
                        return value

                    offset = 4 + integer()  # vendor string
                    count = integer()
                    if count > 10000:
                        raise ValueError("comment limit")
                    for _ in range(count):
                        length = integer()
                        if offset + length > len(block):
                            raise ValueError("short tag")
                        key, sep, value = (
                            block[offset : offset + length].decode("utf-8").partition("=")
                        )
                        offset += length
                        if sep and value.strip():
                            tags[key.upper()] = value.strip()
                    break
                handle.seek(size, 1)
                if header[0] & 128:
                    break
        return tags
    except OSError, ValueError, UnicodeError:
        return None


def music_notice(path):
    tags = music_tags(path)
    if tags is None:
        return "无法校验音乐标签；保留原专辑目录，建议检查标签"
    missing = [
        label
        for key, label in (("ALBUM", "专辑"), ("TITLE", "曲名"), ("TRACKNUMBER", "曲号"))
        if key not in tags
    ]
    if not (tags.get("ALBUMARTIST") or tags.get("ARTIST")):
        missing.append("艺术家")
    return (
        "音乐标签缺少" + "、".join(missing) + "；保留原目录，建议补标签"
        if missing
        else "按原专辑目录整理，保留内嵌音乐标签"
    )
