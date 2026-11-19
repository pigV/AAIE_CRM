# VISA Result Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** 按已确认第二阶段方案集中结果状态、下一步、文件与日期。
**Architecture:** 新增无副作用的结果页 renderer，消费已有 case/result projection 与材料 rows；保持原表单端点。限定 CSS/JS，复用现有文件控件，不重复日期或结果表单。
**Tech Stack:** Python、原生HTML/CSS/JS、既有staging沙箱。
**Spec:** docs/superpowers/specs/2026-11-19-visa-result-ux-design.md

## Global Constraints

- 不新增递交限制，不自动改变案件状态，不运行生产迁移或部署。
- 不提交/推送；材料分支和验收服务保留；只在新任务目录写入。
- 用户已授权连续实施，技术细节由本会话决定。

## Review Focus

- 已上传回执≠正式递交，已收到体检通知≠已完成体检，自动检测结果≠官方已确认。
- 分组后所有未知或历史材料仍可访问，隐藏控件仍归属正确保存表单。
- Date of Grant和Grant Sent On不混淆；没有结果不伪造0天。
- 结果更正、周期和案件归属及原有权限/审计不被绕过。
- 只保留一套日期/结果表单；手机无横向溢出或浮动助手遮挡。

### Task 1: 结果阶段及材料分区
**Files:** visa_result_workspace_ui.py、tests/test_visa_result_workspace_ui.py、app.py。
- [x] 写阶段和分组测试，先运行确认缺少功能失败。
- [x] 实现 presentation-only stage_summary(case,result) 与 render_result_materials(handler,...rows)，使用原保存/上传/文件接口。

### Task 2: 页面接入及日期集中
**Files:** visa_workspace_candidate.py、visa_document_processing_ui.py、assets/visa-result-workspace.css/js、tests/js/visa_result_workspace.test.cjs。
- [x] 为compact日期/结果更正和单份日期表单补测试，保留所有原字段/action/CSRF。
- [x] 将处理信息只放入结果页，学生与独立案件均使用新wrapper，保留独立官方确认与递交版本入口。
- [x] 实现分组定位/展开、保存位置、dirty提示；确认返回结果页及原组。

### Task 3: 隔离验证与交付
**Files:** 测试fixture、reports/acceptance-result.md。
- [x] 新建本任务staging配置18970/18971、合成数据和存储；不复制生产或材料任务数据库。
- [x] 运行相关Python/JS和业务回归，浏览器验证桌面/手机及保存回读。
- [x] 独立审查、修复重要问题并复测，保存截图、哈希及中文验收记录。
- [x] 保留分支和服务供验收，不部署。
