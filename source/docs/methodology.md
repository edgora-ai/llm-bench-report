# 一次会话模型能力基准 · standard-v1

## 目标与样本

本基准衡量固定工具环境下，一次全新 coding-agent 会话自主完成离线动画页面的表现。一个样本是「日期批次 × 工具 × 模型路由 × 任务 × 新会话」。每个模型每任务一次；模型可在同一会话内自主迭代，不设置费用、token 或轮次预算，不追加人工指导、不由评测器修复产物。

不同工具的内部系统提示、上下文管理和工具实现不同，跨工具结果包含执行器影响。模型别名不保证后端权重版本固定；未知权重版本明确留空。单样本只支持观察，不能证明统计意义上的退化。

## 固定任务

任务定义在 `bench/tasks/*.json`，逐字保留原始提示词，并保存 `standard-v1` 完整提示词。两个任务分别是「鳄鱼骑自行车 SVG 动画」和「黑洞模拟动画页面」。统一入口 `index.html`，自包含、离线运行，不搜索外部资料、不调用图片生成。黑洞是视觉模拟任务，不隐含科学级相对论求解要求，不限定 SVG/Canvas/WebGL 实现算法。

新增任务或优化提示词必须新建版本；不得覆盖已使用的版本。正式批次记录完整任务快照、提示词 SHA-256、评分版本和运行条件指纹。不要把提示词变化解释为模型变化。

## 工具与无污染协议

`isolated-agent-v1` 允许文件读写、终端和预置 Python Playwright/Chromium 自主测试。禁止子代理/其他模型、Web 搜索/抓取、图片生成、外部 MCP、用户技能/记忆/插件继承。当前主模型不能借默认 Haiku/Opus 路由代做。禁止恢复旧会话或连接共享 OpenCode server。`isolated-agent-v2-effort` 的工具、权限和隔离与 v1 完全相同，只新增推理强度条件（见下节）；v1 样本不与 v2 样本直接排名。

bubblewrap 网络 namespace 在本机两次失败（`Failed RTM_NEWADDR: Operation not permitted`），因此使用真正的 Docker 容器：

- 每次独立工作目录、PID namespace、HOME、缓存与客户端配置。
- `--network none`、只读根文件系统、非 root 用户、capabilities 全部移除、no-new-privileges。
- 容器不挂载宿主 HOME、其他结果、看板数据库、Docker socket 或真实 provider 认证。
- 仅本次可写工作区、只读 CLI/浏览器运行时、冻结模型目录、推理桥接 Unix socket可见。
- 宿主受信协调器持有真实认证，容器仅持有无外网效力的本地占位认证。
- 推理通道只允许当前路由和登记的 Messages（含同模型 count_tokens）/Chat Completions/Responses 端点；Claude 端点允许登记的 `beta=true` 查询，不允许通用代理、搜索/图像 provider 工具及辅助模型。
- 代理保持原 OpenCode 包装器选择的出口语义；密钥不在命令参数、归档或看板中出现。

CLI 版本、镜像摘要、实际权限配置、客户端适配器与运行策略 SHA-256 都构成条件证据。连接身份只记录脱敏指纹，不包含认证、URL用户信息或查询值；运行器源码版本亦记录在条件中。批次冻结执行策略源文件，后续样本发现源码变化会阻塞，不能悄悄混入一批。冻结范围是 `bench/**/*.py`、`runtime/launch.py`、`runtime/evaluate_worker.py`、`docs/methodology.md`、`containers/runtime.Dockerfile`；`tests/` 不在其中，因为测试不参与执行。阻塞是设计行为而非故障：2026-09-30 的批次在 7/26 时因 `bench/runner.py` 与本文件在运行期间被改动而停止，剩余样本未跑。已封存样本的归档完整无损，队列记录可从 worker 事件流恢复；恢复运行应起**新批次**（它会按当时的源码重新冻结），而不是设法绕过守卫 —— 绕过守卫等于让不同代码版本的样本混进同一条件组。preflight 核对容器内 launcher/evaluator 与宿主源码哈希一致，漏重建镜像会阻止启动。容器内部 Claude 二次 bwrap sandbox 关闭，以避免 namespace/依赖冲突；这不关闭外层容器隔离，也不使用 permission bypass。

`preflight` 在真实容器中验证外部文件不可读、认证变量不继承、工作区写入与网络不可达。只存在 CLI 二进制或切换 cwd 不视为隔离成立。任何隔离失败阻塞运行，不静默降级。

## 批次执行

先刷新模型目录到本项目独立 HOME/缓存，不改变用户全局配置。free 资格来自 input/output/cache 价格元数据，不仅依据模型名称。缓存候选与实际调用成功分别记录；模型下线、限流、认证/基础设施失败不从清单中消失。

冒烟采用每种工具一个模型、两个任务，共四个新会话。冒烟结果与正式比较分开；基础设施失败的第一次尝试也保留，重试不覆盖。正式批次采用保存随机种子的交错顺序，队列顺序与串行批次可比。

**并发生成 + 串行评估**（2026-09-30 起，取代纯串行）。`--concurrency N` 只并发模型会话，评估始终串行：实测 4 路并发生成时 4 核负载仅 0.42、单容器 8–25% CPU，因为 CPU 密集的只有两段——模型用预装浏览器自测、评估用软件 WebGL 渲染，后者串行化以免互相污染截图时序。并发不压缩单次墙钟：串行基线 `gpt-6-sol` 单次 19.9 分钟，4 路并发下单次 2.7–8.9 分钟，总墙钟从 4×单次降到约 9 分钟，吞吐约 2 倍。generation duration_ms 从 worker 准备完毕、启动 CLI 前计时，不含宿主排队与后续评估；并发改变资源竞争，跨批次比较耗时时仍须核对 concurrency 字段。

**批次清单的并发写入必须加锁**（2026-09-30 实测事故）。并发下每个完成的会话都要更新同一份 `batch.json`（追加 `run_ids`、回填队列槽位、追加重试项）。无锁时两个线程读到同一份旧数据、各自写回，后写者覆盖先写者：实测 26 样本批次在 7 个样本后崩溃，`run_ids` 只剩 3 个，4 个已完整封存的样本从清单中消失，而它们的归档仍在盘上——样本存在却无法被批次找到、重跑或汇报。`write_json` 的临时文件名也必须按写入者唯一：固定 `.tmp` 会让一个线程把文件移走而另一个线程的 `replace()` 失去源，留下损坏的 JSON。现由 `tests/test_batch_concurrency.py` 驱动真实 run_batch 覆盖容量、worker异常与兄弟成功、冻结变化、取消及评估异常收尾，而不是复制实现的测试。提交前持久化 run id，完成按 id 幂等收录；单个 future 异常不能丢掉其他已启动任务的结果。清单丢失以 run id 定位磁盘归档、核对哈希并与 worker 事件交叉验证，恢复项标记来源，不得假装队列本来就正确。冻结策略改变时停止新提交；如果评估策略也改变，不用新策略默默评估旧批次，保留待恢复状态。

不设置会话生成时限；网络建立和外部评估有技术超时，和模型迭代预算分开。评估超时按渲染成本而非模型预算设定：软件 WebGL 下单次 CDP 截图实测可达约 166s，四次截图加录像与移动端会超过旧的 180s 上限，旧上限会静默销毁强产物的全部证据；现为 1800s 基础设施余量，评估发生在会话结束之后。操作员 Ctrl-C 或进程终止必须标为 interrupted/blocked，不宣称模型正常完成。CLI 退出正常只说明会话执行正常，不等于页面符合要求。

**恢复批次必须逐条指定队列**（2026-10-01 实测）。CLI 原本只支持 `--model` 与 `--task`，二者相乘得到笛卡尔积：7 个已跑样本的批次要恢复 19 个未跑样本，按模型分组启动会展开成 38 项，其中一半是**已经跑过的组合**。基准的样本单位是"每模型每任务一次会话"，多跑计划外样本不是浪费，是污染 —— 排名里会多出重复样本，而"每模型每任务一次"的声明当场失效。因此增加 `--pair TOOL,MODEL,TASK`（可重复），给出时队列即逐条取用，不做展开；与 `--model`/`--tool`/`--task` 互斥，格式错误与未知工具在启动前报 usage 错误而不是让 worker 抛出 traceback。恢复批次本身是**新批次**，按当时源码重新冻结，不能续接原批次记录。

**长会话与停滞**。不设会话时限意味着一个模型可以持续打磨到自行结束，这是要测的行为而不是缺陷：实测 `gpt-6.1-sol` 做黑洞题运行 56 分钟仍未结束，其间 34 次 Edit、13 次 Read、12 次 Bash，产出桌面/平板/第二版共 3 张自截图，按视口逐个验证 —— 属于系统性自测，不是空转。因此**不设自动终止**：任何基于时长或重复模式的自动中断都会把"迭代更久"这一被测变量本身剪掉。

代价是批次可能被一个不收敛的会话挂住。操作员用 `bench.cli stop <job-id>` 取消；先保存 cancel.json，核对 PID 身份再发 SIGINT，仅停止该 job 批次登记的 run id 对应容器，不停止其他 job。协调器在 executor 等待退出前处理取消，记录 interrupted 并收录/封存已产出的证据。判断是否停滞看三样东西，而不是看时长：CLI 事件流是否仍在增长、输出目录 mtime 是否仍在更新、进程 CPU 时间是否仍在累积。三者都停才是停滞，此时应 `stop` 并按 `interrupted` 归档重跑。

**传输中断重试**。推理流中途断开（`stream disconnected before completion`、`ECONNRESET`、`socket hang up`、`premature close`）归类为 `transport_interrupted` —— 是连接断了，不是模型答不出。这类样本自动重试**一次**，作为**新样本**入队（原队列末尾，不打乱已冻结顺序），并在批次清单里记录 `retry_of` 与 `retry_reason`；失败的那次保留自己的完整归档，不被重试覆盖，也不冒充首次尝试。限流、认证失败、模型不存在、以及模型自己交白卷都不重试 —— 那些是真实结果或真实不可用，重试只会掩盖。

重试逻辑曾有三类事故。**队列项是三元组不是映射**：并发路径曾写成 `dict(queue[index])`，第一次重试就抛 `dictionary update sequence element #0 has length 11`，原归档留下但重试没有启动。**最终排空后必须继续提交**：Python 的 list iterator（包括 enumerate）能看到尚未耗尽时的 append，并不是快照。历史漏跑发生在提交循环结束后才处理最后一批完成结果，此时追加的重试没有下一轮提交。**重试本身不得再重试**：旧代码只检查当前槽位的 retried，而新 child 没有该标记，连续断流会无限追加。现用 attempt=0/1，只有首次传输中断允许追加一次；child 的 retry_of 固定指向首次 run，父槽位用 retry_run_id 单独记录 child，不写自指 lineage。`tests/test_batch_retry.py` 与真实调度器测试覆盖连续两次断流只启动两次会话、final drain 重试确实提交、非传输失败不重试。

2026-10-08 的跨批次恢复使用可重复 `--retry-run RUN_ID`，与精确 `--pair` 合并；父归档的文件哈希、任务版本、错误及真实 child/活跃领取必须先验证。历史 retried=True 只代表意图，不能替代 child 会话证据。恢复会话直接为 attempt=1，不再自动重试。新 manifest 首次创建即带 attempt/retry_of/retry_reason，旧归档不回写。Astra 明确加入两题新首次会话，最高目录档为 max（CLI 参数级证明，不保证隐藏后端遵循），不永久扩大默认发现队列；GLM 仍跳过。504 保留现有分类，不自动扩大传输重试协议。

## 推理强度

推理强度是基准条件，不是可调参数。早期样本从未设置强度，因此测到的是各家默认档位之间的差异，而不是模型能力差异；这 17 份既有归档一律保留原样，标记为强度未记录，不回填、不改写。

档位按模型自身目录声明解析，规则写在 `bench/effort.py`：

- 目标恒为该模型可用的最高档。目录里最高档名不同（`xhigh` / `max` / `high`），按固定阶梯 `none < minimal < low < medium < high < xhigh < max` 取模型实际声明中的最大值，不按声明顺序也不按字符串比较。
- 模型档位阶梯低于最高档时下调到它自己的上限，并记录 `downgraded_from`。
- Claude Code 传 `--effort`，OpenCode 传 `--variant`（其 help 文本写作 "model variant (provider-specific reasoning effort)"）。preflight 每次从镜像内两个 CLI 的 help 实际探测这两个标志是否存在，缺失即阻塞，不靠假设。opencode 的 help 走 stderr 而非 stdout，探测必须合并两个流。
- 目录只声明模型会推理、但不给出档位阶梯时，记为 `fixed_unspecified_effort`：强度由模型内部固定，如实记录，不假装可控。
- 目录中没有该模型 ID 时记为 `not_in_catalogue`。Claude Code 侧的 `kimi-k3-256k`、`deepseek-flash` 是网关别名，与 OpenCode 目录中的 `kimi-k3`、`deepseek-v4-flash` 只是名称相近，不做前缀猜测。目录不可读记为 `catalogue_unreadable`，ID 歧义记为 `ambiguous_in_catalogue`。
- `config/bench.toml` 的 `runtime.effort` 可设为 `off`，此时记为 `disabled_by_operator`，仍然不落回各家默认值。

每份归档记录四个字段：`reasoning_effort_requested`（请求的最高档）、`reasoning_effort_applied`（实际传入的档位，不可控时为 `null`）、`reasoning_effort_supported`（模型声明的完整阶梯）、`reasoning_effort_control`（控制状态）。这四个字段进入条件指纹，因此不同档位的运行不会被看板合并为同一条件组。

跨日期比较的前提是四字段一致。强度不可控的模型可以横向观察其固定档位下的表现，但不得与强度可控的模型直接排名。

## 计量

所有缺失数值为 `null`，界面显示「未知」，不补零。

- 墙钟会话耗时由外层单调时钟测量，包括 CLI/容器启动；API 时间单独保存。
- 首个模型事件时间是可观测指标，不称精确 TTFT；准备、队列和外部验收与会话时间分开。
- Claude 最后 result 是累计权威值，不再叠加 assistant usage/modelUsage。
- Claude 非缓存输入、cache-read/cache-write 分开；总输入包含缓存，reasoning 不二次叠加输出。
- OpenCode 去重每个 step_finish 的计量；其归一化 input 已排除缓存，input_total 加回 cache。优先 provider `tokens.total`，reasoning 不再次加入。
- 保存完整原始 usage 和计量语义；未知或部分 usage 不冒充完整 totals。
- CLI `total_cost_usd`/step cost 标为 `cli_reported_unverified`。特别是自定义路由的 CLI 价格不一定可信；即使报告 0，也不声称账单为 0。
- OpenCode目录零价证明免费标价，不等同于账单核验。未来若导入 provider 账单/价格快照，应保存来源、币种和版本并单独展示，不改原始报告值。
- 汇总显示已知费用小计及覆盖率；不同计量来源的费用不能冒称统一实际账单。
- `source_bytes` / `source_file_count` 统计输出目录中的 HTML/SVG/CSS/JS 实际字节及文件数，排除截图、录像和过程日志。它们仅辅助观察产物变化，不表示越大越好，也不自动判定缩水；旧样本缺失这些指标保留未知，不修改最终归档。

## 自动验收与视觉评分

模型会话结束、容器退出后才运行外部评估。评估结果不回灌模型，不进行自动补修。评估浏览器没有 provider socket、密钥或看板会话，使用 `--network none`；仅本次静态服务可用。CSP与请求拦截是附加防线，不单独充当网络隔离。

客观检查：入口存在、HTTP 加载、脚本错误、外部请求、SVG要求、固定观察窗口帧变化、400px溢出。保存桌面/移动截图、多个时点截图、约8秒动画录像、console与检查JSON。帧变化仅表示观察到动态，不能证明运动正确或画面美观。软件 WebGL 和浏览器版本记录在报告中，性能不是 GPU 通用基准。

已实测的渲染环境限制（2026-09-30，`llm-bench-runtime:v2`，Chromium 151.0.7922.34 + SwiftShader）：页面自身渲染很快（实测 requestAnimationFrame 间隔中位数约 19ms），但 `Page.screenshot` 对持续重绘的 WebGL 画布可能不返回——同一次运行中 30s 与 120s 两次调用都在“taking page screenshot”后超时，CDP `Page.captureScreenshot` 反而能完成，代价是 1440×900 约 130s。已捕获的两个 Claude 产物不含 WebGL（0 处）因而正常截图，其中一个评估仅 21.5s。结论：截图失败不能直接推断页面有缺陷，也不能推断模型能力差；重 WebGL 产物正是强模型的典型输出，评估器不得系统性无法记录它。修复此问题后必须重跑受影响样本的外部评估（评估是会话后独立步骤，可对既有归档重算，不修改模型产物与生成记录），并在 `conditions`/评估报告中记录实际使用的捕获路径与耗时。

评估器基础设施故障与产物缺陷严格分开：评估进程崩溃、超时或未产出报告时，状态记为 `infrastructure_error`，不得折算成模型的产物失败或质量差；看板也不得把只剩 `provider_route` 的收缩检查集显示为「全部通过」。

**HTTP403 不等于违反隔离路由**（2026-10-08）。Astra/Luna 的黑洞样本同模型 `count_tokens?beta=true` 请求已在冻结网关允许范围内，事件中的上游阶段字段证明403来自供应商；旧验收把任意403计作受限路由拒绝，属于本地归因错误，不能据此推断模型调用了辅助模型。新网关事件记录 `status_origin`（upstream/local_policy/local_gateway）、`policy_rejected` 与固定非敏感 `reason_code`，不记录请求正文或认证。provider_route 仅对确证的本地策略拒绝记 fail；上游403原样转发并另计于检查说明，路由通过不代表供应商可用或会话/产物成功。旧事件的 request_id 键（即使null）及 read_timeout_override 只在已核对的冻结日志实现中用于证明上游来源；未知历史实现、来源不明的403保留 unknown。本次修复不重评或改写旧56份归档，不伪造 token count，不扩大已用完的重试预算；上游拒绝、过载和语义断流仍是未解决的外部限制。

**重评估可以更新评估；未完成的生成记录不是结果**（2026-09-30 实测矛盾并修正，2026-10-01 再修正）。评估在会话之后运行，评估器自身会失败（实测 `Page.screenshot` 30 秒超时、错误字段为空），修好后必须能对既有归档重算。索引层原本对已封存 run 一律拒绝覆盖，结果重评估成功后归档是对的（`evaluation.json` 与 5 份证据都在），数据库却停在旧的 `infrastructure_error`，`rebuild` 也以 `skipped` 跳过 —— 数据库是归档的镜像，这个状态下二者永久不一致，任何从库读取的地方都会认为该样本未评估，且不会自愈。

第一次修正误把运行占位当成不可变事实：`running` 的空 metrics/null 耗时被真实结果填充时触发 RunConflict，5 个已落盘样本没有记入批次。随后又把全部 metrics 冻结，阻止评估添加 source_bytes/source_file_count；仅保护 final→final 则留下 generated 篡改与 final→generated 洗白漏洞。2026-10-08 修正为明确状态及字段归属：queued/running 的生成占位允许完成；从 generated 起，生成错误/分类、退出码、usage、非派生 metrics、耗时、CLI 命令、模型记录和生成结束时间冻结。身份、完整 conditions、prompt hash 和重试 lineage 从创建起固定。评估白名单只更新 evaluation/checks、评估时间、派生归档状态和证据；metrics 仅允许 source_bytes/source_file_count，归档故障写 archive_error，不改生成 error。generated→终态必须匹配 generation_status；终态回退或改判拒绝。

生成结束建立不可变 generation_artifacts 哈希基线，浏览器评估前与封存时均验证；追加 evidence 与评估日志不能重定义原输出/过程文件。generation_finished_at/finished_at 保持生成结束，evaluation_finished_at 单独记录评估结束。重评重建 checks，不保留旧 pass 或重复 provider_route。预先校验状态/事实再替换 disk manifest，SQLite 写入在事务中再次验证；磁盘权威仍可通过 rebuild 与空库灾备逐条恢复。旧归档不迁移回写。

崩溃时已封存但未记入清单的样本，从 `worker.jsonl` 的 `run_start` 与归档目录回填（逐条按 run id 定位，不按日期或顺序猜），回填项标记 `recovered_from`，不假装队列本来就正确。

人工 rubric 六个维度各0–4：任务符合度、主体/场景辨识、动作/运动连贯、视觉细节、可用性/响应式、交付完整性。0=缺失或严重不符合，1=局部/严重问题，2=基本可用但明显缺陷，3=完整且少量缺陷，4=优秀且完整。各任务维度说明见任务JSON。没有评分就是待评；代码行数/字节数不自动转换质量分，不自动生成 LLM 评委评分。

评分记录姓名或评价者标识、理由、盲评标记、rubric与证据。盲评隐藏模型/日期/成本并随机排序；若产物自带模型署名可能解盲，保留原始文件并记录疑似解盲，不改造原始产物。模型生成产物不在看板主站执行；默认展示隔离录制的视频/截图，原始HTML/SVG作附件下载。

## 归档与重建

`runs/<UTC日期>/<run-id>/` 保存 prompt、任务快照、stdout/stderr、gateway元事件、最终回复、输出目录、验收报告和证据、manifest与checksums。每次运行UUID独立。最终记录不可覆盖；评分作为 `reviews/*.json` 追加，不修改生成证据。

正式批次在 `data/batches/<id>/protocol/` 留存当时的执行器/适配器/隔离器/评估器源码、方法文档与 Dockerfile，并在批次 preflight 中保存逐文件哈希。没有复制用户认证、全局设置或原始环境变量，协议快照也不挂载到模型容器。镜像按摘要引用；若要迁移机器做逐位复现，备份时另行 `docker save` 保留对应镜像，不把浮动基础镜像重建当作完全相同环境。

SQLite 是可重建的查询索引，不是唯一证据来源。完整JSON与列索引共存，直接SQL可核对；WAL/事务保证并发读写。`verify` 校验实际文件哈希；`reindex` 从归档合并恢复记录和评分，可在空新数据库做灾备演练。非法符号链接、特殊文件或校验失败明确标为归档异常。

日志落盘对已知密钥脱敏，不保存环境变量全集；「完整过程」指可获得的客户端事件，不承诺 CLI未输出的内部思考或逐字编辑历史。不要对外发布原始私有日志。

## 可比性与后续日期

同条件比较使用任务、提示词、rubric、工具、权限、CLI/镜像版本和隔离策略指纹。条件变化产生趋势断点，缺失数据不连线/补零。正式批次与冒烟、fixture永不混榜。未来相同日期或不同日期均可执行同一CLI，新run不覆盖旧run；日期按UTC归档并保存带时区时间。

公开报告以脱敏自包含合并快照发布：index.html 是最新全量，purpose=benchmark 默认排除冒烟，date_from/date_to 筛选真实样本日期。历史日期命名 HTML 实际为合并副本，不冒称日期专属或不可变日快照；后续发布保留其旧字节，README 说明语义。公开计量保留来源与未知值、attempt/retry_of，原始日志、私有配置及密钥不发布。14 条既有非盲 AI 建议评分仍待操作员复核，不回填新样本质量分。

本实现不依赖仅在当前Claude会话存在的定时任务。要持续每日运行，用主机服务/cron调用明确命令并单独保存调度日志；没有确认可靠调度前不声称已建立长期定时任务。
