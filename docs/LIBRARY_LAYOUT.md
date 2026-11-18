# 目录与 Jellyfin 扫描

[返回首页](../README.md)

下列均为通用容器路径示例。管理员负责挂载与建库；插件负责输出文件及自有 `collection.xml`，不连接 Jellyfin API。

## 目录示例

```text
/library/
├── Shows/示例动画 (2024)/
│   ├── Season 01/
│   │   ├── 示例动画 S01E01 - Group.mkv
│   │   ├── 示例动画 S01E01 - Group.zh-Hans.ass
│   │   └── extras/…
│   └── _resources/
│       ├── .ignore
│       └── 来源标识/原目录结构/字体、扫描图、LOG 等
└── Movies/示例电影 (2024)/…

/music/示例动画 (2024)/原始专辑路径/
├── 专辑 A/Disc 01/01.flac
├── 专辑 A/Disc 02/01.flac
└── 专辑 B（HiRes）/01.flac

/collections/示例动画 (2024) [ab-tv] [boxset]/collection.xml
```

默认按 AB 的标题、年份生成作品目录；显式 TMDB 纠错时追加 `[tmdbid-ID]`。音乐不添加下载任务哈希。碟号明确且专辑、艺术家标签一致的相邻目录可规范成多碟专辑；其他发行版保留原路径。

| 输出 | Jellyfin 库类型／处理 |
| --- | --- |
| `/library/Shows` | 电视剧；按季集和同名字幕／音轨识别 |
| `/library/Movies` | 电影；正片和特典分别处理 |
| `/music` | 音乐；专辑识别依赖音频标签，不用番剧 TMDB 信息改标签 |
| `/collections` | 独立混合类型辅助库，导入本地合集清单 |
| `_resources` | 原结构归档，空 `.ignore` 排除扫描 |

若 `library_root` 已经是电视剧库根目录，将 `tv_subdirectory` 留空。音乐库只扫描 `music_root`，不必扫描它的上层目录。

## 音乐与附件

`music_root` 留空时音乐先归档，配置后可补整理。明确的封面和同名 LRC/ELRC 随专辑输出；字体、扫描图、压缩包、LOG、NFO 等归档。无法关联的视频字幕／音轨也归档并显示原因。

整轨音频＋CUE、不支持或无法确认的音乐格式保留归档，不自动切轨或解压。插件不改音乐标签，入库不代表专辑刮削正确。Jellyfin 的[音乐目录规则](https://jellyfin.org/docs/general/server/media/music/)以专辑目录和内嵌标签为准。

## 作品合集

设置 `collections_root` 后，成功整理的 Series、Movie 和专辑目录可归入同一作品合集。默认以 AB 类型、标题、年份聚合；显式 TMDB 纠错后以类型和 ID 聚合。多个条目可以共享作品身份，但文件处理仍严格限定在各自关联任务中。

单一剧集暂不另建重复合集，后续专辑入库即自动关联；音乐不细分第几季 OST。只纳入插件已成功整理且目标身份核验通过的资源，不扫描外部收藏或推断不同作品间的系列关系。

1. 将清单根目录加入独立混合类型辅助库，保留本地 XML 读取，关闭该库的元数据写回（`MetadataSavers=[]`）。可从首页隐藏辅助库入口。
2. 先扫描视频、音乐成员，再扫描合集。可从音乐库的「合集」查看包含本库专辑的作品；客户端展示可能不同。
3. 清单引用必须使用 Jellyfin 内的绝对路径。若 AB 的 `/library` 对应 Jellyfin 的 `/media/video`，设置 `collection_path_map=[{"local":"/library","remote":"/media/video"}]`；同路径不需要映射。前缀按目录边界匹配，最长优先。

合集目录与下载、视频、音乐根目录不能重叠。不要将 `collection.xml` 放在真实专辑或视频的父目录：Jellyfin 会将该目录识别为 BoxSet，影响普通媒体扫描。清单只存引用，不复制媒体。

成功整理后增量更新 XML，内容不变不改写。全局「更新作品合集」只校验历史并重建清单，不重新整理媒体。用户改过的清单不覆盖；更换清单根目录时旧文件保留，需自行调整扫描范围。

Jellyfin 导入外部合集时可能合并已有成员；仅从 XML 删除路径不保证移除已索引关系，需要在 Jellyfin 管理。插件不验证扫描结果，也不修改 Jellyfin 数据库。

实现依据：[BoxSet 目录识别](https://github.com/jellyfin/jellyfin/blob/master/Emby.Server.Implementations/Library/Resolvers/Movies/BoxSetResolver.cs)、[XML 导入](https://github.com/jellyfin/jellyfin/blob/master/MediaBrowser.LocalMetadata/Providers/BoxSetXmlProvider.cs)、[合集元数据合并](https://github.com/jellyfin/jellyfin/blob/master/MediaBrowser.Providers/BoxSets/BoxSetMetadataService.cs)。
