# Dashboard Offer Bucket Implementation Plan

> **For agentic workers:** Use superpowers:executing-plans to implement in this session. One independent final review.

**Goal:** 首页统一“等待学校发 Offer”分组，明细保留申请类型及周期。
**Architecture:** 用仅供展示的分组函数映射阶段；所有首页统计入口复用，不修改领域投影或数据。
**Tech Stack:** Python unittest、既有HTML渲染及只读SQLite快照、原生隔离服务。
**Spec:** docs/superpowers/specs/2026-11-22-dashboard-offer-bucket-design.md

## Constraints
- 独立目录、数据库、配置及19780/19781端口；不部署、不推送、不改生产数据。
- 不改其他未提交内容，不安装依赖。
- 数据副本、密码、截图与测试日志只放STAGING_DATA，不纳入源码。

## Steps
- [x] 先写并运行失败测试：混合普通/转学、仅转学、无转学、其他阶段隔离，总数及原周期链接不变。
- [x] 修改dashboard_stage_presentation.py的首页分组辅助函数，app.py图表及分组明细使用该函数。
- [x] business_change_sync.py刷新counts、mobile/home/data.py手机统计复用；学生列表07筛选包含同组转学，其他筛选不变。
- [x] 运行展示、真实处理器及刷新/手机回归，验证原投影不变。
- [x] 独立数据库副本对照生产只读名单，核验初次加载和刷新、桌面和390px手机、原周期跳转。
- [x] 新上下文只读审查、修正发现；保留独立验收环境与报告。

## Execution record
- 先运行9项新测试：8项功能失败、1项原有行为通过；实施后新测试及相关回归共106项通过。
- Ruling: 明细沿用现有“首次申请/转学”标签表达用户要求的新申请/转学区别，不改全局业务类型文案。
- 真实桌面、390px手机、手动刷新及两个真实快照API均为13（11+2），总在办周期仍为62；手机入口名单逐条核对13位，包含两位转学生。
- 原始62条工作流投影未修改，生产代码未改、未提交推送、未部署。
- 修改前既有旧断言失败：test_student_lifecycle_phase6_ui.StudentLifecyclePhase6UIContracts.test_desktop_and_mobile_share_business_operations_without_internal_panels；独立记录，未改无关测试。
- 独立只读代码审查未发现本次引入的确定缺陷。
