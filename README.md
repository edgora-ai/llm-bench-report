# 模型能力基准 · 一次会话评测

固定任务、固定隔离环境下的模型一次会话完成度对比。2026-11-02 新增 opencode/beam-free 两题样本：推理经记录在案的算子网关路由（batch route_override，非官方 zen/inference 路由），条件与既有 opencode free 样本不同，不直接排名；opencode-go/iceman-contributor-free 因本机无 OpenCode Go 凭据未运行。新增样本视觉评分待人工复核。

2026-11-09 Crouton Alpha Free：新增四条真实尝试，未挑选最佳结果。Claude Code + 既有 gateway 的鳄鱼、黑洞两题成功；OpenCode 官方 inference 入口的两题均因 HTTP 429 / FreeUsageLimitError 未交付，失败记录同样保留。两条路线和条件不同，不作等条件排名。命令分别显式指定 --effort max / --variant max；条件中的 applied=max 表示目录解析和 CLI 档位选择，不是后端独立确认，实际推理档位及底层权重仍未知。生成耗时包含模型自检，CLI 费用未经账单核验。补充浏览器验证发现：鳄鱼暂停仅停止骑行，云朵仍移动；黑洞在 400×800 手机视口中，控制面板遮挡黑洞主体，虽然无横向溢出检查通过。黑洞是艺术动画，未验证物理准确性。原作未修改；四条新记录均无正式视觉评分。

2026-11-10 发布 GLM-5.3-Flash（样本日期 2026-11-09）：通过远端 macOS 的既有 ZCode 0.17.1 app-server 通道生成，新增两份成功原作及两条生成前基础设施启动失败，保留 attempt / retry_of 关系，不挑选最佳结果。首次失败由 Unix socket 路径过长引起，尚未开始模型调用，不计为模型质量失败。鳄鱼、黑洞生成会话分别为 26分1.743秒、36分4.527秒，包含模型自检，不是纯 API 推理耗时；期间未中断、未追加修复提示，原作未由评测员修改。两题均显式设置 max，并有 session/setModel 回执；这只证明会话配置，不证明后端实际推理强度或底层权重。每题 fresh HOME / 工作目录 / 会话及 macOS 文件沙箱，不是 network-none，仍有共享临时目录；与既有本地断网 Docker 生成条件不同，不作等条件排名。本地派生评估在断网 Docker 中运行；桌面和手机原作交互另行实测通过，含暂停、重置及本地 PNG 导出，公开预览自身仍受下载等沙箱限制。未发现额外模型或联网搜索调用，但 CLI disallowlist 并非已证实全部强制生效：黑洞曾使用 TaskOutput 读取本地截图命令的输出，不是子代理；冻结 conditions 中 removed 的措辞表示意图，实际强制限制未完全验证。provider-tagged input_tokens 已包含 cache_read_tokens，不能按查看器通用提示重复相加；reasoningTokens 原始零值与观察到的 reasoning 流冲突，因此归一化推理 token 数及费用均保留未知，不补零。黑洞是模拟展示，未认证物理准确性；软件渲染下的帧率不代表真实 GPU 性能。四条新记录无正式视觉评分。原始运行日志、凭据、provider 配置和私有主机信息不公开。

2026-11-12/11-13 补发两轮此前只做内部分析、未公开的样本，共 20 条，与本次新样本一起全量重建报告。(a) deepseek-flash 官方路由 vs 网关路由对照 6 条：2 条被宿主 supervisor 终止的 interrupted 尝试（基础设施中断，不计为模型失败），4 条 completed 分别覆盖官方与网关路由；同题同 prompt、effort=max、concurrency=1，只换 provider 路由，并各自与 2026-10-17 官方期归档对照。结论见本地分析记录：没有证据表明网关路由换成了更弱的模型，两题均完成且自动检查全过；但网关路由明显更慢、token 消耗更高，黑洞题成品视觉完成度略低且出现持续 WebGL 告警。单日单样本、条件漂移，不作等条件排名。(b) 免费/订阅通道 14 条：beam-free 2 条 completed；iceman-contributor-free 2 条 completed、4 条 session_error 空交付；opencode-free（网关别名，上游 longcat-2.5-free）1 条 completed、5 条失败（4 条 session_error、1 条 rate_limit 凭据冷却）。失败尝试原样保留、不挑选最好结果；免费标价 0 不等于账单。两轮与既有 claude-code / opencode 样本条件不同，不合并排名。新增 20 条均无正式视觉评分。

2026-11-18 发布网关新增路由 gpt-6.2-astra / gpt-6.2-sol：新增 4 条样本（2 模型 × 2 题），standard-v1 任务与 prompt、effort=max、claude-code + 断网 Docker 条件。四条约会均正常完成（28.9–41.9 分钟、52–62 轮，含模型自检；鳄鱼 8/8、黑洞 7/7 自动检查通过，无 console 错误）。模型目录刷新后两条新路由声明 low..max，实际传参 max 并记录 declared——只证明目录声明与 CLI 参数，不证明隐藏后端遵循或权重版本；backend_weights_version 仍未知。CLI stderr 的 unrecognized_model 与 kimi/deepseek 历史样本相同，不代表路由不可用；两次 astra 会话各有 1 次 count_tokens 上游 403，未影响交付。成本：这 4 条在报告中的费用改为按社区模型价格 API（models.dev/api.json，抓取于 2026-11-18，sha256 cdd7faff…）价格重算，费用来源标注「models.dev 价格快照 2026-11-18 重算（按归档 token；非账单）」；目录价 astra 为 input $8 / output $40 / cache read $0.4，sol 为 $2 / $10 / $0.1（每百万 token），归档 token 的单请求 context（38k–64k）未触发 272k 阶梯。重算结果 astra 鳄鱼 $4.7335、黑洞 $4.2520，sol 鳄鱼 $0.8555、黑洞 $1.2310；不是供应商账单。归档的 CLI 自报值对两个未知别名使用了 $4/$20 回退价（costBasis=unknown，sol 高估约 2×、astra 低估约 2×），因此不再当作已核实成本展示；其余历史样本费用保留归档来源，本次未重算。视觉评分：4 条均为 AI 建议六维分（astra 鳄鱼 24/24、sol 鳄鱼 23/24、astra 黑洞 24/24、sol 黑洞 24/24），非盲、待操作员复核；单样本不作统计结论。

本次为全量重建：报告包含本地已封存的 149 条公开记录（126 条 benchmark + 23 条 smoke），排除 4 条生成前基础设施失败的记录（与既有过滤规则一致），证据、已审核原作包与历史记录按既有规则保留。为不超过 GitHub Pages 1 GB 站点上限，本次不新增日期命名副本（既有日期文件保留原字节、未重写）；按日期筛选请用首页链接。原始运行日志、凭据、provider 配置与私有主机信息不公开。

2026-11-18 发布后修正：公开画廊「模型 / 来源」导航按展示名去重——GLM-5.3-Flash 与 glm-5.3-flash（大小写别名）、crouton-alpha-free 与 opencode/crouton-alpha-free（同一模型的两条路由）各合并为一个入口，合并入口的 title 显示路由数；运行记录、评分、证据、费用与既有日期文件均未改动。完整记录里的原始路由别名仍分开列示，用于精确筛选。

本仓库的报告由 `llm-bench` 发布步骤自动生成，报告为只读脱敏合并快照，不含凭据、原始日志或本机路径。源码与运行配置分开，私有配置、二进制和完整运行归档不上传。
index.html 随发布更新。日期命名文件是首次创建时的合并数据副本，不是该日期专属数据；旧版文件保留原字节，不重新包装成日快照。

在线首页按需加载截图、录像及已审核的原作入口与运行依赖。可见卡片自动在不透明源沙箱中运行已审核原作；不是视频回放。“运行原作”暂停/继续内联预览，“放大原作”打开隔离预览弹窗。原文件字节不变，执行文档经过隔离与资源装载适配。沙箱限制下载、存储及常规外联，不提供容器级断网或资源配额。

[下载离线完整版](offline.html)（自包含，首次下载较大）

[项目源码与复现说明](source/README.md)

[同题作品对比](index.html) · [含冒烟的全部尝试](index.html?purpose=)

按样本日期筛选最新合并数据：
- [2026-09-30](index.html?purpose=benchmark&date_from=2026-09-30&date_to=2026-09-30)
- [2026-10-01](index.html?purpose=benchmark&date_from=2026-10-01&date_to=2026-10-01)
- [2026-10-08](index.html?purpose=benchmark&date_from=2026-10-08&date_to=2026-10-08)
- [2026-10-09](index.html?purpose=benchmark&date_from=2026-10-09&date_to=2026-10-09)
- [2026-10-10](index.html?purpose=benchmark&date_from=2026-10-10&date_to=2026-10-10)
- [2026-10-11](index.html?purpose=benchmark&date_from=2026-10-11&date_to=2026-10-11)
- [2026-10-12](index.html?purpose=benchmark&date_from=2026-10-12&date_to=2026-10-12)
- [2026-10-13](index.html?purpose=benchmark&date_from=2026-10-13&date_to=2026-10-13)
- [2026-10-14](index.html?purpose=benchmark&date_from=2026-10-14&date_to=2026-10-14)
- [2026-10-16](index.html?purpose=benchmark&date_from=2026-10-16&date_to=2026-10-16)
- [2026-10-17](index.html?purpose=benchmark&date_from=2026-10-17&date_to=2026-10-17)
- [2026-10-30](index.html?purpose=benchmark&date_from=2026-10-30&date_to=2026-10-30)
- [2026-11-02](index.html?purpose=benchmark&date_from=2026-11-02&date_to=2026-11-02)
- [2026-11-09](index.html?purpose=benchmark&date_from=2026-11-09&date_to=2026-11-09)
- [2026-11-12](index.html?purpose=benchmark&date_from=2026-11-12&date_to=2026-11-12)
- [2026-11-13](index.html?purpose=benchmark&date_from=2026-11-13&date_to=2026-11-13)
- [2026-11-18](index.html?purpose=benchmark&date_from=2026-11-18&date_to=2026-11-18)

## 结果解释

会话正常结束、页面交付、自动检查与人工视觉评分分别展示。CLI费用未经供应商账单核验，未知计量不补零。既有14条非盲AI建议评分仍待人工复核。

画廊默认仅展示每个（工具，模型，任务）三元组的最新一次；点击详情进入完整实验台查看历史对照。全部尝试与正式样本计数见首页。

第二轮补齐样本：1 个组合新交付；5 个组合已尝试仍失败（多为 session_error/length 空交付）；1 个组合条件阻塞（step-5 黑洞）。失败样本保留，不被覆盖，也不重跑。

重新发布请在 `source/` 内执行 `python3 tests/publish.py <site-directory> --include-source`。
