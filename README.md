# 模型实验台

模型入口默认展开，选择模型或切换任务后直接定位作品。Kimi K3 两个主评测作品与六项评分保持原样。MiMo 鳄鱼新增一次独立 SVG 补测（standalone-svg-v1）：length 截断，推理 Token 32000、输出 Token 0、文件 0。没有录像、自动重试或付费回退，也没有新增评分。此补测与原 HTML 协议分开记录，不能据此判定模型绘画能力。

本仓库的报告由 `llm-bench` 发布步骤自动生成，报告为只读脱敏合并快照，不含凭据、原始日志或本机路径。源码与运行配置分开，私有配置、二进制和完整运行归档不上传。
index.html 随发布更新。日期命名文件是首次创建时的合并数据副本，不是该日期专属数据；旧版文件保留原字节，不重新包装成日快照。

在线首页直接展示视口内已审核的实时原作，截图仅作回退；不是视频回放。可停止和重启，筛选或离开视口会清理预览。原文件字节不变，执行文档经过隔离与资源装载适配。沙箱限制下载、存储及常规外联，不提供容器级断网或资源配额。

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

## 结果解释

会话正常结束、页面交付、自动检查与人工视觉评分分别展示。CLI费用未经供应商账单核验，未知计量不补零。共40条评分记录，其中26条新非盲AI作品复核建议待人工复核；保留14条历史评分，不合成总分。

画廊默认仅展示每个（工具，模型，任务）三元组的最新一次；点击详情进入完整实验台查看历史对照。全部尝试与正式样本计数见首页。

第二轮补齐样本：1 个组合新交付；5 个组合已尝试仍失败（多为 session_error/length 空交付）；1 个组合条件阻塞（step-5 黑洞）。失败样本保留，不被覆盖，也不重跑。

重新发布请在 `source/` 内执行 `python3 tests/publish.py <site-directory> --include-source`。
