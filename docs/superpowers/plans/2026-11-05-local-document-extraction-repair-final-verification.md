# 本机申请文档识别：后续修复与隔离验收

后续状态已更新：CAAW 分块日期过滤及 VIC Offer 语言结束日冲突现已修复，见 `2026-11-05-local-document-remaining-repairs-verification.md`。下文保留本报告生成时的历史验收状态。

日期：2026-11-05。开发目录：`/Volumes/Data/AAIE_CRM/worktrees/local-document-extraction-20261105`。本报告接续 review-enhancement-verification，取代其中“核心测试尚有三项问题、真实页面未验收”的状态。没有生产部署、生产迁移、生产重启或 Git 提交/推送。

## 修复内容

- `local_document_lab/layout_dates.py`：以完整学校名称、同一学校角色的原文语句或同页同一行的日期标签定位语言课程日期。排除混合主课/语言课程的段落、其他学校、跨页和重复来源；有多个相互竞争的日期来源时同时清除模型摘要及语言课程项日期，交由人工确认。
- `application_document_extraction.py`：本机 broker 文件队列、任务身份/模型/来源校验、原文证据、失败时人工 REVIEW。启用标记必须有明确 scope，且与进程环境变量一致；单独 `enabled:true` 不会启用。
- `app.py`、`assets/local-document-review.js`：在现有审核流程展示本机候选值；人工输入保持优先，有冲突时由用户明确选择。源片段、页码、日期显示及切换文件类型后的过期响应得到验证。
- 修复实际执行遇到的超时：`import_cache.py` 缓存固定环境的公开软件元数据和 transformers 导出结构；不缓存学生数据或执行 pickle。缓存指纹不匹配时使用原库流程。冻结环境的 Python 3 模块已预编译；compileall 对历史 Python 2 文件 `olefile/olefile2.py` 报语法错误，不能称为全部预编译成功。
- `parse.py` 根据原文内容选择需要完整布局解析的页码范围；剩余页面保留全部可读文字、原始页码及页面级位置精度标记。扫描页、独有日期及显式课程/学校等信息仍要求布局解析。`infer_v4.py` 优先读取结构块；v3 保留供历史回放。
- `tests/test_core.py` 修复两项 staging 禁止创建 symlink 的预期和一项失效的故障注入。回滚测试使用真实数据库连接，在实际 Offer INSERT 后触发 SQL 错误，在请求连接清理前验证回滚，未替换真实事务管理。

## 验证结果

- 当前修复相关 Python 测试：41 项申请文档测试、51 项本机识别测试、2 项原生上传路径保护测试，全部通过。另 15 项核心测试全部通过。
- 合成真实 Chrome 测试通过：人工值保护、明确选择冲突候选、日期显示、源页码、HTML 转义、文件类型过期响应、失败回退及已完成候选保持。
- 三份原始 PDF 经过实际 CRM 上传→REVIEW→本机模型→确认保存：旧 NSW cover、VIC Offer、用户提供的 NSW SAMPLE。仅使用独立合成学生与独立数据库。最终 21 项提交字段回读全部匹配，保存 PDF 的 SHA256 与原件一致。额外验证了用户明确选择学校候选，以及手动金额、币种和日期的保存。
- 实际停止本任务 broker 后，页面进入人工回退，仍可编辑和取消；取消未增加业务文档。随后已恢复 broker 并验证 heartbeat。后续有意上传的 Offer 不计入先前取消验证的文档计数。
- 当前实际模型运行中，Offer 两个已有学校参考字段及 SAMPLE 四个参考字段正确。10 份历史探索样本的固定输出回放，已有 12 项参考字段全部正确；内部候选完整性为 9/10，不能当作准确率或盲测结果。VIC Offer 的语言结束日期无法单独确认，按规则留空，不用混合段落的主课结束日代填。
- 性能诊断证明重复软件元数据扫描及动态导入是主要冷启动开销。实际三个任务的解析/推理阶段分别约 46/95、17/65、70/150 秒；这些是已观察样本的阶段时间，不是通用网页响应保证，且不含所有来源/模型哈希校验时间。
- 全库旧测试尝试及受限项已记录在 `STAGING_DATA/results/complete-suite-accounted.json`：5,560 条去重结果记录中，4,231 通过、124 失败、700 错误、36 跳过、467 超时/加载受限、2 导入受限。记录可能包含模块加载占位项，因此不是 5,560 个独立成功执行的方法。所有选定待测方法均有结果或受限记录，不能称为全库通过。常见错误包括 PermissionError、IsolationError、迁移重建备份要求、缺失文件及断言；不能把未做基线对比的失败一律断言为既有问题。源快照较早，全库结果不替代上述当前代码专项测试。

## 隔离及验收范围

所有原件、截图、字段值、模型输出和测试数据库留在本机被忽略的 STAGING_DATA；不发送到第三方 OCR/模型。任务处理进程由内核 sandbox 禁网，web 仅经本地文件队列提交任务。公开模型预下载与原件处理分离。

实际 UI 仅监听本任务 `127.0.0.1:18960`，使用 `STAGING_DATA/ui-validation/database/browser.db`。该合成库为页面测试应用现有迁移 20260805_013、20260808_016、20261031_043，并留存备份和回执；没有新增生产 schema 变更，也没有访问或修改生产学生记录。

当前仍未证明任意新模板的全字段语义正确，也未作生产验收。正式发布还需独立核对生产差异、运行身份及发布后验收；不能据少量参考字段和开发测试宣称整体准确率或生产已上线。

## 本机证据

- `STAGING_DATA/results/actual-review-persistence.json`：21 项字段、PDF 哈希、合成学生及取消验证。
- `STAGING_DATA/results/actual-review-browser.json`、`actual-review-fallback.json`：真实页面流程。
- `STAGING_DATA/results/review-core-tests.json`：15 项结果及被测源码 SHA256。
- `STAGING_DATA/results/layout-final-evaluation.json`：历史输出回放，12/12 参考字段和 9/10 内部完整性。
- `STAGING_DATA/logs/application-document-repair-final.log`、`local-document-repair-final.log`、`review-browser-repair-final.log`：当前专项测试。
- `STAGING_DATA/ui-validation/fixture-migration-receipt.json`：仅合成库的既有迁移。

独立代码审查发现的日期角色混淆、学校子串匹配、竞争来源未清除模型值、非 scoped 启用及虚假事务故障注入均已修复并补充测试。后续性能改动另经专项测试和上述真实原件运行验证。

最终只读健康验证：隔离 web /health 返回 staging 且数据库路径精确匹配合成库；本任务 broker 心跳新鲜。服务保持可供本机审核。git diff --check 及当前修改模块语法检查通过；核心测试记录的 app.py/test_core.py SHA256 与最终源码一致。
