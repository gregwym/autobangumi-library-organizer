"""AB is the default metadata source; saved TMDB mappings are explicit overrides."""

from .models import Mapping


def from_ab(task, override=None):
    native = task.get("ab_metadata") or {}
    if task.get("rule_id") is None or not (native.get("title") or "").strip():
        raise ValueError("尚无明确的 AB 番剧归属，请先关联下载任务或修正 AB 记录")
    values = {**task.get("rule_mapping", {}), **(override or {})}
    # Title, year and type are one AB identity. Season/file corrections can be
    # scoped to a task, but must not silently turn an AB movie into a TV series.
    values.update(media_type=native.get("media_type", "tv"), tmdb_id=None)
    return Mapping.model_validate(values), {
        "title": native["title"],
        "year": native.get("year") or "",
    }
