# releases/main 作为正式生产代码目录的切换方案

## 用户已确定的目标与授权

用户已允许导入当前正式服务数据，并要求以后 `/Volumes/Zhang/AAIE CodeX/CRM/releases/main` 的代码启动为生产环境。生产启动逻辑修改、具体迁移和正式入口切换按用户“先给修改方案、确认后再改”的规则，在本方案确认后执行。

代码根固定为 `/Volumes/Zhang/AAIE CodeX/CRM/releases/main`；正式数据根固定为 `/Volumes/Zhang/AAIE CodeX/CRM/releases/runtime/production`。代码更新不搬动数据库、密钥或业务文件。测试须显式选择 `runtime/staging/<实例>` 和独立端口，不允许默认连接合成测试库，也不因生产配置缺失回退创建空库。

## 已完成的导入与现状核实

- 现有正式 Admin/Parent：PID 10491/10493，端口 8780/8781；当前业务库为 `/Users/faqiang/Downloads/AAIE_CRM_Test_5985902_20261030/STAGING_DATA/database/staging.db`。这些服务技术标记仍为 staging，本方案以用户指定的现有正式端口为迁移来源，不把技术标记当成另一套数据源。
- 内容提取：PID 9779，8792，同一部署中的 `database/content_extraction_service.db`。
- Agent Reach：PID 10362，8790；数据库为独立后端 `var/data/xhs-intelligence.sqlite3`。
- 本次导入位置：`/Volumes/Zhang/AAIE CodeX/CRM/releases/runtime/imports/production-20261101-022012`。
- 三份数据库均通过 SQLite 在线备份取得；各自 `integrity_check=ok`，外键异常均为 0。它们是各库独立一致性快照，不是多库和附件的全局原子快照，正式切换前仍须停写并最终同步。
- CRM：students 176、parents 344、contracts 222、schools 1079、users 7。Agent Reach：runs 104、trend_runs 12、managed_keywords 18。
- 已复制并校验：39 个 storage 文件、25 个上传暂存文件、71 个报告、2 份模板和匹配的字段加密密钥。源 secure_assets 目录为空，不据此宣称签章等依赖完整。NAS 内容没有复制；授权配置仅保存为来源参考，未启用。
- `IMPORT_MANIFEST.json` 保存快照来源、数据库校验和、业务计数和逐文件哈希。目标卷自动产生的 AppleDouble `._` 元数据不参与业务文件完整性比较；没有删除源文件。
- 当前 29880/29881/29890/29892 是独立合成测试实例；导入副本未接入它们，也未替换现有准备中的生产库。

## 当前必须解决的问题

1. `start.command`、“打开 AAIE CRM.command”、“停止 AAIE CRM.command”等入口仍调用旧 `test_copy_control.py`，不支持用户指定的默认生产语义。
2. `runtime_control.py`/`runtime_server.py` 当前只支持 localhost 且拒绝 NAS/LAN 授权；来源正式管理端支持局域网 `192.168.1.123`，且启用了 NAS 读写。须迁移并验证相同业务能力，不得把权限空缺伪装成上线成功。
3. `runtime_auxiliary_control.py` 当前仅允许 staging；需为正式数据根和生产端口增加明确的生产准入、进程身份及生命周期支持，继续保持测试与生产隔离。
4. 最新正式 CRM 库仍需迁移 `20261031_043_offer_language_school`，内容是给 offers 增加可空 `language_school_name`。先在副本演练；正式最终副本需显式迁移，不通过应用启动隐式修复。
5. 新生产启动预检缺少字段密钥与模板；本次导入已获得匹配文件，但尚未写入活动生产根。数据库中的旧绝对附件路径、NAS、签章、家长公网入口、OCR 及当前实际外部服务能力须核对。不得直接继承整份终端环境或无差别放行外网。

## 确认后执行的最小改造

- 修改 `runtime_configuration.py`、`tools/runtime_control.py`、`tools/runtime_server.py`、`staging_environment.py`，支持明确生产配置下的原有局域网/NAS能力，并保持路径和身份检查。
- 修改辅助服务入口与控制器，使用正式独立数据目录、原有四个端口和明确生产控制授权。保留 Agent Reach 历史、配置及既有功能边界，不自动开启新的调度或新的外部数据源。
- 修改现有启动、停止、重启脚本，使本代码目录默认选择生产配置；测试必须显式指定。普通启动遇到已有外来监听者时拒绝启动，不抢占端口或终止未知进程。
- 使用导入副本演练 043 和路径迁移，测试登录、学生列表/详情、合同及附件、家长入口、报告、内容提取、Agent Reach；不使用合成账号替换正式账号。
- 记录本次代码文件清单及哈希作为可回退候选；保留已有未提交修改，不擅自提交、推送或强制重置 Git。

## 正式切换步骤与回退

1. 在现有服务继续运行时完成代码改造、副本演练、所需依赖和路径校验。任何关键功能失败都不切换。
2. 核实四个现有进程的完整命令、PID、出生时间和启动控制方式，确认家长公网入口/局域网依赖。仅使用已核实的所属实例控制，不安装或改动 workspace 外的系统服务文件。
3. 进入短暂停写窗口，停止旧的正式服务，取得最终数据库与变更附件同步。保存原生产准备目录和最终切换前快照，避免混入本次导入后的新增业务记录差异。
4. 将最终已核实数据放入 `runtime/production`，显式完成 043；使用匹配密钥、业务模板和经核对的附件/NAS路径。启用新入口：管理端 8780、家长端 8781、内容提取 8792、Agent Reach 8790；地图使用新配置的 8793（先检查可用性）。
5. 验证健康接口、代码/数据库身份、业务读取、账号登录和正式入口；通过后开放新环境业务写入，旧环境保持停止，避免双写。
6. 开放写入前若失败，停止新实例并用已记录的旧入口恢复原服务/数据库。若新环境已产生业务写入，不直接回退旧快照；先保存新增数据并制定数据收敛方案。

正式启动默认指向生产并不意味着获准绕过这些检查。生产切换成功后才把该目录标记为正在运行的正式代码目录。

## 已确认并追加到 adjustment.md

`/Volumes/Zhang/AAIE CodeX/CRM/releases/main` 固定为正式生产代码目录，默认启动必须使用 `/Volumes/Zhang/AAIE CodeX/CRM/releases/runtime/production`；测试运行须显式指定独立 staging 配置和端口。禁止缺失生产配置时回退合成测试库、空库或旧部署路径。代码更新不移动或覆盖生产持久数据。

## 状态

2026-11-01 已完成：启动入口默认生产，最终停写同步和 043 迁移通过，正式端口已切换到 releases/main 与 runtime/production，门禁验收后已开放。adjustment.md 已追加。Agent Reach 按独立部署规则保留原后端和数据库，由 CRM 验证身份及健康，不迁移其浏览器/采集依赖。详细证据与既有问题见 docs/runtime-layout.md 及 runtime/cutover-20261101。
