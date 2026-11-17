"""Read-only post-scan verification; never equates refresh acceptance with recognition."""

from pathlib import Path


def compare(planned, items, local_root, remote_root):
    by_path = {}
    by_id = {i["Id"]: i for i in items if i.get("Id")}
    for item in items:
        paths = [item.get("Path")] + [s.get("Path") for s in item.get("MediaSources", [])]
        for path in paths:
            if path:
                by_path.setdefault(path, []).append(item)
    result = []
    for p in planned:
        if not p.get("target") or not p.get("identity"):
            continue
        path = str(Path(remote_root) / Path(p["target"]).relative_to(local_root))
        candidates = by_path.get(path, [])
        identity = p["identity"]
        ok = False
        for item in candidates:
            if identity["media_type"] == "movie":
                ok = item.get("Type") == "Movie" and str(
                    item.get("ProviderIds", {}).get("Tmdb")
                ) == str(identity["tmdb_id"])
            else:
                series = by_id.get(item.get("SeriesId"), {})
                ok = (
                    item.get("Type") == "Episode"
                    and item.get("ParentIndexNumber") == identity["season"]
                    and item.get("IndexNumber") == identity["episode"]
                    and str(series.get("ProviderIds", {}).get("Tmdb")) == str(identity["tmdb_id"])
                )
            if ok:
                break
        result.append(
            {
                "path": path,
                "state": "verified" if ok else "mismatch" if candidates else "not_indexed",
                "detail": "作品和季集已核对"
                if ok
                else "作品或季集不符"
                if candidates
                else "尚未扫描到此文件（或媒体库路径映射不正确）",
            }
        )
    return result


async def verify(options, planned):
    import httpx

    if not options.jellyfin_url or not options.jellyfin_api_key:
        raise ValueError("请先配置 Jellyfin 地址与 API Key")
    items = []
    async with httpx.AsyncClient(
        timeout=30, headers={"X-Emby-Token": options.jellyfin_api_key}
    ) as client:
        for start in range(0, 100000, 500):
            r = await client.get(
                options.jellyfin_url.rstrip("/") + "/Items",
                params={
                    "Recursive": "true",
                    "Fields": "Path,ProviderIds,MediaSources",
                    "IncludeItemTypes": "Series,Episode,Movie",
                    "StartIndex": start,
                    "Limit": 500,
                },
            )
            r.raise_for_status()
            data = r.json()
            page = data.get("Items", [])
            items.extend(page)
            if not page or len(items) >= data.get("TotalRecordCount", len(items)):
                break
        else:
            raise ValueError("媒体库超过本次核对上限，请缩小媒体库范围后重试")
    return compare(
        planned, items, options.library_root, options.jellyfin_library_root or options.library_root
    )
