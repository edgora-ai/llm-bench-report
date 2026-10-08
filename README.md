# 模型能力基准 · 一次会话评测

固定任务、固定隔离环境下的模型一次会话完成度对比。

本仓库的报告由 `llm-bench` 发布步骤自动生成，报告为只读脱敏合并快照，不含凭据、原始日志或本机路径。源码与运行配置分开，私有配置、二进制和原始归档不上传。
index.html 随发布更新。日期命名文件是首次创建时的合并数据副本，不是该日期专属数据；旧版文件保留原字节，不重新包装成日快照。

[项目源码与复现说明](source/README.md)

[正式评测](index.html?purpose=benchmark) · [含冒烟的全部尝试](index.html?purpose=)

按样本日期筛选最新合并数据：
- [2026-09-30](index.html?purpose=benchmark&date_from=2026-09-30&date_to=2026-09-30)
- [2026-10-01](index.html?purpose=benchmark&date_from=2026-10-01&date_to=2026-10-01)
- [2026-10-08](index.html?purpose=benchmark&date_from=2026-10-08&date_to=2026-10-08)

## 结果解释

会话正常结束、页面交付、自动检查与人工视觉评分分别展示。CLI费用未经供应商账单核验，未知计量不补零。既有14条非盲AI建议评分仍待人工复核。

2026-10-08 的 Sol 与 6.1-Sol 鳄鱼题人工补跑均正常结束、8/8检查通过；此前失败保留，不被新结果覆盖。Astra/Luna早先黑洞样本的上游count_tokens403曾被旧评估逻辑误记为路由违规；原检查仍保留，源码已区分上游与本地拒绝，不把历史误判当成模型调用了其他路由的证据。

重新发布请在 `source/` 内执行 `python3 tests/publish.py <snapshot.html> --include-source`。
