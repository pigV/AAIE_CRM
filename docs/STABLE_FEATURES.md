> 迁移恢复说明（2026-11-05）：本文从旧 CRM workspace 恢复，用作历史功能保护清单。旧状态或路径不代表当前验收结果；当前规则以项目根 AGENTS.md 和用户确认内容为准，完成状态须看本轮实际验证记录。

# CRM Stable Features Protection List

This file is the source of truth for CRM features that must not be broken by future changes.

Status values:
- `Stable`: confirmed working by testing or user acceptance.
- `Protected`: must not be changed indirectly without scope declaration.
- `Under Development`: active work is allowed.
- `Needs Retest`: listed for protection but still needs a fresh regression pass.

Default rule: new or repaired features become `Stable + Protected` after the user confirms they work.

## Protected Feature Register

| ID | Feature | Status | Protection Notes | Regression Coverage |
| --- | --- | --- | --- | --- |
| SF-001 | 学生管理列表 | Protected + Needs Retest | Do not break `/students`, table/card views, sorting, filters, or deleted-student visibility. | `scripts/regression_check.py --core`; manual UI check |
| SF-002 | 学生详情页 | Protected + Needs Retest | Do not break `/students/<id>` tabs, workflow summary, or section rendering. | `scripts/regression_check.py --core`; manual UI check |
| SF-003 | 学生搜索功能 | Protected + Needs Retest | Do not change search query handling or student list filters without targeted tests. | `scripts/regression_check.py --core`; manual UI check |
| SF-004 | Application 文件上传 | Protected + Needs Retest | Do not change shared upload, storage, versioning, or NAS path rules casually. | `scripts/regression_check.py --preview`; manual upload check |
| SF-005 | Application 文件预览 | Protected + Needs Retest | Do not change unified preview dialog, preview image generation, or page count logic casually. | `scripts/regression_check.py --preview` |
| SF-006 | Application 文件下载 | Protected + Needs Retest | Do not change document download routes or headers without checking preview/download flows. | `scripts/regression_check.py --preview`; manual download check |
| SF-007 | 文件类型修改 | Protected + Needs Retest | Do not change document type detection or review confirmation behavior without focused tests. | `scripts/regression_check.py --preview` |
| SF-008 | Offer 上传和自动识别 | Protected + Needs Retest | Do not change Offer OCR/text extraction rules while fixing unrelated upload bugs. | `scripts/regression_check.py --core` selected Offer tests |
| SF-009 | COE / OSHC 文件预览 | Protected + Needs Retest | Do not break COE/OSHC document type detection, preview, or editable panels. | `scripts/regression_check.py --preview` |
| SF-010 | 学生信息表在线填写 | Protected + Needs Retest | Do not break public parent form rendering, validation, JSON submit, or friendly error messages. | `scripts/regression_check.py --student-info` |
| SF-011 | 学生信息表提交后生成 Word | Protected + Needs Retest | Do not change `StudentInfo.docx` generation or archive side effects without tests. | `scripts/regression_check.py --student-info` |
| SF-012 | Word 文件命名规则 | Protected + Needs Retest | Keep `<First Last>_info.docx` and version suffix behavior stable. | `scripts/regression_check.py --student-info` |
| SF-013 | 合同生成 | Protected + Needs Retest | Do not change contract payload, template merge, fee rules, or DOCX/PDF conversion casually. | `scripts/regression_check.py --contract` |
| SF-014 | 合同链接生成 | Protected + Needs Retest | Do not break signing token hashing, regeneration, revocation, or expiry rules. | `scripts/regression_check.py --contract` |
| SF-015 | 合同微信授权 | Protected + Needs Retest | Do not change WeChat OAuth or identity binding unless the change is explicitly scoped. | `scripts/regression_check.py --contract`; manual WeChat check |
| SF-016 | 合同在线签名 | Protected + Needs Retest | Do not change signature capture, parent login, submit, or signed PDF placement casually. | `scripts/regression_check.py --contract` |
| SF-017 | 已签合同归档到 Application | Protected + Needs Retest | Keep signed contract and confirmation page archive behavior stable. | `scripts/regression_check.py --contract` |
| SF-018 | 合同预览和下载 | Protected + Needs Retest | Do not break contract center/student contract preview and download routes. | `scripts/regression_check.py --contract --preview` |
| SF-019 | 合同删除 / 作废 | Protected + Needs Retest | Do not break void behavior, signed document markers, or signing-link invalidation. | `scripts/regression_check.py --contract` |
| SF-020 | 学校方案生成 | Protected + Needs Retest | Do not change report snapshot, PDF render, brochure append, or Application sync casually. | `scripts/regression_check.py --school-plan` |
| SF-021 | 学校方案安全查看链接 | Protected + Needs Retest | Keep share token hashing, expiry, old-link invalidation, and viewer links stable. | `scripts/regression_check.py --school-plan` |
| SF-022 | 微信授权查看 | Protected + Needs Retest | Do not break WeChat-bound viewer identity checks. | `scripts/regression_check.py --school-plan`; manual WeChat check |
| SF-023 | 水印显示 | Protected + Needs Retest | Watermark must show correct viewer/student context and not block reading. | `scripts/regression_check.py --school-plan`; manual mobile check |
| SF-024 | 学校方案家长确认 | Protected + Needs Retest | Do not break confirm permission, signature, required ranking validation, or audit records. | `scripts/regression_check.py --school-plan` |
| SF-025 | 学校排序确认 | Protected + Needs Retest | Ranking must be complete, unique, and archived in the expected order. | `scripts/regression_check.py --school-plan` |
| SF-026 | 最终确认方案归档 | Protected + Needs Retest | Keep final confirmation PDF generation and Application versioning stable. | `scripts/regression_check.py --school-plan` |
| SF-027 | 页面操作后不自动跳到顶部 | Protected + Needs Retest | Bottom-page actions must use scoped AJAX or preserve scroll when possible. | `scripts/regression_check.py --interaction`; manual UI check |
| SF-028 | 手机端安全查看页面 | Protected + Needs Retest | Do not apply broad mobile CSS that affects admin pages. | `scripts/regression_check.py --school-plan`; manual mobile check |
| SF-029 | 手机端学校对比看板 | Protected + Needs Retest | Mobile compare board must use cards or responsive layout without horizontal page overflow. | `scripts/regression_check.py --school-plan` |
| SF-030 | 后台所有按钮操作后的局部刷新逻辑 | Protected + Needs Retest | Do not replace local updates with full page reloads unless explicitly required. | `scripts/regression_check.py --interaction`; manual UI check |
| SF-031 | Student Detail Initial Load Performance V1 | Stable + Protected | Reduces initial work when clicking a student ID into the student detail page while keeping page behavior unchanged. Do not casually modify this logic; future changes require a Safe Box point, a written reason, focused tests, and confirmation that student detail, student links, file preview, contracts, Offer, and Workflow are unaffected. This stable scope must not change database structure, student data, contracts, Offer, visa, file upload, or other business flows. | `tests/test_core.py`; manual checks for student list ID click, student detail load speed, student links, file preview, contracts, Offer, and Workflow |

## Promotion Rule

When the user confirms a feature is working:
1. Change status to `Stable + Protected`.
2. Add or update the matching regression test.
3. Record the latest verified date and any manual test notes.
4. Future unrelated work must list the feature under "protected features not touched" when relevant.

## Current High-Risk Shared Areas

- `app.py`: routes and most business workflows.
- `assets/app.js`: global browser behavior and AJAX actions.
- `assets/style.css`: global layout and broad element selectors.
- `database.py`: schema creation, indexes, and migration-style column additions.
- `student_doc.py`: StudentInfo Word and contract Word generation.
- `ui.py`: shared shells, navigation, and public form markup.
- `templates/contracts/`: contract template source files.
- `config/wechat_oauth.env.example`: WeChat OAuth configuration reference.
