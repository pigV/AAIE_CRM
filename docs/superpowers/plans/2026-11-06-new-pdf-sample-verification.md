# 新PDF样本验证与修复（2026-11-06）

开发目录：`/Volumes/Data/AAIE_CRM/worktrees/local-document-extraction-20261105`。本轮专注申请文件识别；Revenue测试暂停。没有生产部署、迁移、重启，未提交或推送项目Git。

## 样本与评估范围

- 用户提供20份PDF，共95页：CoE 5、CAAW 5、Offer 7、确认信3。原件只读，私有副本及结果留在被忽略的 `STAGING_DATA/new-samples-20261106`。
- 20份文件哈希各不相同，其中4份命中过往处理记录；其余16份未命中本次检查的既有处理记录。按同一学生合并后为4组，不能当20种独立模板或20个独立学生。
- 先用13份诊断，整组7份保留；在修复代码冻结后才读取保留组内容并执行真实解析/模型推理。保留组首次试验没有全部通过，其失败未被删除或改写。
- 字段答案由原文及渲染页独立标注。南澳Provider初始参考误把for写成of，核对原件Provider行后纠正，保留旧标注及纠正审计，未将该人工笔误算成模型错误。ISEC角色由用户明确确认：语言阶段；主课从Year 9开始。
- 最终只对已标注的72项进行核对：诊断组48项（含4项应为空），南澳组24项。不是所有字段或任意模板的整体准确率。

## 已复现的问题及修复

1. **有文字层但字体映射损坏。** 一份CAAW在Docling中变成数字编码；签字Offer亦含多页坏文字。仅启用库内坏文字替换还不能覆盖该文件：现先检查PDF原始文字层，再按原页号分段，仅对判坏页面强制本机整页OCR。正常页面保持默认解析；后续坏页不能被当作可跳过的法律条款尾页。分段保留原页号，块ID及table_id统一加前缀，来源不匹配则拒绝。
2. **栏目名/不适用被当作学校。** 表格中明确Not applicable或空语言学校保持空；模型的泛称栏目占位不等于真实机构。学校和日期按同页标签/值或明确列头关联，不能借用另一角色或另一页。
3. **主课日期漏读、引用错单元格。** 原文有Host school日期，但模型只给语言程序，或把日期值引用到Course Dates表头。现从明确同页表格恢复可验证声明，每条仍经原文引用、日期、页码校验。不是放松grounding去接受错误块号。
4. **南澳竖排多阶段表。** 原关联规则误把School:标签单元格视为空，实际名字在旁边。现在按每个Year Level/School/Start/End/Course Name阶段独立恢复，ISEC归语言，后续Year阶段归主课，不按位置交替推断。竞争/不完整来源不拼接。
5. **模型输入超限。** 首次保留试验中4页确认信也超过14,000 token，浮点坐标元数据是重要占用。模型视图只传块号、文字、页码及表格行列；完整原包和坐标保留供证据/界面校验。26,000字符、14,000输入token、3,500输出token限制均未提高。
6. **恢复路径可能复活被清除的角色。** 原始顶层/程序内声明均保留参与冲突审查，缺失或竞争角色具有持久禁止状态。顶层语言/课程日期也参与审查；多程序误混语言与主课、逆序区间、另一个有效学校或日期来源均有反例保护，不能把反向日期排序成正常日期。

涉及功能文件：`local_document_lab/parse.py`、`local_document_lab/infer_v4.py`、新增`local_document_lab/table_roles.py`、`application_document_extraction.py`。新模块纳入提取签名，原始PDF、解析包和模型输出的哈希校验保留。所有处理worker仍由内核沙箱禁网。

## 实际验证

| 范围 | 结果 | 限定含义 |
|---|---|---|
| 本机文档专项 | 95项通过 | 最终源代码的合成及已有回归 |
| 申请文档适配层 | 23项通过 | 隔离公开源码副本，未读生产数据 |
| 13份诊断参考 | 44项正确值、4项正确空值 | 两份乱码文件真实重解析/推理，其余使用固定原输出验证最终适配规则；不是13份全部按最终版本重跑 |
| 7份首次保留试验 | 4份CoE/CAAW的14项正确；2份Offer候选空；1份确认信输入超限 | 失败原样保留，不能称首次盲测通过 |
| 南澳问题修复后 | 24项匹配 | 3份真实重新推理、4份原模型输出按当前适配规则回放，属于已见样本回归 |

南澳确认信修复后实际输入6,857 token，两份Offer各10,268 token；都在既有14,000上限以内。不同文件时间/内存不作通用性能保证。

代码审查发现的冲突复活、顶层日期遗漏、错误角色日期授权等均先补公开合成RED反例再修，最终限定复核无已报告残留。语法和差异检查以最终代码哈希记录为准。

## 真实CRM页面验收

本任务开发web/broker已在核对PID命令和工作目录后重启，仍仅监听`127.0.0.1:18960`，数据库为本任务`STAGING_DATA/ui-validation/database/browser.db`，只有合成学生。生产服务未触碰。

页面验收使用一份此前乱码CAAW及一份语言栏明确不适用的Offer，检查上传→REVIEW→本机候选→人工值保护→确认保存。首次脚本30秒导航等待不足；服务器日志随后返回审核303，失败脚本和日志保留。重试仅调整验收脚本等待到180秒并使用DOMContentLoaded，不改变产品限制。

最终页面验收2份均成功：CAAW的两项福利日期、Offer的学校/课程开始日候选与独立参考一致；Offer不产生不适用的语言学校候选。两个有意人工修改值保持不变。确认保存后，合成库6字段回读匹配（4模型字段、2人工字段），存档PDF哈希等于原件。当前任务请求、解析包、模型输出及最终代码签名一并核对一致，20份用户原件哈希未改变。

验收后 `/health` 仍为staging且数据库精确指向本任务合成库，broker心跳新鲜。测试环境已有家长通知轮询缺表错误，未把该错误改称已解决，也未据本次上传成功声称全库正常。

## 本机证据

全部位于`STAGING_DATA/new-samples-20261106`：

- `manifest.private.json`、`inventory.private.json`、`prior-exposure-audit.private.json`、`cohort-resolution.private.json`。
- `baseline-summary.json`、`reference-fields.private.json`、`root-causes.private.json`、各文件parse/infer原始结果。
- `holdout-freeze.json`、`holdout/summary.json`、`holdout/frozen-round-evaluation.private.json`、参考纠正审计。
- `ocr-recheck-v2-summary.json`、`ocr-inference-summary.json`、`post-blind-regression-summary.json`。
- `fixed-diagnostic-evaluation.private.json`、`final-heldout-family-regression.private.json`。
- `local-document-final-state.log`、`application-document-final-regression.log`、各次RED记录、`final-code-verification.json`。
- `owned-ui-restart.log`、`ui-verification.json`、`ui-retry-run.log`及页面预期文件。

用户批准的纠错记录已追加至根`adjustment.md`。此前全库历史测试待办仍单独存在，本轮没有冲销它们；也没有把20份样本的结果称为生产验收或任意新模板保证。

最终补记：`ui-persistence-verification.json`核验6字段、两份存档PDF哈希及20份原件未改；`final-owned-health.json`核验当前测试环境和broker。`git diff --check`通过。
