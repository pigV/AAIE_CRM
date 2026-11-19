# Material Checklist Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [x]`) syntax for tracking.

**Goal:** 实施已确认的材料页查找、紧凑布局、上传入口和保存位置反馈。

**Architecture:** 独立无启动副作用的 Python renderer 生成材料页，app.py 仅在初始化后的 Material Checklist 调用；结果页保持旧 renderer。独立 CSS/JS 限定在新面板内，继续提交既有表单。

**Tech Stack:** Python 标准库、原生 HTML/CSS/JavaScript；现有 CRM 测试与上传管线。

**Spec:** docs/superpowers/specs/2026-11-19-visa-material-ux-design.md

## Global Constraints

- 保持现有材料要求、自动引用、状态含义、上传校验、权限、CSRF、审计和归类确认。
- 不部署、不重启生产、不安装依赖、不修改 schema、不提交或推送。
- 当前用户已确认开发，由当前会话连续执行，普通技术细节自行决定。

## Review Focus

- 分类、搜索及状态筛选组合后无结果时可清除；未保存字段不因筛选丢失。
- 保存失败不显示成功；离开前提醒，成功后恢复原定位。
- 独立案件不能预选其他案件材料，不能绕过人工确认。
- 用户文本、文件名、备注及搜索中的特殊字符不能成为 HTML。
- 新样式与脚本不影响 VISA Result 或其他工作区；小屏、长文件名可换行。

### Task 1: 紧凑材料 renderer 与原页面接入

**Files:** Create visa_material_workspace_ui.py, assets/visa-material-workspace.css, tests/test_visa_material_workspace_ui.py; Modify app.py, visa_evidence_candidate.py.

**Interfaces:** render_material_workspace(handler, student_id, case, rows, *, panel_id, title, description, show_smart_batch) -> str，消费原页面已做有效状态计算的初始化 rows，输出完整面板与既有保存/上传入口。

- [x] 先写 renderer 行为测试：待办数量、有效已收到状态、不适用、escaped 内容、独立与学生入口、未初始化及结果页沿用原行为。
- [x] 运行测试确认缺少功能失败；实现 renderer 与接入；运行 Python 回归。

### Task 2: 独立案件定向上传与真实前端交互

**Files:** Create assets/visa-material-workspace.js, tests/js/visa_material_workspace.test.cjs; Modify app.py, visa_only_materials.py.

**Interfaces:** target_material_item 仅作当前案件人工确认表单的预选；JS 从 data-visa-material-workspace 面板读取 data-* 行属性，不重建表单。

- [x] 先测试合法当前材料预选、非法/外案件拒绝预选，保留 return_tab 与材料位置。
- [x] 实现真实搜索组合筛选、分类计数、展开、状态颜色、dirty 反馈、离开提醒及案件作用域位置恢复。
- [x] 运行脚本语法、DOM 交互测试；检查状态更改后计数和保存 payload，避免筛选漏掉字段。

### Task 3: 隔离验证与验收交付

**Files:** task-local reports/，必要的测试 fixture。

- [x] 使用现有 bundled Python/Node 运行相关回归，输出保存在本目录；测试数据、临时文件及存储绑定本任务。
- [x] 核查改动范围、文件哈希和生产文件未改；审查隔离数据库/文件读写边界。
- [x] 能运行浏览器则验证桌面/窄屏、搜索上传保存回读；受限制则明确未验证，保留验收目录。
- [x] 输出中文验证记录和验收步骤，等待材料页验收，不开始 VISA Result。
