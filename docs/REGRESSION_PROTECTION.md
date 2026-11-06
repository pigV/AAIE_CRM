> 迁移恢复说明（2026-11-05）：本文从旧 CRM workspace 恢复，用作历史功能保护清单。旧状态或路径不代表当前验收结果；当前规则以项目根 AGENTS.md 和用户确认内容为准，完成状态须看本轮实际验证记录。

# CRM Regression Protection Rules

This document defines how future CRM changes should avoid breaking stable features.

## 本次修改范围声明

Before code changes, Codex must state:

1. 本次要修改的功能是什么
2. 会改哪些文件
3. 哪些文件不应该碰
4. 可能影响哪些已有功能
5. 是否涉及数据库 migration
6. 是否涉及 PDF 生成
7. 是否涉及微信授权
8. 是否涉及 Application 文件
9. 是否涉及前端全局组件
10. 是否涉及 CSS / JS 全局样式
11. 本次需要跑哪些回归测试

For a local fix to feature A, do not refactor unrelated features B, C, or D.

## 禁止误改区域

These areas are protected. If they must be changed, explain the reason, risk, and regression plan first.

1. 全局 CSS, especially broad selectors such as `table`, `button`, `input`, `select`, `textarea`, and mobile media rules
2. 通用预览组件 and `/documents/<id>/preview`
3. 通用文件保存、版本、下载、NAS path safety, and Application document archive logic
4. 微信 OAuth, callback, identity binding, and token handling
5. Application 文件表结构 and document type classification
6. 合同 PDF / signature / stamp merge logic
7. 学校方案生成, PDF rendering, secure share links, watermarks, and confirmations
8. 全局路由 dispatch and permission routing
9. 数据库基础字段, migration-style column additions, and indexes
10. Broad cleanup or architecture rewrites caused by one local bug

## 高风险公共文件

| File | Risk |
| --- | --- |
| `app.py` | Most routes and business logic live in one file. Small edits can affect many workflows. |
| `assets/app.js` | Global browser behavior, AJAX forms, preview dialog, scroll behavior, and mobile interactions. |
| `assets/style.css` | Global visual system and broad selectors. Mobile changes can leak across modules. |
| `database.py` | Schema and migration-style setup for students, documents, contracts, school plans, and WeChat fields. |
| `student_doc.py` | StudentInfo Word and contract Word generation. |
| `ui.py` | Shared page shell, parent form markup, navigation, and common HTML helpers. |
| `templates/contracts/` | Contract output depends on these templates. |
| `config/wechat_oauth.env.example` | OAuth setup documentation and callback reference. |

## CSS / JS Scope Rule

Prefer module namespaces:

- CRM admin pages: admin-specific container or existing panel classes
- Parent form: `.parent-form` and public form data hooks
- Contract signing: contract-specific classes and data hooks
- School plan secure view: school-plan secure-view namespace
- File preview: preview-specific data hooks and classes

Avoid adding broad rules that target every `table`, `button`, `input`, or mobile layout globally.

## Regression Command

Use the project-local command:

```bash
python3 scripts/regression_check.py --student-info
python3 scripts/regression_check.py --contract
python3 scripts/regression_check.py --school-plan
python3 scripts/regression_check.py --preview
python3 scripts/regression_check.py --interaction
python3 scripts/regression_check.py --full
```

Reports are written to:

```text
reports/regression/
```

## 修改后回归测试报告

After a CRM code change, report:

1. 本次修改了什么
2. 修改了哪些文件
3. 没有修改哪些保护功能
4. 跑了哪些回归测试
5. 哪些测试通过
6. 哪些测试失败
7. 如果失败，是否已经修复
8. 是否可能影响旧功能
9. 是否需要用户手动重点测试

Do not only say "fixed".

## Git and Rollback Rule

The current CRM folder may not be a Git repository. If Git is unavailable, use project-local backups and regression reports first.

Recommended once Git is enabled:

1. Create a feature or fix branch per task.
2. Commit the current stable state before risky work.
3. Commit after the fix and tests.
4. Keep database migrations traceable and reversible where possible.
5. Do not commit, push, or initialize Git without user confirmation.

Suggested branch names:

- `feature/contract-signing-refresh`
- `fix/contract-signature-position`
- `fix/no-scroll-to-top`
- `fix/school-plan-mobile-layout`

## Release Checklist

Before treating a change as ready:

1. CRM 首页能打开
2. 学生列表正常
3. 学生详情正常
4. Application 文件正常
5. 合同功能正常
6. 学校方案功能正常
7. 微信授权正常 or clearly marked manual-test-required
8. 手机端页面正常 or clearly marked manual-test-required
9. PDF 预览正常
10. DOCX 预览正常
11. 不出现 502
12. 不出现白屏
13. 不出现中文乱码
14. 按钮操作后不跳顶部
15. 已稳定功能没有被破坏
