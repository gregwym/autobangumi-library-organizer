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

0.5.0 兼容实现已改为 AB Torrent.bangumi_id + downloader_id + qb_hash 确认 entry；历史 qb_hash 为空时只解析标准 magnet BTIH 或 mikanani.me/Download/YYYYMMDD/<hash>.torrent，再与 qB 精确核对。标题正则和下载器标签不参与归属判定。缺失/冲突关联保持待确认，人工关联不能覆盖明确的 AB 所属 entry。单番预览在读取下载文件清单前限制归属，执行时重新核对，旧明细仅更正归属而不搬动媒体文件。此临时 URL 兼容逻辑不能替代正式的持久下载关联 API。

不同 entry 可映射同一 TMDB 作品和季，在目标库保留多个组版本；不可反过来按媒体库身份扩大源文件范围。增量验收覆盖新任务完成、任务内字幕随后完成、AB 记录延迟出现、重复扫描零重复复制，以及修改一个 entry 不触碰另一个 entry 的文件。

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

映射记录：source release / file id → TMDB type/id/season/episode(s)，带映射来源、revision、人工锁定状态。AB 原有季度、季偏移、集数偏移是默认季集来源，插件应实时继承，不另建同级配置。旧文件已应用偏移的情况须有原始发布集号或明确映射证明，不能再加一次。规则级映射与逐文件 override 分开；后者不能意外应用到后续不同文件。

### 文件级计划与执行账本

建议对象：`OrganizePlan(revision, mapping_revision, source_snapshot, operations)`。

每项包括源文件 fingerprint、file kind、目标、版本标签、原因、previous owned paths。正片/特别篇/附加内容/字幕/资源/未知明确区分，不能用扩展名+目录深度推断正片。

状态：discovered → needs_mapping / planned → linking → linked；可有 held_conflict / retry_wait / retained_resource。不要求整个合集全部改名成功才处理字幕或宣布正片可用。

执行前验证预览版本，创建目标前记录意图；不覆盖非自有文件；按实际策略验证源与目标身份后写结果；结果必须区分 hardlink / reflink / copy。当前插件采用硬链 → Linux FICLONE → 普通复制，明确告知复制占用额外空间；不能将三种方式一律标为“硬链成功”。分别持久化源与目标 fingerprint，避免副本重复处理或映射变更时误清理。磁盘满、权限和 I/O 错误不得按“不支持”降级。新链接验证后才允许删除经 fingerprint 确认仍为自有的旧链接。崩溃恢复必须依赖意图和文件事实，不依赖标签或日志。

计划目标碰撞在执行前解决。字幕绑定具体视频版本；电影多正片、多个集数区间、季 0 特典不能一律套简单偏移。

0.6.0 的附带资源策略：视频特典归所属季；单个文件可明确覆盖为 Season 00 特别篇，不改变同任务正片季度。音乐使用可选独立输出根目录，未配置则归档；配置后可重新评估既有归档，甚至在下载任务移除后使用已验证的插件自有副本。重放仍须核对当前 AB entry 归属、绑定、源身份和副本 fingerprint，不因媒体库身份相同跨 entry。目标类型应扩展为 video / music / archive，归档可成为后续受控迁移的数据源，不能只依赖下载器当前文件列表。部分资源重放不能抹去同任务的正片整理历史。字幕/音轨对视频的依赖也应包含在计划中，视频目标冲突时阻止发布孤立的 sidecar。

### 新事件与媒体服务器职责边界

`library.files_committed` 应包含实际媒体库路径及媒体身份，供媒体服务器刷新、NFO、通知使用。不要把下载目录路径复用为媒体库路径。事件至少一次投递，带稳定 operation_id，订阅者幂等。

扫描请求成功 ≠ 识别成功。由独立媒体服务器插件提供可选 `library.verify` 按媒体库路径、作品 provider id、季集号对账，包含 Jellyfin MediaSources 多版本路径；不自动覆盖用户元数据。附加资源保存率和可播放媒体识别率分别报告。

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

AB 内置 media-server-refresh 使用旧 `X-Emby-Token`。Jellyfin 12 默认禁用旧认证，现有有效 API Key 也会返回 401；0.6.0 整理插件曾使用 `Authorization: MediaBrowser Token="..."`；0.7.0 已移除整个媒体服务器 API 集成，改为文件驱动。后续上游 PR 应同步修正内置刷新插件并增加拒绝旧认证头的回归测试，不要求用户重新生成密钥或开启旧认证。参考 [Jellyfin 12 发布说明](https://jellyfin.org/posts/jellyfin-release-12.0/)。

## 文件驱动音乐关联（0.7.0）

整理器输出媒体文件和自有 `collection.xml` 清单，不依赖 Jellyfin URL、API Key 或 item ID。通用 `collection_path_map(local, remote)` 仅翻译 XML 的文件系统引用，容器路径相同时不配置；媒体服务器的库创建、扫描、验证由管理员或独立集成处理。上游文件提交事件可通知已有刷新插件，刷新插件持有权限，不应倒置到整理器。Music、video 与 collections 输出根目录独立；增量合集只纳入已成功发布且所有权验证通过的 Series、Movie 和 MusicAlbum 目录，跨 entry 可聚合到同一作品，但每次文件整理仍保持 entry 隔离。作品身份继承 AB 或已有 TMDB 覆盖，不以种子／组定义合集；不引入外部资源搜索或系列关系推断。历史成功记录可单独重建清单，不重新计划媒体操作；中断时的所有权意图不能冒充已发布成果。

目标 Jellyfin 12.4 的实机约束：`LockData=true` 会阻止后续本地元数据导入，所以生成清单使用 false。独立的生成合集库须将 `MetadataSavers=[]`，避免服务器回写清单。当前 BoxSet 元数据合并对外部清单成员取并集；清单新增成员可导入，但仅从 XML 删除成员不保证从 Jellyfin 数据库移除。插件不为绕过此限制请求媒体服务器 API；如用户要清空或重建合集，交由 Jellyfin 管理。

Jellyfin 12.4 实机确认：生成清单用独立 Mixed 辅助索引库扫描，并从用户首页隐藏该辅助入口；用户从 Music 库自身的「合集」页浏览。Boxsets 页面展示全局合集；Mixed 文件夹页筛掉 BoxSet，Mixed 合集页又按成员所属库筛选（并非按清单文件路径），因此无法作为单独音乐合集入口。Music 库的合集查询能返回其专辑所属的外部清单集合。此设置属于媒体服务器部署说明，不引入插件的媒体服务器 API 权限。

0.7.1 新增 entry 级 `scope=all|resources`，资源范围限制优先于任务覆盖，保存于插件独立绑定中，不修改 AB 原生季度或下载路径。上游详情扩展 API 应允许这一类插件特定的逐番策略，并在自动扫描时读取同一有效配置。

### Shadow DOM 与弹窗焦点（0.7.2 兼容层）

AB 4.0 的 Naive UI / Vueuc 焦点陷阱以 `mainEl.contains(getPreciseEventTarget(event))` 判断边界；插件双层 Shadow DOM 中的输入不满足 Node.contains，导致焦点立即跳回弹窗标签。上游应按 `event.composedPath().includes(mainEl)`（并正确处理 Tab 边界）判断，不应全局关闭焦点陷阱。插件临时仅对自身详情面板的 focus 事件，把 composedPath 的起点重定位到 AB 的 light-DOM slot；外部焦点事件不变，卸载时移除监听。参考 [Vueuc FocusTrap](https://github.com/07akioni/vueuc/blob/main/src/focus-trap/src/index.ts)。

## 原生默认行为与可选纠错（0.8.0）

完成任务只要有明确、未删除的 AB 归属，默认使用其 official_title、year、episode_type、season、season_offset 和 episode_offset；不再以 TMDB 绑定作为准入条件。AB 4.0 Bangumi 没有持久 TMDB ID，不能要求插件为了接替原生功能再让用户逐番搜索。显式 TMDB 覆盖仍有最高优先级，原有输出布局保持；切回 AB 模式只需保存空 ID，无需清理历史数据库或修改下载器。电影默认选体积最大的唯一正片候选；大小相同保持待确认。无法解析的多集单文件、小数集号和冲突仍待人工确认。

正式 metadata API 应提供上述 AB 字段与 revision、有效季号计算、已确认的可选 provider IDs、文件解析结果。TMDB enrichment 是可选 provider，网络失败不能阻断其他 AB entry。原生重命名/硬链是自动完成默认；替换 organizer 同样必须默认处理未来新番，人工映射只作覆盖。有效设置优先级为任务覆盖 > 单番覆盖 > AB 元数据；单番 resources 范围始终约束任务覆盖。

自动整理与手动补链须区分：已提交成功后用户删除的目标不自动重建；只写了发布意图而尚未成功的文件仍需恢复。当前插件以独立 SQLite 成功记录判断，不依赖 qB 标签。未来事件应有 restore_missing 语义，回放完成事件保持原生的不重建行为。自动开关升级后保持原值，开启时历史未绑定任务也会进入整理范围；维护页须明确提示这一点。

## 原生文件迁移与启用审阅（0.10.0）

建议正式 `ctx.library.legacy_outputs(source_identity)` 返回原生布局规则、源／目标 fingerprint 与成功记录，读操作不得泄露其他插件任意 KV。当前兼容层只读取 hardlink 插件 `link:` 记录及其 source_root/library_root；不写原生 KV。插件按明确路径规则和源身份确认候选，原生记录缺失时才做完整内容核验，多个候选留待确认，不以文件名／集号证明版本一致。接管在用户确认的单次计划中执行，先记迁移所有权意图，再在同一媒体库文件系统中发布名称、校验和清理旧路径；禁止“复用”失败后悄悄复制。

自动开启 API 需要完整自动范围预览 token；关闭无需 token。全量表格包含不变、复用、接管改名、新建、跳过、待确认。token 校验任务、设置和绑定快照并单次消费，开启前执行确认计划；失败保持关闭。后续新下载自动增量，不能把首次历史接管隐藏在开关副作用里。

0.10.2 的迁移计划增加明确的 work_root 范围；原生精确路径／记录未命中时，只在当前作品目录内查找视频和已绑定 sidecar 的同扩展名、同大小候选，最终以源身份或完整 SHA-256 证明内容。手工季号修正不会因文件名变化变成重复复制。大小／mtime／季集相同不足以接管；多个有效候选留待确认，其他文件键的已拥有目标排除，符号链接目录不遍历，核验 I/O 失败保持待确认。未来正式 legacy_outputs 应允许按作品范围和文件身份发现已改名副本，避免插件读取整个库或猜测文件名。

0.10.3 将媒体库内迁移改为原子、不覆盖目标的移动；迁移意图仍须先持久提交，重启后按文件身份恢复，不依赖旧路径仍存在。应提供 `move_no_replace` 和受控的空目录清理接口，后者只接受已迁移旧文件的父目录链，止于输出根目录，并保护下载根目录及其他配置根目录；不得用递归删除实现。文件已成功迁出、空目录因权限不能移除时，不将媒体文件整理标记为失败。

0.10.4 增加外部移动后的音乐根目录记录更新：配置新根目录、暂停整理后，以成功历史中的源身份和完整 SHA-256 核验每个新文件；全批通过后备份并原子更新插件归属、目标 fingerprint、历史和合集引用，源下载 fingerprint 不变，媒体文件零写入。未来原生维护 API 应把这种“文件已由管理员移动，仅更新记录”的操作与移动文件的整理计划分开，提供完整预览、单次 token、配置与记录快照核验，以及提交后的清单更新重试；不能只改路径配置后把已有音乐当成新文件复制。
