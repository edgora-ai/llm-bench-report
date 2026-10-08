# LLM Bench · 一次会话实验台

固定任务、全新隔离会话、不可覆盖归档、按日期与模型比较的中文看板。比较的是「模型路由 + coding-agent 工具 + 固定环境」，不是纯API聊天能力。

## 已定义任务

- `crocodile`：自行车与骑车鳄鱼，原创 SVG 动画，禁止搜索和图片生成。
- `blackhole`：离线黑洞模拟动画页面，允许 SVG / Canvas / WebGL。

提示词、原始需求和六维 rubric 位于 `bench/tasks/*.json`，方法在 [docs/methodology.md](docs/methodology.md)。优化提示词必须新增版本，旧结果不覆盖。

## 源码与复现前置条件

公开报告位于 [GitHub Pages](https://edgora-ai.github.io/llm-bench-report/)，源码位于报告仓库的 `source/`。公开仓库不含运行归档、数据库、认证、真实provider配置或CLI二进制；不能仅靠公开快照恢复完整原始实验。

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

默认比较 `benchmark`，冒烟和测试fixture不混榜。先在“用途”选择smoke可查看环境联调尝试。**CLI正常退出不等于任务完成**；自动检查与视觉评分分开，未评分显示待评。视频是会话结束后在离线隔离浏览器录制的原始画面，页面不会在看板主站执行；HTML/SVG作为下载附件。评估器自身超时或崩溃记为 `infrastructure_error`，不折算成产物失败；检查集缺失时看板不显示为「全部通过」。

`artifacts/observatory.html`（或明确指定的版本文件）是自包含、无API、不可写入的脱敏快照。GitHub Pages使用轻量多文件版：元数据和可信查看器内联，缩略图按视口加载，全尺寸截图和录像由操作触发；`offline.html` 保留可下载的单文件完整版。两版run数据与完整媒体派生字节一致，缩略图是额外派生，不修改归档。公开媒体经过压缩，部分录像裁掉无内容边缘，不等于档案原始像素。生成HTML/SVG始终不在报告主站执行。

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

`.report-manifest.json` 记录当前报告的精确文件/hash/MIME/大小，`media/` 只放内容寻址的栅格图和录像；`.report-assets.json` 是发布器维护的追加式资源所有权账本。修改过的已管理文件、未知媒体或符号链接均阻止发布，不自动接管、删除或覆盖。已有日期HTML保持原字节，新日期副本来自自包含offline文件，不来自轻量index。日期文件仍是历史合并副本，不是假装的单日数据集。

当前发布验收器支持 `python3 tests/verify_public_snapshot.py --help` 和 `--site artifacts/site --output /tmp/report-check`；需要原生Python Playwright与已安装的Chromium，可使用现有隔离runtime镜像执行。它先检查冷缓存首屏请求预算，再进行全图片解码及指定录像播放，两阶段分别统计。历史对照可显式传入 `--baseline-snapshot` 与对应 `--baseline-viewer-script`，不把不同时代的查看器混为一谈。

发布器只向已存在且有权限的仓库提交；clone失败不会新建仓库。它在临时clone的发布分支提交，再fast-forward推送指定分支，不使用force。Pages根目录是报告，`source/` 是白名单源码。`source/.source-manifest.json` 只记录源码文件哈希，不与报告资源清单混用，不含私有运行数据。源码目录有未知文件或已管理文件被另外修改时停止，不静默覆盖。

自动传输重试最多一次，原失败不覆盖。操作员要求再次补跑时，使用明确的新批次、新会话，并在本机保存人工请求和旧/新run来源；不能把已用完的retry lineage续成无限重试，也不能把人工重复样本冒充首次单样本比较。

## 后续日期

以后再次运行同一命令会生成新批次和UUID，不覆盖旧数据。改工具、提示词、评分或镜像需新的条件版本；日期趋势中指纹变化显示断点。单次样本的下降只作观察，不能直接断言“降智”。本项目没有伪装成持久化的会话内定时任务；需要每天运行时，可用主机cron/systemd显式调度该CLI并留存日志。
