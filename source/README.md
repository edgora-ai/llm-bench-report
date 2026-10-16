# LLM Bench · 一次会话实验台

固定任务、全新隔离会话、不可覆盖归档、按日期与模型比较的中文看板。比较的是「模型路由 + coding-agent 工具 + 固定环境」，不是纯API聊天能力。

## 已定义任务

- `crocodile`：自行车与骑车鳄鱼，原创 SVG 动画，禁止搜索和图片生成。
- `blackhole`：离线黑洞模拟动画页面，允许 SVG / Canvas / WebGL。

提示词、原始需求和六维 rubric 位于 `bench/tasks/*.json`，方法在 [docs/methodology.md](docs/methodology.md)。优化提示词必须新增版本，旧结果不覆盖。

## 源码与复现前置条件

公开报告位于 [GitHub Pages](https://edgora-ai.github.io/llm-bench-report/)，项目源码位于报告仓库的 `source/`。v2报告另外公开经过逐项审核的原作入口和必要依赖，存放在非执行JSON包中。公开仓库不含完整运行归档、数据库、认证、真实provider配置或CLI二进制；不能仅靠公开快照恢复完整原始实验。

需要 Linux、Python ≥3.11、Docker、Node.js（脚本语法验证）及 ffmpeg（截图/视频导出测试）。从仓库根目录进入 `source/`；使用源码目录或 editable installation，当前wheel不保证打包任务、网页及runtime资源。

```bash
cp config/bench.example.toml config/bench.toml
python3 -m pip install -e '.[dev]'
python3 -m unittest discover -s tests -p 'test_*.py'
```

example仅是配置模板，执行真实模型前必须填写本机路径并准备私有provider settings、模型目录与CLI运行时。`bench/egress.py` 当前要求可读的私有Claude settings文件，OpenCode认证与出口按本机设置解析；example将这些配置放入忽略的 `config/`。直接出口可在本地 `config/opencode-egress.conf` 写入 `direct`。不要提交这些文件。

当前 `discover --refresh` 仍使用实现中的固定OpenCode安装路径；迁移机器需要核对并调整该发现入口，不假装开箱即用。固定镜像依赖版本见Dockerfile；要复现实验条件，应核对CLI版本、源码哈希、镜像摘要和归档中的条件指纹，而不是只重建同名标签。无本机模型目录或原始归档时，依赖真实数据的集成测试会明确skip；临时fixture单元测试不需真实provider认证。

## 本机使用

从本项目源码目录执行（本机配置与隔离镜像准备完成后）：

```bash
python3 -m bench.cli tasks
python3 -m bench.cli discover --refresh
python3 -m bench.cli preflight
python3 -m bench.cli serve
```

监听地址与端口位于 `config/bench.toml`，也可用 `BENCH_HOST` / `BENCH_PORT` 覆盖。默认仅本机。首页/health公开；结果、附件和评分API需要登录。登录密钥保存在 `data/dashboard-token`（0600），可在本机查看：

```bash
python3 -m bench.cli token
```

不要将该值、provider配置或原始日志发布到共享渠道。浏览器登录使用 HttpOnly session cookie，不在localStorage保存token。

## 执行真实批次

```bash
# 两种工具各一个模型，两个任务，共4个独立冒烟
python3 -m bench.cli run --purpose smoke --seed 20260930

# 指定单模型/单任务，不续接旧会话
python3 -m bench.cli run --purpose benchmark \
  --tool claude-code --model gpt-6.1-sol --task crocodile

# 默认队列以发现规则和目录为准；先核对 discover 输出，不假定所有别名均启用
python3 -m bench.cli run --purpose benchmark --seed 20260930
```

Claude Code可请求路由（不等于默认队列或已验证可用）：`gpt-6.1-sol`、`gpt-6-sol`、`gpt-6-astra`、`gpt-6-luna`、`deepseek-flash`、`glm-5.3-flash`、`kimi-k3-256k`。free目录通过元数据识别，包含名称不带free但价格为0的路由。不自动替换模型，服务不可用和限流保留结果。

长批次可以作为独立后台进程运行，避免终端／当前助手会话结束成为人工时间上限。它只执行本次有限清单，完成后退出，不自动重复收费测试：

```bash
python3 -m bench.cli start --purpose benchmark --seed 20260930
python3 -m bench.cli jobs <上一步返回的job-id>
python3 -m bench.cli stop <job-id>  # 先保存取消意图，再发出SIGINT，仅停止该job容器
```

状态与私有日志在 `data/jobs/<job-id>/`。退出本终端不会取消后台批次；需要终止时显式使用stop。主机重启或SIGKILL无法保证正常封存，应检查running记录与日志，不把它们当作完成。

每个样本使用新容器、独立工作区/HOME/配置/会话和无外网network namespace；真实认证留在受信宿主推理通道。允许模型在会话内使用终端和预置浏览器自测；禁止辅助模型、搜索、图片生成及全局记忆/插件。没有费用/token/迭代轮数上限。Ctrl-C会终止当前容器，保留中断证据，不追加“请修复”提示。

## 结果与看板

- `runs/<UTC日期>/<UUID>/`：任务/prompt、客户端事件、最终回复、原始输出、SHA-256、截图、录像和检查报告。
- `data/bench.sqlite3`：索引，可由归档重建。
- `data/batches/<批次>/batch.json`：完整计划清单、顺序随机种子、环境与目录快照。
- 看板：同题作品对比、结果矩阵、成本与耗时、日期趋势、完整记录；本地看板保留盲评分，公开页面只读。

默认比较 `benchmark`，冒烟和测试fixture不混榜。先在“用途”选择smoke可查看环境联调尝试。**CLI正常退出不等于任务完成**；自动检查与视觉评分分开，未评分显示待评。视频是会话结束后在隔离浏览器录制的画面，不是实时原作。评估器自身超时或崩溃记为 `infrastructure_error`，不折算成产物失败；检查集缺失时看板不显示为「全部通过」。本地认证看板保持HTML/SVG附件下载及原有安全头，实时预览通过导出的v2报告使用。

`artifacts/observatory.html`（或明确指定的版本文件）是自包含、无API、不可写入的脱敏快照。GitHub Pages使用 `progressive-static-v3` 作品画廊：首屏内联当前样本的精简索引和画廊程序，桌面先读两张缩略图、手机一张。任务、用途、模型名称查找和放大截图不下载完整实验台；近可见卡片并行请求已审核原作包与SRI可信运行器；运行器加载后有界读取响应、完整校验并隔离启动，也可用卡片按钮暂停或继续。完整记录、复杂筛选、成本、矩阵和高级比较由明确操作一次装载经过大小及SHA-256校验的完整v2文档。`offline.html` 保留字节不变的单文件完整版。两版run数据与完整媒体派生字节一致，缩略图是额外派生，不修改归档。公开媒体经过压缩，部分录像裁掉无内容边缘，不等于档案原始像素。

在线浏览和分享请使用轻量首页；按日期分享用首页筛选链接（例如 `?date_from=2026-10-08&date_to=2026-10-08`），不要把约41MB的日期存档或离线文件当在线入口。静态报告复用筛选和证据的派生数据，收起的无媒体历史在展开时创建卡片；手机媒体对比只装载当前A/B列的大图，另一列切换后再读取，所有尝试和证据仍保留。

上一轮（2026-10-09）同条件 Chromium 新旧v2对照：真实gzip HTTP、冷缓存、每组3次取中位数。首屏DOM节点814→590；1.6Mbps／150ms／4倍CPU节流下，桌面和手机搜索处理至绘制分别60.7→47.3ms、64.4→49.3ms，另有两版共同的250ms输入防抖。普通网络筛选约37–40ms，没有稳定提速；首页可交互时间在各组约0.30–1.43s，没有稳定的大幅改善。本地节流测试不包含用户到GitHub的真实网络等待，不代表其实际加载时间。可用 `verify_public_snapshot.py --performance-only --baseline-site ... --baseline-viewer-script ...` 复测，旧查看器须独立保存。

### 历史作品画廊实测（2026-10-09）

以已部署的 `9ca0695` 为基线，Chromium、真实gzip level 6 HTTP，桌面1440×900／手机400×850，每组新旧交替3次冷缓存取中位数。首页解压后590,974→101,589 bytes，gzip 113,907→25,596 bytes；含首屏缩略图的传输量，桌面147,783→59,472 bytes，手机131,300→42,989 bytes；首屏DOM节点590→226。

| 网络与设备 | 第一张真实作品可见：旧→新 | 画廊内容与基础控件就绪：旧→新 | 实际任务切换至绘制：旧→新 |
| --- | --- | --- | --- |
| 20Mbps／40ms，桌面 | 262.0→136.5ms | 176.4→104.4ms | 62.7→46.6ms |
| 20Mbps／40ms，手机 | 265.2→126.5ms | 179.9→99.9ms | 62.8→46.9ms |
| 1.6Mbps／150ms／4倍CPU，桌面 | 1438.0→732.6ms | 1050.3→417.5ms | 168.5→87.6ms |
| 1.6Mbps／150ms／4倍CPU，手机 | 1336.0→648.6ms | 1022.1→408.0ms | 165.9→86.0ms |

按需装载有额外等待：明确点击原作至运行器装载，普通桌面／手机165.5／151.5→210.6／214.7ms，慢速946.1／877.0→1171.0／1171.9ms。明确进入完整查看器至就绪，普通桌面／手机82.1／65.1→691.1／283.0ms，慢速339.6／320.7→1224.2／1217.2ms；旧版此前已在首页下载并初始化完整查看器。第一次立即进入高级记录的路径因此可能更慢，不能称所有路径均提速。基础筛选与截图放大不产生此请求，单作也不先加载完整版。原作装载时间不是画面运动证明；另以真实Canvas像素变化验证了运动和桌面／手机视口。

测量中的 `interactive_ms` 含人为等待network-idle及后续任务点击，不作为最早可交互时间或正式TTI。以上为本地节流对照，不代表用户到GitHub的真实路由／TTFB。双作独立控件、RAF-SVG属性推进／暂停、CSS动画推进／暂停及手机A/B生命周期通过；本环境双作像素截图超时，未宣称双作像素验证通过，也未重复35原作×3浏览器矩阵。

公开作品卡的“运行原作”会在校验包及逐文件SHA-256后启动真实HTML/SVG/Canvas/WebGL。原件字节保持不变；执行文档只增加隔离前缀，并将已登记本地JS/CSS引用适配为携带原字节的data URL，保留脚本顺序和defer。截图、录像、实时原作是独立模式；原作缺文件或报错不会被补齐、改写成成功或偷偷替换成录像。失败但有交付的attempt同样保留。

完整查看器中的单作预览及两份原作比较均需明确启动；手机仅运行可见列，关闭或切换销毁实例。轻量画廊另有近可见卡片自动预览，离屏、筛选或分页时释放上下文。原作在双层不透明源沙箱中执行，不进入主站DOM，不获得报告存储、认证或任意网络代理；下载、全屏和部分浏览器功能受限。浏览器策略不等于容器级断网，不能保证WebRTC/DNS等所有通道或硬CPU/内存/GPU配额。运行环境是访问者的浏览器，实际视口和缩放单独显示，不保证与历史评估像素、性能或动画进度一致。“已装载”也不是新的验收结论。

作品按任务、模型/工具及具体run展示；重复与失败记录不被“最好结果”替换。比较默认同题同Prompt，不同或未知执行条件必须明确标注。截图阶段不是精确动画时间，录像联动只是从各自录制起点重播。结果矩阵区分会话结论、入口检查、评估及证据收录；入口检查缺失保持unknown，证据登记不等于加载成功。生成会话耗时不包含排队与后评估，评估耗时缺失时不反推。

成本均带来源。`cli_reported_unverified` 不是供应商账单，自定义模型价格尤其需要核对。未知值显示未知，费用汇总显示已知小计与覆盖率，不补0、不编造质量分。后台模型权重版本未知时不能仅凭名称证明未变化。

推理强度是基准条件。`config/bench.toml` 的 `runtime.effort = "maximum"` 让每条路线按自身目录声明的最高档运行（`max` / `xhigh` / `high` 各自不同），并记录请求值、实际值、完整阶梯和控制状态。目录无档位阶梯的模型记为强度不可控，不假装可控。`claude --effort` 与 `opencode run --variant` 的存在性每次 preflight 从镜像内实际探测，缺失即阻塞。设置 `effort = "off"` 会被记录，不会落回各家默认值。早期未记录推理强度的归档保留未知，不回填。

## 验证、导出与恢复

```bash
python3 -m unittest discover -s tests -p 'test_*.py' -v
node --check web/app.js
python3 -m bench.cli verify
PYTHONPATH=. python3 tests/verify_archives.py  # 真实SQL/API/查询/临时库灾备核对，需看板服务运行
python3 -m bench.cli reindex
python3 tests/dashboard_e2e.py --help
```

看板可按当前筛选导出CSV/JSON。数据库重建从 `runs/*/*/manifest.json` 和追加 `reviews/*.json` 恢复；`verify` 对比磁盘真实文件与归档哈希。备份应同时包含 runs 和 data/batches，不只备份数据库。最终产物不可覆盖；后来评分只追加记录。

## 重建隔离镜像

准备本机已授权的CLI二进制（不要复制用户配置）：

```bash
mkdir -p containers/assets
cp "$CLAUDE_BINARY" containers/assets/claude
cp "$OPENCODE_BINARY" containers/assets/opencode
docker build --no-cache -f containers/runtime.Dockerfile -t llm-bench-runtime:v4 .
python3 -m bench.cli preflight
```

构建阶段可安装固定版本的Playwright及系统库；模型生成阶段禁止联网安装依赖。CLI版本与最终镜像digest写入每个运行条件。不要在运行批次期间重建同一镜像标签；运行器会检查镜像是否改变并阻止继续。

本机 bubblewrap 由于网络namespace权限失败而切换Docker，实际隔离验证保存在 `data/preflight.json`。若容器无法运行，不降级为仅切换cwd。Docker运行时只有当前工作区、冻结模型目录和本次socket挂载，未挂载Docker socket或宿主HOME。

## 公开发布

模型与外部评估全部结束、归档核验通过后，再从认证本地看板生成公开快照。`BENCH_DASHBOARD_URL` 应为实际配置的服务地址；密钥由快照脚本读取本机文件，不放入发布参数。

```bash
python3 tests/make_snapshot.py artifacts/share.html "$BENCH_DASHBOARD_URL"
cp web/app.js artifacts/share.viewer.js
python3 tests/make_site.py --help
python3 tests/make_site.py artifacts/share.html artifacts/site --input-viewer artifacts/share.viewer.js
python3 tests/publish.py artifacts/site --include-source --dry-run
python3 tests/publish.py artifacts/site --include-source
```

`--input-viewer` 必须对应输入快照的可信发布版本；新查看器由当前 `web/` 构建。复用已有快照时不用再次收集API数据、录制或重编码视频。构建器拒绝覆盖内容不同的非空目录，改版使用新的输出目录。已有单文件发布命令仍适用于旧式仓库；已切换多文件的仓库必须传入验证后的site目录，不能用单文件覆盖入口并留下过期资源清单。

如需加入原作，在持有归档的机器上逐项完整审阅入口及必要依赖，另存绑定实际SHA-256的审核JSON后升级已有site；不能把自动扫描无命中直接写成审核通过。审核记录标明真实审阅者，本次工具支持的`reviewer: assistant`不代表用户逐项审阅。

```bash
python3 tests/make_site.py artifacts/site artifacts/site-originals \
  --upgrade-site --input-viewer artifacts/share.viewer.js \
  --archive-root runs --audit artifacts/original-review.json
python3 tests/publish.py artifacts/site-originals --include-source --dry-run
python3 tests/publish.py artifacts/site-originals --include-source
```

已经完整验收的v2包可无损派生新画廊，不再次读取归档或重建原作：

```bash
python3 tests/make_gallery.py --help
python3 tests/make_gallery.py artifacts/site-originals artifacts/site-gallery \
  --input-viewer artifacts/share.viewer.js --input-runtime artifacts/share.preview-runtime.js
python3 tests/verify_public_gallery.py --help
# 单独执行 --audit-only、--performance-only、--ux-only、--original-smoke；
# 独立保存旧site和匹配的可信web/app.js、preview-runtime.js及基线锁。
python3 tests/publish.py artifacts/site-gallery --include-source --dry-run
```

公开画廊保留评分优先卡片与可见卡片内联原作预览；仅运行已审核且可用的原作，离屏、筛选或分页时释放原上下文。「运行原作」用于暂停/继续卡片预览，「放大原作」打开隔离预览弹窗；不可运行的原作仅在有桌面截图时提供截图放大。已装载的原作发生运行错误时，显示有界纯文本诊断、销毁沙箱并回退静态图，保留手动重试；不修改模型输出，也不把错误改记为通过。验收分别记录缩略图首屏、自动原作启动及显式放大/完整查看器阶段，自动预览请求与传输量计入结果，不冒充截图-only冷启动。初始原作包只能来自当前近可见卡片（桌面至多2份、手机至多1份），不提前加载完整查看器或录像。候选源码锁须包含当前 `web/preview-runtime.js` 及验收器定义的完整候选源码集（含修复包校验依赖）；历史基线仍用其冻结SDK独立校验，不为候选改写历史信任锚。

### 修复版动态预览（非原始交付）

对确有源码但因交付缺陷无法运行的作品，可另行生成明确标注的派生预览；它不是模型原作，不用于评分，也不改变历史评审。当前仅登记两条固定配方：Nemotron 3 Ultra 黑洞只修正阻止启动的单个常量声明；CatJam 鳄鱼固定提取首份完整 HTML，移除其后的围栏、评论和重复文档，不择优、不重新绘制。黑洞的单声明修复在真实 Chromium 中仍触发 `createRadialGradient` 非有限数值异常，当前审核为 `withhold`，不纳入候选；不会扩大补丁去重写粒子逻辑。当前候选仅纳入通过动态验收的 CatJam 鳄鱼。没有交付入口的运行不会生成修复作品。

`tests/make_repairs.py --help` 提供待审派生文件生成入口。配方绑定 run ID、归档原始入口 SHA-256、精确操作和结果 SHA-256；归档注册/校验和不符立即拒绝。派生文件必须单独全文审阅并记录审核，不继承原作审核，也不把自动扫描无命中当作审核通过。审核后才能构建新的候选画廊：

```bash
python3 tests/make_gallery.py artifacts/site-originals artifacts/site-gallery-repairs \
  --input-viewer artifacts/share.viewer.js --input-runtime artifacts/share.preview-runtime.js \
  --archive-root runs --repair-audit artifacts/repair-review.json
```

有已审核修复包的卡片默认显示“修复版，非原始交付”，可切换原始交付或历史截图，放大弹窗同样标明版本与修改内容。完整版/离线版仍只包含原始交付，不含派生修复版。修复资源采用内容寻址 JSON、v3 独立 `repair` 角色及严格的来源绑定，复用原有不联网、双层 opaque sandbox；原作与派生包不以同源裸 HTML 发布。原作描述符、完整 v2 查看器、离线文件、媒体和评分保持原字节；校验器仅排除独立验证过的修复资源后，继续精确对照冻结 v2 基线。候选源码锁须包含修复构建模块，历史信任锚不改写。生成候选不等于发布，上线仍须单独授权和验证。

完整文档和SDK在 `viewer/<sha256>.html`／`.js`。这些路径是画廊校验后加载的资源，不是可直接导航的报告入口；请分享根首页。旧v1/v2校验分支仍独立严格，v3的离线文件仍使用既有v2合同。公开验收脚本不重建候选；使用真实gzip HTTP，不拦截网络、替换API或把空壳FCP当成作品可见。画廊浏览器验收使用 Playwright 的完整 Chromium 新无头模式（`channel='chromium'`），回执记录浏览器版本；旧 `headless_shell` 在复杂实时原作下可能卡住原生交互，不能以强制点击或放宽时限绕过。额外测试依赖安装到独立临时环境，不修改冻结镜像。验收另用不暂停目标的原生 CDP 观察异常：只有原生上下文、双层沙箱所属 run 和独立校验的原作脚本源码都匹配时，才将模型异常单列为原作证据；所有 Playwright 原始异常及原生堆栈仍保留。可信界面、SDK 及无法可靠归属的异常仍使验收失败，不按 `about:srcdoc` 字样宽泛过滤。画廊测试 HTTP 服务按已实测的 Pages 文本资源传输策略，对支持 gzip 的请求以 level 6 压缩 HTML、JS 和 JSON；媒体保持原传输，候选与基线操作服务器使用同一策略，统计实际编码后传输量而非清单中的解码大小。

审核JSON结构为`{version: 1, reviewer: "assistant", files: {"run-id/index.html": {sha256, decision: "include"或"withhold", reviewed_full: true, reason}}}`，文件名相对于各自output目录。只为确实全文审阅且摘要一致的文件登记`reviewed_full`；缺失审核的原作保留未审核状态，不公开。缺依赖是交付完整性问题，不自动等于隐私拒绝。升级v2输入时另传与输入匹配的`--input-runtime`；公开源码副本没有私有归档或该审核文件，不能凭空恢复这一步。

`.report-manifest.json` 记录当前报告的精确文件/hash/MIME/大小。`media/` 只放内容寻址的栅格图和录像，v2的`originals/`只放已审核原作的内容寻址JSON；原作源码不作为同源裸HTML发布。`.report-assets.json` 是追加式资源所有权账本，v1仅管理媒体，v2无损继承旧媒体并追加原作包，v3继续保留全部旧资源并追加两个可信 `viewer/` 资源。修改或缺失的已管理文件、未知媒体/原作/查看器或符号链接均阻止发布，不自动接管、删除或覆盖；禁止版本降级。已有日期HTML保持原字节，不会自动获得实时预览；最新首页按日期筛选可使用新能力。新日期副本来自自包含offline文件，不来自轻量index；日期文件仍是历史合并副本，不是假装的单日数据集。

当前发布验收器支持 `python3 tests/verify_public_snapshot.py --help` 和 `--site artifacts/site --output /tmp/report-check`；需要原生Python Playwright与已安装的Chromium，可使用现有隔离runtime镜像执行。它先检查冷缓存首屏请求预算，再进行全图片解码及指定录像播放，两阶段分别统计。历史对照可显式传入 `--baseline-snapshot` 与对应 `--baseline-viewer-script`，不把不同时代的查看器混为一谈。

实时原作另由 `python3 tests/verify_original_previews.py --help` 所示入口验收，逐份记录来源校验、实际运动、控件操作、双作比较及离线结果。独立隔离检查用 `python3 tests/verify_preview_isolation.py --help`，通过可达canary和被动观测验证浏览器策略，不用断网、请求abort或替换API替产品阻断。静态报告的流量检查不能代替隔离证明。额外浏览器在独立测试环境安装，不重建或修改已冻结的评测镜像；实际未覆盖的浏览器和功能须明确列出。

发布器只向已存在且有权限的仓库提交；clone失败不会新建仓库。它在临时clone的发布分支提交，再fast-forward推送指定分支，不使用force。Pages根目录是报告，`source/` 是白名单源码。`source/.source-manifest.json` 只记录源码文件哈希，不与报告资源清单混用，不含私有运行数据。源码目录有未知文件或已管理文件被另外修改时停止，不静默覆盖。

自动传输重试最多一次，原失败不覆盖。操作员要求再次补跑时，使用明确的新批次、新会话，并在本机保存人工请求和旧/新run来源；不能把已用完的retry lineage续成无限重试，也不能把人工重复样本冒充首次单样本比较。

## 后续日期

以后再次运行同一命令会生成新批次和UUID，不覆盖旧数据。改工具、提示词、评分或镜像需新的条件版本；日期趋势中指纹变化显示断点。单次样本的下降只作观察，不能直接断言“降智”。本项目没有伪装成持久化的会话内定时任务；需要每天运行时，可用主机cron/systemd显式调度该CLI并留存日志。
