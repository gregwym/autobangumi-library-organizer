from pathlib import Path
from string import Formatter
from typing import Literal

from ab_sdk import secret_field
from pydantic import BaseModel, Field, field_validator, model_validator


class PathMap(BaseModel):
    downloader: str = "default"
    remote: str
    local: str


class Options(BaseModel):
    source_root: str = Field(default="/downloads", title="下载根目录（AB 内路径）")
    library_root: str = Field(default="/library", title="媒体库根目录（AB 内路径）")
    path_map: list[PathMap] = Field(default_factory=list, title="下载器路径映射")
    version_suffix: bool = Field(default=True, title="媒体库文件附加版本后缀")
    episode_template: str = Field(
        default="{title} S{season:02d}E{episode:02d}{version}",
        title="剧集文件名模板",
        max_length=512,
        description="不含扩展名。可用 title、year、tmdb_id、season、episode、group、version、base；version 自动生成带分隔符的组名后缀。",
    )
    movie_template: str = Field(
        default="{base}{version}",
        title="电影文件名模板",
        max_length=512,
        description="不含扩展名。base 为作品名、年份与 TMDB ID；version 为组名后缀。默认值适合 Jellyfin。",
    )
    jellyfin_url: str = Field(default="", title="Jellyfin 地址（可选）")
    jellyfin_library_root: str = Field(
        default="", title="Jellyfin 内媒体库根路径（不同容器路径时填写）"
    )
    jellyfin_api_key: str = secret_field(title="Jellyfin API Key（可选）")

    @field_validator("episode_template", "movie_template")
    @classmethod
    def filename_template(cls, value):
        allowed = {"title", "year", "tmdb_id", "season", "episode", "group", "version", "base"}
        if not value.strip() or any(c in value for c in "/\\\x00\r\n"):
            raise ValueError("文件名模板不能为空或包含路径分隔符")
        for _, field, spec, conversion in Formatter().parse(value):
            if field is None:
                continue
            if field not in allowed or conversion:
                raise ValueError("模板含不支持的字段或转换")
            if spec and (
                field not in {"season", "episode", "tmdb_id"} or spec not in {"d", "02d", "03d"}
            ):
                raise ValueError("数字字段仅支持 d、02d 或 03d 格式")
        return value

    @model_validator(mode="after")
    def roots(self):
        a, b = Path(self.source_root), Path(self.library_root)
        if not a.is_absolute() or not b.is_absolute():
            raise ValueError("下载与媒体库路径必须是绝对路径")
        a, b = a.resolve(), b.resolve()
        if a == b or a in b.parents or b in a.parents:
            raise ValueError("下载与媒体库目录不能重叠")
        for m in self.path_map:
            if not Path(m.remote).is_absolute() or not Path(m.local).is_absolute():
                raise ValueError("路径映射必须使用绝对路径")
        return self


class Mapping(BaseModel):
    media_type: Literal["tv", "movie"] = "tv"
    tmdb_id: int = Field(gt=0)
    season: int = Field(default=1, ge=0, le=999)
    episode_offset: int = Field(default=0, ge=-100000, le=100000)
    # Overrides use the exact source relative name, never just the inferred episode.
    episodes: dict[str, int] = Field(default_factory=dict)
    main_file: str | None = None


class Binding(BaseModel):
    task_key: str
    mapping: Mapping
    rule_id: int | None = None


class Confirm(BaseModel):
    token: str = Field(min_length=20, max_length=100)
