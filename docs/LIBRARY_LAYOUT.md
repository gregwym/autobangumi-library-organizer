# 媒体目录与 Jellyfin 扫描逻辑

v0.9.0：AB 已下载并成功整理的动画和专辑，自动归入同一作品合集。插件只操作文件和自有数据库，不保存媒体服务器凭据或调用其 API。

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
├── anime cds/
│   └── albums/
│       └── 作品名 (年份) [tmdbid-ID]/原始音乐目录/
│           ├── 专辑发行版 A/
│           │   ├── Disc 01/01.flac
│           │   └── Disc 02/01.flac
│           └── 专辑发行版 B（HiRes）/01.flac
└── collections/
    └── 作品名 [ab-tv-ID] [boxset]/collection.xml
```

视频和音乐继续使用现有库与原目录。collections 只存清单，不放媒体；从旧 `anime cds/collections` 迁移时，只移动插件自有清单，保留并验证媒体路径、inode、大小和 mtime。下载原文件保持原样。TMDB 覆盖为可选，因此默认路径没有 TMDB 标记；音乐路径不添加下载任务哈希。

插件按 entry 关联任务，只整理该 entry 的文件；多个 entry 可以映射同一作品，按作品聚合。种子／字幕组不决定合集身份。默认身份为 AB 类型、标题和年份；显式 TMDB 覆盖则按类型和 ID 聚合。不自动推断跨 TMDB 身份的系列关系，不扫描 AB 之外的资源，不修改 Jellyfin 根据 TMDB 生成的其他合集。

## 容器路径约定

| 内容 | NAS 主机 | AB 内路径 | Jellyfin 内路径／扫描 |
| --- | --- | --- | --- |
| 视频 | /volume1/video/bangumi | /library | /media/bangumi，保持现有剧集／电影库 |
| 音乐 | /volume1/video/anime cds/albums | /media/anime cds/albums | 同路径，Anime CDs 音乐库 |
| 合集清单 | /volume1/video/collections | /media/collections | 同路径，独立 Mixed 辅助索引库 |

配置 `collections_root=/media/collections`，并设置 `collection_path_map=[{"local":"/library","remote":"/media/bangumi"}]`。映射只改 XML 中的绝对引用，不移动文件，不负责网络连接；路径前缀按目录边界匹配，最长前缀优先。同路径无需配置。新目录须由管理员预先创建和挂载，插件不自行修改 Compose。

三个输出根与下载根互不包含。合集不放在 CD 内，因为它同时关联动画和音乐；也不放进真正的作品／专辑目录，避免改变媒体解析方式。

## 对照扫描逻辑

| 内容 | 插件输出 | Jellyfin 处理 |
| --- | --- | --- |
| 正片、字幕、外挂音轨 | 季集命名，sidecar 跟随具体视频 | 电视剧库按季集和同名规则解析 |
| 电影 | 独立 Movie 目录及命名 | 电影库正常解析 |
| 单碟／多碟音乐 | 原曲名和标签；标签一致才规范 Disc 01 / Disc 02 | MusicAlbum 目录，Audio 读取标签 |
| CD、HiRes、发行版本 | 保留不同专辑路径 | 分别识别，不用番剧 TMDB 猜音乐标签 |
| 音乐封面、歌词 | 明确 cover/folder、同名 LRC/ELRC 随专辑 | 读取本地图片和歌词 |
| 合集 | collection.xml 引用 Series／Movie／MusicAlbum 目录 | BoxSetResolver 识别，XML provider 导入 LinkedChildren |
| 字体、扫描图、其他附件、未明确的整轨 CUE | 原结构归档到 _resources | 空 .ignore 排除归档扫描 |

BoxSet 默认 First 优先级，先于 MusicArtist、MusicAlbum 和普通 Folder。`[boxset]` 或 `collection.xml` 会把其所在目录识别成 BoxSet，关联成员的 BoxSet 不按普通目录递归。因此保持独立清单目录，原有视频库和音乐库继续正常扫描。

## 自动聚合与历史记录

成功视频引用整个 Series 或 Movie 目录，多个集数、季和字幕组只形成一个目录成员；音乐引用专辑目录，不细分第几季 OST。普通单一剧集只登记关联，不额外生成重复合集，后续有专辑入库即自动生成。新文件整理成功后增量更新清单；未完成、失败、歧义和归档附件不作为成员。

旧版成功记录可补建视频关联，须同时核对已提交成功、目标所有权及 fingerprint。全局「更新作品合集」仅重建清单，不重新计划、复制或移动媒体；适合升级和路径映射变更。仅音乐与归档范围仍不处理视频文件，可以读取其既有成功记录用于关联。没有历史成功证明的已有媒体不接管，也不盲扫目录。

清单按集合排序去重；内容不变时不改写 XML。下载任务移除后保留已成功入库资源及关联。手工编辑过的清单不覆盖；旧清单根退出当前配置后保留，迁移由管理员核对所有权后执行。

## 媒体服务器配置与浏览

保留 Anime CDs 音乐库扫描 albums；独立辅助索引库扫描 `/media/collections`，从用户首页隐藏该索引入口。启用本地 XML 读取、目录监控，`MetadataSavers=[]`，由插件维护 XML、Jellyfin 只读扫描。不要修改用户手工管理的其他合集库的保存策略。

音乐库的「合集」按成员所属音乐库筛选，包含本库专辑的作品会出现在这里；全局「合集」同时展示其他电影／作品合集。混合库的文件夹页隐藏 BoxSet，不能把辅助索引入口当作浏览入口。不同客户端对跨媒体成员的展示需实际验证，插件不能保证客户端 UI。

依赖顺序是成员先索引、合集再解析引用。BaseItem.GetLinkedChild 对暂未找到的路径可缓存 Guid.Empty；首次迁移应等成员扫描完成，再刷新合集元数据确认。增量整理完成与合集已显示是两个状态，显示取决于 Jellyfin 目录监控和定时扫描。插件不通过 API 刷新或修库。

Jellyfin 对外部清单关联取并集：新增引用可导入，从 XML 删除路径不保证删除已有数据库关联。需要重建时由 Jellyfin 管理，不由 AB 插件修其数据库。

## 依据

- [官方音乐目录与多碟规则](https://jellyfin.org/docs/general/server/media/music/)
- [BoxSetResolver](https://github.com/jellyfin/jellyfin/blob/master/Emby.Server.Implementations/Library/Resolvers/Movies/BoxSetResolver.cs)
- [MusicAlbumResolver](https://github.com/jellyfin/jellyfin/blob/master/Emby.Server.Implementations/Library/Resolvers/Audio/MusicAlbumResolver.cs)
- [MusicArtistResolver](https://github.com/jellyfin/jellyfin/blob/master/Emby.Server.Implementations/Library/Resolvers/Audio/MusicArtistResolver.cs)
- [BoxSet XML provider](https://github.com/jellyfin/jellyfin/blob/master/MediaBrowser.LocalMetadata/Providers/BoxSetXmlProvider.cs)
- [BoxSet linked children 与递归](https://github.com/jellyfin/jellyfin/blob/master/MediaBrowser.Controller/Entities/Movies/BoxSet.cs)
- [外部合集元数据合并](https://github.com/jellyfin/jellyfin/blob/master/MediaBrowser.Providers/BoxSets/BoxSetMetadataService.cs)
- [关联路径解析及未匹配缓存](https://github.com/jellyfin/jellyfin/blob/master/MediaBrowser.Controller/Entities/BaseItem.cs)
