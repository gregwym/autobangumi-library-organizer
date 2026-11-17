from pathlib import Path
from string import Formatter
from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator


class PathMap(BaseModel):
    downloader: str = "default"
    remote: str
    local: str


class Options(BaseModel):
    source_root: str = Field(default="/downloads", title="下载根目录（AB 内路径）")
    library_root: str = Field(default="/library", title="媒体库根目录（AB 内路径）")
    music_root: str = Field(
        default="",
        title="音乐库根目录（可选，AB 内路径）",
        description="留空时音乐按原目录归档；填写后自动补整理已归档音乐。须为已存在、AB 可写且 Jellyfin 可见的独立音乐库目录；不自动创建 Jellyfin 媒体库。",
    )
    tv_subdirectory: str = Field(
        default="Shows",
        title="剧集分类子目录",
        description="若媒体库根目录已是 Jellyfin 电视剧库，请留空，直接在根目录下建立番剧文件夹。",
    )
    movie_subdirectory: str = Field(
        default="Movies",
        title="电影分类子目录",
        description="若媒体库根目录已是 Jellyfin 电影库，请留空。分类目录须对应 Jellyfin 的媒体库路径。",
    )
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
    collections_root: str = Field(
        default="",
        title="音乐合集清单目录（可选）",
        description="生成 collection.xml，交给媒体服务器的合集库扫描。须与音乐库、视频库、下载目录分开；两容器内的音乐路径须一致。不连接媒体服务器 API。",
    )

    @field_validator("tv_subdirectory", "movie_subdirectory")
    @classmethod
    def subdirectory(cls, value):
        if value and (
            value in {".", ".."} or any(c in value for c in "/\\\x00\r\n") or not value.strip()
        ):
            raise ValueError("分类子目录须为空或单个目录名，不能包含路径分隔符")
        return value

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
        if self.music_root:
            music = Path(self.music_root)
            if not music.is_absolute():
                raise ValueError("音乐库路径必须是绝对路径")
            music = music.resolve()
            if any(
                music == root or music in root.parents or root in music.parents for root in (a, b)
            ):
                raise ValueError("音乐库必须与下载和视频库目录分开，不能重叠")
        if self.collections_root:
            collection = Path(self.collections_root)
            if not self.music_root or not collection.is_absolute():
                raise ValueError("合集目录须为绝对路径，且须先配置音乐库")
            collection = collection.resolve()
            if any(
                collection == root or collection in root.parents or root in collection.parents
                for root in (a, b, Path(self.music_root).resolve())
            ):
                raise ValueError("合集目录不能与下载、视频库、音乐库重叠")
        for m in self.path_map:
            if not Path(m.remote).is_absolute() or not Path(m.local).is_absolute():
                raise ValueError("路径映射必须使用绝对路径")
        return self


class Mapping(BaseModel):
    scope: Literal["all", "resources"] = "all"
    media_type: Literal["tv", "movie"] = "tv"
    tmdb_id: int = Field(gt=0)
    season: int = Field(default=1, ge=0, le=999)
    episode_offset: int = Field(default=0, ge=-100000, le=100000)
    # Overrides use the exact source relative name, never just the inferred episode.
    episodes: dict[str, int] = Field(default_factory=dict)
    specials: dict[str, int] = Field(default_factory=dict)
    main_file: str | None = None

    @model_validator(mode="after")
    def episode_maps(self):
        if self.episodes.keys() & self.specials.keys():
            raise ValueError("同一文件不能同时指定正片和特别篇集号")
        if any(n < 0 or n > 100000 for n in (*self.episodes.values(), *self.specials.values())):
            raise ValueError("集号必须在 0 到 100000 之间")
        if self.specials and self.media_type != "tv":
            raise ValueError("电影不能使用 Season 00 特别篇映射")
        return self


class Binding(BaseModel):
    task_key: str
    mapping: Mapping
    rule_id: int | None = None


class Confirm(BaseModel):
    token: str = Field(min_length=20, max_length=100)
