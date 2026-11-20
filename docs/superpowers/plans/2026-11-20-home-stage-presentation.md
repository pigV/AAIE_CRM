# Home Stage Presentation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** 首页移除签证核实提示，明确转学资料准备，并统一阶段颜色，在隔离环境截图验收。

**Architecture:** 新增纯展示模块，按stage_key统一颜色、标签和展示顺序。app.py在整页和现有快照刷新共用路径调用它，保留VISA管理提示及全部事实分类。

**Tech Stack:** Python、现有服务端HTML、CSS、SQLite隔离副本、现有OS沙箱运行控制。

**Spec:** docs/superpowers/specs/2026-11-20-home-stage-presentation-design.md。

## Global Constraints

- 只在独立工作树修改；不改生产数据或服务，不执行迁移或上线。
- 不安装依赖；不提交、推送；保留原有工作树和测试资料。
- 使用现有已迁移隔离库的独立副本，独立端口、namespace与存储；真实生产和NAS由沙箱拒绝。

## Review Focus

- 新增转学阶段或重排后，原阶段颜色保持稳定。
- 转学周期仍计入分布，不能仅为隐藏难懂标签丢掉学生。
- 历史/已结案/未知阶段徽标有安全的默认颜色，HTML需转义。
- 首页整页及自动刷新片段均不重新出现VISA提示，VISA管理仍有提示。
- 数据库业务事实不能因展示变化而写回或重算改动。

## Task 1: 共享阶段展示

**Files:** Create dashboard_stage_presentation.py、tests/test_dashboard_stage_presentation.py；Modify app.py、assets/dashboard-workflow-distribution-chart-v1.css。

- [ ] 先写并运行测试，证实当前缺少共享颜色/展示顺序能力。
- [ ] 实现stage_color(key)、stage_label(key,fallback)、ordered_stage_definitions(definitions)、stage_badge_html(key,label)，纯函数无数据库依赖。
- [ ] 图表和最近学生共用这些函数；只从首页删提示调用；VISA管理保留。
- [ ] 运行纯展示、既有阶段分类和分布回归；检查差异。

## Task 2: 隔离原生页面与截图

- [ ] 备份已存在隔离库到新独立staging运行目录，按新根重定位已有路径，复用隔离密钥/模板，清理复制会话，保留隔离管理员并记录独立环境身份；不从生产取数据或配置。
- [ ] 端口使用前核实空闲；用现有runtime_control启动本任务受管服务；记录健康及OS生产/NAS拒绝访问证据。
- [ ] HTTP登录后核对首页与dashboard_overview/dashboard_workflow快照没有VISA核实提示，visa_overview仍保留；对比数据行数量/阶段key前后相同。
- [ ] 浏览器验证最近学生徽标文字色与图表对应条形颜色完全一致，标签和顺序正确；触发只读刷新后重验。
- [ ] 保存首页、颜色对照、转学阶段明细、VISA提示截图并交付用户；不进行生产变更。
