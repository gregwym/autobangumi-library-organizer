# 媒体目录与 Jellyfin 扫描逻辑

2026-11-17：核对插件代码、NAS 目录、Jellyfin 12.4 当前库配置，以及 Jellyfin 上游解析实现。下述共同父目录扫描属于待切换验证方案，不能把源码推断当作目标服务器实测。

## 目录与职责

```text
/volume1/video/
├── bangumi/
│   └── 作品名 (年份) [tmdbid-ID]/
│       ├── Season 03/
│       │   ├── 作品名 S03E01 - 字幕组.mkv
│       │   ├── 作品名 S03E01 - 字幕组.zh-Hans.ass
│       │   └── extras/…
│       └── _resources/
│           ├── .ignore
│           └── 来源标识/原目录结构/字体、扫描图、LOG、其他附件…
└── anime cds/
    ├── albums/
    │   └── 作品名 (年份) [tmdbid-ID]/
    │       └── 原始音乐目录（如 CDs）/
    │           ├── 专辑发行版 A/
    │           │   ├── Disc 01/01.flac
    │           │   └── Disc 02/01.flac
    │           └── 专辑发行版 B（HiRes）/01.flac
    └── collections/
        └── 作品名 [ab-tv-ID] [boxset]/collection.xml
```

下载目录保留原文件。插件按 entry 关联任务，只整理该 entry 的文件；多个 entry 可以落到同一作品目录，并聚合到同一作品合集。发布方式为硬链 → Reflink → 复制，状态与所有权保存在插件数据库。v0.7.3 起音乐目录不插入任务哈希；附件归档仍保留来源标识，以避免原始附件路径重名。

插件的输出根仍为 `music_root=/media/anime cds/albums`、`collections_root=/media/anime cds/collections`。这两个目录互不包含，符合插件的范围和所有权校验。Jellyfin 扫描根可以是它们的共同父目录；不能因此把插件的 music_root 也改成父目录，否则会触发输出范围重叠校验。

## 对照扫描逻辑

| 内容 | 插件输出 | Jellyfin 处理 |
| --- | --- | --- |
| 正片、字幕、外挂音轨 | 季集命名；字幕、音轨跟随对应视频的文件名 | 电视剧库按季集和同名规则解析 |
| 单碟音乐 | 一张专辑一个目录，保留曲目名称和标签 | MusicAlbum 识别专辑目录；Audio 读取内嵌标题、艺术家、曲号等 |
| 多碟音乐 | 仅标签明确且相互一致时规范成 Disc 01 / Disc 02 | 按专辑及碟号标签识别；内嵌标签优先 |
| CD、HiRes、不同发行版 | 保留不同专辑路径 | 分别扫描，不由番剧 TMDB 信息猜测音乐标签 |
| 音乐封面、歌词 | 明确的 cover/folder、同名 LRC/ELRC 随专辑 | 读取本地图片和歌词 |
| 合集清单 | 独立目录内的 collection.xml，引用专辑目录绝对路径 | BoxSetResolver 识别目录；XML provider 导入 LinkedChildren |
| 字体、扫描图、其他附件、未明确的整轨 CUE | 按原结构归档到 _resources | 空 .ignore 排除归档，不混入媒体扫描 |

上游解析优先级：BoxSet 默认 First，音乐艺术家 Second，音乐专辑 Third，普通目录 Last。BoxSetResolver 识别 `[boxset]` 或 `collection.xml`，没有限定必须为独立 Boxsets/Mixed 类型库。因此一个 Music 库扫描共同父目录，原则上能同时发现音乐专辑和合集；独立辅助库不是解析器要求。

不要把 collection.xml 放入存放实际专辑的作品目录，也不要放在 anime cds 根目录。它会把该目录变成 BoxSet；带关联成员的 BoxSet 不按普通文件夹继续递归。独立的 collections 分支保留了正常的专辑扫描路径。

当前深层原目录布局可以识别专辑，但 Jellyfin 也把原始 CDs 目录识别成一个目录型 MusicArtist。实际专辑艺术家仍来自音乐标签；“文件夹视图”与“艺术家视图”不是同一套层级。去掉任务哈希不会自动改变这一行为。

## 建议的单库配置与验证

保留现有 Anime CDs 的 Music 类型和库身份，把扫描路径改为 `/media/anime cds`，让 albums 和 collections 都被覆盖；最终不再保留重叠扫描同一清单的 Anime CD Collections 辅助库。仅移除辅助库注册，不删除 XML 或媒体文件。切换前保存配置、专辑身份及数量，扫描后对照确认。

扫描清单的库保留本地 XML 读取，将 MetadataSavers 设为 []，由插件维护 XML、Jellyfin 只读导入。AB 与 Jellyfin 的音乐容器路径目前一致：`/media/anime cds`。合集 XML 引用的是 Jellyfin 可见的绝对专辑路径，不是 NAS 主机路径。

依赖关系是“专辑先入索引，合集再关联专辑”，不是“目录名 albums 按字母先于 collections 就保证先扫描”。上游 BaseItem.GetLinkedChild 会把尚未找到的关联项缓存为 Guid.Empty；不能假设后续专辑出现就立即重新解析。首次或大规模路径迁移应等全库扫描完成，再刷新合集元数据确认成员。后续由目录监控和 Jellyfin 定时扫描导入；插件不调用媒体服务器 API。增量文件整理与媒体服务器完成合集关联是两件事，合并扫描方案还必须验证“新增专辑后合集更新”的顺序和缓存行为。

目标服务器实测基线为 18 个 MusicAlbum、197 首 Audio、1 个音乐 BoxSet，含 18 个专辑成员。共同父目录方案尚未在目标服务器切换，不能据此宣称辅助库已移除。

Jellyfin 上游对外部合集成员取并集：新增路径可以导入，删除 XML 中路径不等于删除数据库旧关联。路径迁移后要确认旧专辑不再被索引，并检查最终合集没有重复或失效成员。

## 同一作品合集关联动画和音乐

BoxSet 的关联成员可以是剧集 Series、电影 Movie 或专辑 MusicAlbum；因此可用一份作品合集清单，链接整部动画及相关专辑，无需把两类文件搬到同一目录。当前插件只生成音乐成员，动画成员尚未实现。

当前路径差异必须先解决：视频在 AB 是 `/library`，Jellyfin 是 `/media/bangumi`；音乐两边是 `/media/anime cds`。清单不能直接写入 AB 的 `/library`。上游支持把相对路径按合集目录解析，但当前两个容器的目录拓扑不同，直接按 AB 路径计算相对位置也不成立。后续方案应明确文件系统路径约定，不给整理插件加入 Jellyfin API 凭据，也不猜测媒体服务器挂载。对于用户从音乐库进入的合集，其包含动画的呈现还应在目标客户端验证。

音乐库的“合集”入口按成员所属库筛选；全局“合集”页仍可能同时展示电影和音乐合集。合并扫描根不能把全局合集页变成仅音乐页面。

## 依据

- [官方音乐目录与多碟规则](https://jellyfin.org/docs/general/server/media/music/)
- [BoxSetResolver](https://github.com/jellyfin/jellyfin/blob/master/Emby.Server.Implementations/Library/Resolvers/Movies/BoxSetResolver.cs)
- [MusicAlbumResolver](https://github.com/jellyfin/jellyfin/blob/master/Emby.Server.Implementations/Library/Resolvers/Audio/MusicAlbumResolver.cs)
- [MusicArtistResolver](https://github.com/jellyfin/jellyfin/blob/master/Emby.Server.Implementations/Library/Resolvers/Audio/MusicArtistResolver.cs)
- [BoxSet XML provider](https://github.com/jellyfin/jellyfin/blob/master/MediaBrowser.LocalMetadata/Providers/BoxSetXmlProvider.cs)
- [BoxSet linked children 与递归](https://github.com/jellyfin/jellyfin/blob/master/MediaBrowser.Controller/Entities/Movies/BoxSet.cs)
- [外部合集元数据合并](https://github.com/jellyfin/jellyfin/blob/master/MediaBrowser.Providers/BoxSets/BoxSetMetadataService.cs)
- [关联路径解析及未匹配缓存](https://github.com/jellyfin/jellyfin/blob/master/MediaBrowser.Controller/Entities/BaseItem.cs)
