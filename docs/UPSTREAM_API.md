# AB 插件 API 需求

状态：**提案，尚未提交上游 PR**。建议接口名用于说明契约，不代表 AB 已有能力。基线为 [AB 4.0.0 / bc23984](https://github.com/EstrellaXD/Auto_Bangumi/tree/bc23984ae4a6d388c0b8381e48f6aa9b60e338ba)；提交前须按目标分支复核。

目标是让[订阅审核](https://github.com/gregwym/autobangumi-subscription-review)和[媒体库整理](https://github.com/gregwym/autobangumi-library-organizer)移除内部接口适配，同时保留原生默认行为。版本策略、音乐分类、合集 XML 和文件执行账本仍由插件维护，不要求并入宿主。

## 现有接口的缺口

| AB 4.0 接口 | 可用能力 | 缺口 |
| --- | --- | --- |
| `DOWNLOADER` provider | 实现下载器客户端 | 无供其他插件使用的稳定任务查询、持久关联与提交服务 |
| `torrent.filter` / `torrent.adding` | 过滤、改写添加请求 | 过滤异常时放行；无法表达持久待审核，也无法阻断全部添加入口 |
| `METADATA_PROVIDER` | 新规则入库前补全元数据 | 无已有条目的完整季集快照、修订号及按 ID 查询服务 |
| `RENAME_STRATEGY` / `MEDIA_FILES` | 生成下载文件名、分类 | 不能替换完整整理流程，也不能独占其文件操作范围 |
| `torrent.organized` | 宿主整理后通知 | 路径属于下载器，不能代表媒体库已发布；无完成任务历史查询 |
| UI slots / `API_ROUTER` | 全局设置、单番页和鉴权路由 | 缺少带修订号的条目上下文；Shadow DOM 输入有焦点兼容问题 |
| `kv` / `data_dir` | 插件私有持久化 | 无跨后台任务的维护协调及用户指定本地来源更新服务 |

依据：[SDK context](https://github.com/EstrellaXD/Auto_Bangumi/blob/bc23984ae4a6d388c0b8381e48f6aa9b60e338ba/backend/src/ab_sdk/context.py)、[ingest](https://github.com/EstrellaXD/Auto_Bangumi/blob/bc23984ae4a6d388c0b8381e48f6aa9b60e338ba/backend/src/ab_sdk/ingest.py)、[events](https://github.com/EstrellaXD/Auto_Bangumi/blob/bc23984ae4a6d388c0b8381e48f6aa9b60e338ba/backend/src/ab_sdk/events.py)、[过滤失败策略](https://github.com/EstrellaXD/Auto_Bangumi/blob/bc23984ae4a6d388c0b8381e48f6aa9b60e338ba/backend/src/module/plugin/host.py)。

## P0：下载任务与归属

建议由 `ctx.downloads` 提供以下服务；不向调用插件暴露下载器凭据。

```python
async def list_tasks(*, bangumi_id=None, completed=None, cursor=None) -> TaskPage: ...
async def files(download_id) -> FileSnapshot: ...
async def submit(request, *, idempotency_key, policy_revision) -> Submission: ...
async def lookup_submission(idempotency_key) -> Submission | None: ...
```

| 对象 | 必要字段 |
| --- | --- |
| `DownloadTask` | 稳定 `download_id`、`downloader_id`、`provider_task_id`、`bangumi_id`、`release_id`、状态及 revision |
| `DownloadFile` | 稳定 `file_id`、相对路径、下载器路径、AB 本地映射路径、大小、选中状态、完成度及 snapshot revision |
| `Submission` | 提交键、策略 revision、`pending / accepted / rejected / unknown`、下载关联和可解释原因 |

- 关联和提交意图先落库，再请求下载器；网络超时进入 `unknown`，先对账再重试。不能假设下载器原生支持幂等键。
- 同一远端任务跨重启身份不变；替换 provider 不改变下载器实例 ID。RSS URL 可含令牌，不作为公开主键。
- 未关联和多重归属显式返回；不能根据标题、媒体库身份或 qB 标签猜所属条目。人工关联也不能覆盖明确的现有归属。
- 查询支持分页、按条目过滤及启动补查；完成事件只用于唤醒，不能代替查询。任务内文件可分别完成。
- 将关联从 `ab:<id>`、完成状态从 `ab:renamed` 迁到宿主持久记录。旧标签仅作经核验的迁移提示，用户自有标签保留。

**验收：**两个条目绑定同一作品时，单番查询不读取另一条目的文件；覆盖新任务、文件延迟完成、宿主记录延迟出现、重复事件和提交结果未知后的恢复。

## P0：统一下载准入与候选

新增强制 `download.preflight`，放在所有下载器添加动作之前，覆盖 RSS、搜索、手动添加、补集和重试。输入包含来源、release、条目快照、集数身份与策略 revision；返回 `allow / hold / reject` 和原因。

`hold` 是可恢复的待审核，不是已下载或永久排除。启用强制准入的范围须持久记录；所选插件缺失、禁用、异常或熔断时，该范围保持阻断。现有普通过滤钩子的默认失败策略不变，未启用审核的安装不受影响。

候选接口需满足：

- 整批发布先持久化，再做 v1/v2 决策；版本维度与修订号分别保留。
- 返回候选匹配、排除阶段和证据；未知字幕组可展示，同分跨季匹配保持歧义。
- 过滤变化可重评已保存的未下载候选，批准后可通过正式提交服务重投；放宽过滤本身不批准旧资源。
- 审核确认绑定候选与规则快照，过期拒绝；重投仍经过全部准入及去重，不能凭旧许可直接调用下载器。

候选不要求无限保留：宿主声明保留范围／期限；清理不得丢失未决提交和已经承诺保留的审核记录。版本选择 UI、缺集策略及修订优先级仍由插件实现。

**验收：**关闭或卸载审核插件不能绕过阻断；同批 v2 在 v1 之后到达仍跳过 v1；取消过滤不会自动补下历史；未知提交不会被另一版本抢占。

## P0：整理器选择与媒体库事件

增加 `ORGANIZER` provider 和持久选择，由宿主在原生改名之前交出任务。原生整理器仍为默认 provider，未选择插件的用户保持原行为。首个实现只需全局独占；按下载器或条目分范围接管可后续扩展。

- 一个范围只能有一个整理器。切换前停止旧整理任务、备份配置并确认选择；插件不可用时暂停，不自动退回源文件重命名。
- 整理器可查询全部已完成任务及后续增量，无需订阅 `torrent.organized` 才开始工作。
- 默认使用 AB 标题、年份、类型、季和偏移；无需逐番 TMDB 绑定。全局规则与逐番覆盖共用有效配置。
- 插件提交成功结果后，由宿主发布 `library.files_committed(operation_id, download_id, bangumi_id, files, media_identity, mapping_revision)`。`files` 是实际媒体库路径及类型，不是下载路径。
- 事件至少一次投递，`operation_id` 稳定；NFO、通知和媒体服务器刷新插件自行去重。失败、待确认及仅写入执行意图的文件不能发布成功事件。

文件执行账本可以继续保存在插件中，但须满足：源／目标身份分开；发布前记录意图；不覆盖非自有文件；硬链、Reflink、复制分别记录；权限、磁盘满和 I/O 错误不能伪装成“不支持”而降级。恢复未完成发布与补回用户主动删除的成功目标须区分。

**验收：**接管后宿主不再改源文件；重复扫描不重复复制；单番整理不触碰其他条目；后续新增单集及字幕可增量发布，原生通知／刷新可订阅新事件。

## P1：元数据与变更

建议 `ctx.bangumi.get(id)` 返回完整只读快照：标题、年份、类型、原季、季偏移、集偏移、有效季号、可选 provider IDs 与 revision。发布 `bangumi.mapping_changed(old, new, revision)`；对历史集号保留原始值和映射来源，避免二次偏移。

TMDB 查询复用宿主的凭据、代理和缓存，提供 search/get/episodes；保留 tv/movie 类型，查询失败不阻断其他条目。TMDB 是可选纠错源，不是整理准入条件。

逐任务和逐文件覆盖由插件保存，引用稳定文件 ID 和映射 revision；只对对应文件生效。单番「仅资源」范围必须约束任务覆盖，不能被其重新放大。

**验收：**修改偏移后候选与去重键可重算，旧批准过期；原始集号不丢失；已改名文件不重复加偏移；新番默认跟随 AB 自动整理。

## P1：维护、历史与旧输出

建议 `ctx.maintenance.run(scope, operation)` 协调停任务、锁、备份、进度、恢复；不持 SQLite 写事务等待网络或大文件核验。操作使用具名契约，不开放任意 SQL 或其他插件的 KV。

| 操作 | 契约 |
| --- | --- |
| 历史清理 | 预览精确集数与影响规则；一次性 token 绑定 revision；协调宿主历史和插件去重；不删除媒体或下载器任务；未知提交不可清除 |
| `legacy_outputs(source_identity, work_scope)` | 返回原生输出布局、成功记录及源／目标身份；无记录时可在明确作品范围核验内容，不盲扫整个库 |
| 开启自动整理 | 完整范围预览后才执行并开启；显示不变、复用、接管、新建、跳过、待确认；执行失败保持关闭 |
| 媒体库内改名 | 核验已有副本后不覆盖地原子移动；失败保留原文件，不静默降级成额外复制；仅清理旧父目录链的空目录 |
| 外部移动后更新记录 | 暂停整理，完整内容核验新位置，整批更新路径与目标身份，源身份保留；不移动或复制媒体，提交后可重试更新合集清单 |

跨宿主与插件数据库的维护需记录步骤及恢复状态，不假设多个 SQLite 文件天然共享事务。预览过期、配置改变、源或目标被替换均拒绝执行；批次核验失败不提交部分路径更新。

**验收：**覆盖原生命名策略、同大小但不同内容、多个候选、权限不足、用户替换目标、跨挂载目录迁移及崩溃恢复；不会把不可访问文件当作不存在重新复制。

## P1：本地来源更新

建议宿主统一提供 `plugins.updates.check/install`。用户显式选择未签名本地来源后，自动检查、单击安装；官方市场继续使用原签名策略。

契约包括固定来源及插件身份、版本／SDK 兼容性、校验和、ZIP 路径校验、更新锁、后台任务协调、代码备份和重载失败回滚。数据迁移须声明备份和恢复办法；不能以代码回滚暗示数据已回滚，也不全局绕过验签。

**验收：**错误 ID、越界路径、校验失败和不兼容版本不安装；同时点击只执行一次；新代码加载失败恢复旧代码；断电后有可恢复的旧代码和数据备份。

## 可独立提交的修复

- `SCHEDULED_TASK` 的 provider ID 按插件隔离，调度标识使用 `<plugin_id>/<provider_id>`。验收两个插件使用同名任务，以及单独重载／卸载互不影响。
- 弹窗焦点边界正确处理 Shadow DOM 的 `composedPath()`，同时保留 Tab 围栏和弹窗外焦点约束；覆盖键盘与嵌套组件输入。无需全局禁用焦点陷阱。

## PR 拆分与迁移

| 顺序 | 范围 | 依赖 |
| --- | --- | --- |
| 1 | 定时任务作用域、Shadow DOM 焦点修复，各自独立 PR | 无 |
| 2 | 稳定下载关联、只读任务／文件服务 | 无；原生流程保持默认 |
| 3 | 幂等提交、统一准入、候选与重投 | 任务关联 |
| 4 | 整理器选择、条目快照、媒体库提交事件 | 任务查询；元数据字段可分 PR |
| 5 | 维护协调、历史清理、原生输出迁移 | 关联及 revision 契约 |
| 6 | 本地来源更新服务 | 维护与插件生命周期 |

每个 PR 包含 SDK 类型、宿主实现、契约测试、兼容说明及最小示例，不合入这两个插件的完整 UI。文件移动和音乐规则等可先留在插件，只在存在通用需求时上移。

接口可用后，订阅审核逐项移除 `review_gate/bridge40.py` 中的下载门面、RSS 过滤和匹配包装；整理器移除 `library_organizer/bridge40.py` 中的改名守卫、配置接管、私有下载查询、TMDB 和原生 KV 读取。两个插件再将维护与更新交给宿主。

现有审批、候选、归属、文件所有权及未决投递必须迁移保留。通过断言插件不再导入 `module.*`、无运行时包装，并同时运行原生及两插件集成测试，作为移除兼容层的完成条件。
