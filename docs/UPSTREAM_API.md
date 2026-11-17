# AB 4.0 后续统一上游提案：订阅审核 + 媒体库整理

状态：**设计提案，尚未向 AutoBangumi 提 PR**。先在两个插件中验证行为，再拆分评审友好的 PR。基线为 AB 4.0.0 `bc23984ae4a6d388c0b8381e48f6aa9b60e338ba`。这里的 API 名称是建议，不是 AB 已存在的接口。

相关项目：
- [订阅审核](https://github.com/gregwym/autobangumi-subscription-review)：版本多选、待确认、历史清理、v2 策略。
- [媒体库整理](https://github.com/gregwym/autobangumi-library-organizer)：源文件保留、TMDB 映射、媒体库硬链、文件状态清单。

## 1. 共同原则与现状

下载任务、解析候选、番剧身份、下载状态、整理状态需要不同的稳定身份。不能用可修改的 qB tag/category 充当数据库；不能因插件停用就退回绕过审核或修改下载文件的流程。

当前 `rename_strategy.target_name` 只能提供种子内新文件名；`media_files` 只有 media/subtitle/ignore；`torrent.organized` 是宿主整理之后的事件。因此完整媒体库整理不能只靠 rename 插件。

当前两个插件的所有临时私有依赖都集中在各自 `bridge40.py`。媒体库整理无需修改 AB 源代码/表结构，不在 qB 中存储状态；显式接管时关闭原生整理与硬链，并通过内存守卫拦住手工触发的宿主 rename。卸载不恢复原生整理。AB 仍可能在下载提交时写关联 tag，此兼容版不读取它。彻底移除宿主关联 tag 要在关联 API 就绪后做。

## 2. 下载任务服务（共同阻塞项）

建议 `ctx.downloads`：

```python
async def list_tasks(cursor=None, completed=None) -> TaskPage: ...
async def files(task_id) -> list[DownloadFile]: ...
async def submit(request, *, idempotency_key) -> Submission: ...
async def lookup_submission(idempotency_key) -> Submission | None: ...
```

稳定主键：AB download_id；明确 downloader_instance_id、provider_task_id、source torrent identity（v1/v2 hash 或 provider opaque id），禁止把凭据/临时 RSS URL 作为公共身份。

DownloadFile 至少含稳定 file_id、下载器相对路径、本地路径映射结果、大小、完成度、是否选中。完成事件只是唤醒信号，须可分页查询和重放，覆盖启动前已下载任务、事件丢失、插件首次安装。

提交关联（download_id → bangumi_id / release_id / submitted policy revision）和提交意图须先落库，再发送网络请求，完成后对账。远端结果不确定不能盲目重发。插件更新不得改变 downloader 实例身份。

未来移除 `ab:<bangumi_id>`、`ab:renamed` 的身份/状态作用；旧 tag 只作一次性迁移提示，需与 DB、任务文件核对，之后不读写。用户自有标签不删除。

## 3. 订阅审核需要的正式接口

### 所有下载入口统一的准入钩子

RSS、搜索、手动、补集、重试必须经过同一个 `download.preflight`。输入包含 release、bangumi、下载意图、来源、已验证的 episode identity。返回 allow / hold / reject；hold 不等于“已经下载”或“永久已过滤”。禁止钩子缺席时悄悄回退到未审核下载器。

### 持久候选与批次处理

`releases.discovered(batch)` 在全局/单番过滤的可追溯阶段保存候选；同批 v1/v2 先全部入队再选择。版本维度（组/分辨率/编码/字幕/来源）与修订号分别存储。配置过滤变化后可重评旧的未下载候选，不仅处理 RSS 新 URL；禁止因此自动补下所有旧候选。

`candidates.query/reparse/rebind` 应返回明确匹配证据与歧义，支持未知字幕组；跨季同名不靠“较高季号优先”猜。精确字幕组优先于包含关系；同分多候选保持未匹配。

### 历史维护与映射修订

`history.preview_clear(selection)` → revision-bound token；`history.clear(token)` 同一受控操作清除宿主下载历史与插件去重状态，有备份、审计、并发边界，不删除源文件或下载器任务。

番剧季集偏移/身份修改发布 `bangumi.mapping_changed(old,new,revision)`，可重算候选与去重键；保留无法安全映射的旧记录，不把去重清空当作修复。

## 4. 媒体库整理需要的正式接口

### 独立 organizer 扩展点和独占所有权

建议 `ctx.organizers.claim(scope, provider_id)` +可持久接管配置。scope 可为下载器/目录/规则。宿主原生整理和多个插件不能同时持有相同 scope。插件异常/停用时 scope 暂停，不能自动退回原生文件重命名。

当前兼容版只能全局接管；未来上游需支持分范围接管。停用原生 rename 不能妨碍下载、资源候选、状态查询。

### TMDB 与稳定映射服务

`metadata.search(type,query)`、`metadata.get(type,id)`、`metadata.episodes(tv_id,season)` 通过宿主凭据/代理/缓存服务调用。必须保留 movie/tv 类型，不以 title 相似度代替媒体身份。搜索歧义由用户确认。

映射记录：source release / file id → TMDB type/id/season/episode(s)，带映射来源、revision、人工锁定状态。规则级映射与逐文件 override 分开；后者不能意外应用到后续不同文件。

### 文件级计划与执行账本

建议对象：`OrganizePlan(revision, mapping_revision, source_snapshot, operations)`。

每项包括源文件 fingerprint、file kind、目标、版本标签、原因、previous owned paths。正片/特别篇/附加内容/字幕/资源/未知明确区分，不能用扩展名+目录深度推断正片。

状态：discovered → needs_mapping / planned → linking → linked；可有 held_conflict / retry_wait / retained_resource。不要求整个合集全部改名成功才处理字幕或宣布正片可用。

执行前验证预览版本，创建目标前记录意图；不覆盖非自有文件；按实际策略验证源与目标身份后写结果；结果必须区分 hardlink / reflink / copy。当前插件采用硬链 → Linux FICLONE → 普通复制，明确告知复制占用额外空间；不能将三种方式一律标为“硬链成功”。分别持久化源与目标 fingerprint，避免副本重复处理或映射变更时误清理。磁盘满、权限和 I/O 错误不得按“不支持”降级。新链接验证后才允许删除经 fingerprint 确认仍为自有的旧链接。崩溃恢复必须依赖意图和文件事实，不依赖标签或日志。

计划目标碰撞在执行前解决。字幕绑定具体视频版本；电影多正片、多个集数区间、季 0 特典不能一律套简单偏移。

### 新事件与 Jellyfin

`library.files_committed` 应包含实际媒体库路径及媒体身份，供媒体服务器刷新、NFO、通知使用。不要把下载目录路径复用为媒体库路径。事件至少一次投递，带稳定 operation_id，订阅者幂等。

扫描请求成功 ≠ 识别成功。可选 `library.verify` 按媒体库路径、作品 provider id、季集号对账，包含 Jellyfin MediaSources 多版本路径；不自动覆盖用户元数据。附加资源保存率和可播放媒体识别率分别报告。

## 5. 共享维护和更新 API

建议 `ctx.maintenance.transaction(scope)` 暂停相关后台任务、获取有序锁、备份 SQLite/配置、执行/恢复；禁止持 DB 写事务等待下载器/TMDB 网络。SQLite BUSY 重试只针对幂等、短事务，不掩盖死锁。

建议宿主提供 `plugins.updates.check/install`，固定来源、清单身份、版本、checksum、兼容性校验、安装锁、代码回滚和可声明的数据迁移备份。用户选择自动检查 + 单击升级。官方市场签名策略与用户明确安装的本地未签名来源分开；不全局绕过签名。

目前两个插件各自实现固定仓库 Release 更新器，每 6 小时检查；成功/失败重载测试使用真正的 AB PluginManager。备份数据但失败只自动回滚代码，停电恢复边界需公开记录。

## 6. 建议上游提交顺序

1. 固化失败案例测试：季号误当集数、特典冲突刷屏、跨季匹配、未知字幕组、过滤变化候选重评。
2. 统一任务服务/准入和持久关联，消除 tag 依赖，提供预览/维护语义。
3. organizer 所有权、TMDB 映射服务、媒体库提交事件，保留原生实现为默认 provider。
4. 文件级状态及迁移、统一更新服务。两个插件逐项删除私有适配器。

验收必须涵盖断电/进程退出、版本升级回滚、同一任务重复事件、路径变化、目标被用户替换、并发规则修改、未下载文件、已有做种及媒体库不受影响。提交 PR 时每项说明已实现/仅提案，不一次性塞入整套 UI 与私有兼容层。

## Provider ID 作用域

AB 4.0 的 `scheduled_task` ID 是全局唯一，两个插件都使用 `check-update` 会导致后加载插件整体失败。当前整理插件使用 `library-organizer-` 前缀规避；建议宿主让 scheduled_task 与 API_ROUTER 一样按插件隔离，在调度器使用 `<plugin_id>/<provider_id>` 作为稳定标识，并覆盖插件共存与热更新测试。

## Jellyfin 刷新认证兼容性

AB 内置 media-server-refresh 使用旧 `X-Emby-Token`。Jellyfin 12 默认禁用旧认证，现有有效 API Key 也会返回 401；当前整理插件刷新与核对统一使用 `Authorization: MediaBrowser Token="..."`。后续上游 PR 应同步修正内置刷新插件并增加拒绝旧认证头的回归测试，不要求用户重新生成密钥或开启旧认证。参考 [Jellyfin 12 发布说明](https://jellyfin.org/posts/jellyfin-release-12.0/)。
