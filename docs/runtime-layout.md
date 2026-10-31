# CRM 代码与数据分离

2026-11-01 已完成正式切换并开放业务写入。

- 正式代码：`/Volumes/Zhang/AAIE CodeX/CRM/releases/main`
- 正式数据：`/Volumes/Zhang/AAIE CodeX/CRM/releases/runtime/production`
- CRM 数据库：`/Volumes/Zhang/AAIE CodeX/CRM/releases/runtime/production/database/staging.db`（保留文件名，运行环境为 production）
- 内容提取数据库：同目录 `content_extraction_service.db`。
- 测试数据：`/Volumes/Zhang/AAIE CodeX/CRM/releases/runtime/staging/<实例名>`。

数据库、密钥、模板、本地附件、预览、报告、上传暂存、日志与缓存位于数据目录。原 NAS 授权保留；NAS 文件继续原位使用。代码更新不移动或覆盖持久数据，runtime 不在 Git 工作树内。

## 默认启动

双击代码目录的 `start.command` 或“打开 AAIE CRM.command”。“停止 AAIE CRM.command”“重启 AAIE CRM.command”“检查 CRM 监控.command”已连接同一生产配置。

在代码目录执行命令时使用 bundled Python：

```sh
/Users/faqiang/.cache/codex-runtimes/codex-primary-runtime/dependencies/python/bin/python3 -B tools/runtime_stack.py status
/Users/faqiang/.cache/codex-runtimes/codex-primary-runtime/dependencies/python/bin/python3 -B tools/runtime_stack.py preflight
/Users/faqiang/.cache/codex-runtimes/codex-primary-runtime/dependencies/python/bin/python3 -B tools/runtime_stack.py start --allow-production-control
```

将 start 换成 stop 或 restart 可控制所属服务。默认配置固定为 `runtime/production/config/runtime.json`，缺失时拒绝启动，不生成空库或回退测试库。禁止直接运行 app.py 初始化。启动不调用 CRM db.initialize 或模板种子初始化，待执行迁移须另行明确处理。

测试须显式传入 `--config` 和绝对配置路径，使用独立端口。测试不能使用 8780、8781、8790、8792、8793，不能继承生产 LAN/NAS 写权限。

## 服务与入口

| 服务 | 端口 | 所有权 |
|---|---|---|
| CRM 管理端 | 8780 | 本生产控制器，保留 LAN |
| 家长表单端 | 8781 | 本生产控制器，localhost 供既有隧道访问 |
| 内容提取 | 8792 | 本生产控制器 |
| 地图 | 8793 | 本生产控制器 |
| Agent Reach | 8790 | 原独立后端，本控制器仅检查 |

管理端：`http://127.0.0.1:8780/login`；局域网：`http://192.168.1.123:8780/login`。
家长原公网域名：`https://form.goabroad.com.au`，须通过实际表单/签约链接访问；根路径返回 404 是既有路由设计。

Agent Reach 的数据库仍在独立候选部署的 `var/data/xhs-intelligence.sqlite3`，未替换为导入档案或受限测试后端。其原进程、浏览器配置、OpenCLI、TrendRadar 依赖及调度边界保持不变。`config/external-agent.json` 固定已核实的 PID、出生时间及命令；独立后端重启后须重新核验并更新身份，CRM 不会擅自信任新 PID，也不会启动或停止该后端。

## 隔离与维护门禁

macOS 原生沙箱限制源码只读，写入限定数据目录及显式授权的 NAS 根；`/Volumes/Server/Students/2026` 仍受保护。停止前核对进程出生时间、完整命令、数据根、配置摘要与所属进程组，不向外来进程发信号。

生产门禁位于 `config/business-write-gate`。UNKNOWN 状态拒绝生产准入；CLOSED 时允许受限读取与登录，业务请求拒绝。切换已先 CLOSED 验证，再以正确 generation 开放。当前 OPEN。不得删除门禁文件来绕过维护。

内容提取使用已安装只读依赖及本地 Paddle OCR，不自动安装软件或开启云端 OCR/转录。新服务保留原隔离运行时的后台任务禁用策略；未修改系统 LaunchAgent 或 Cloudflare 隧道。

## 切换证据与恢复边界

旧三个 CRM 进程以匹配身份的 SIGTERM 停止，随后取得最终数据库快照和文件副本，显式完成 043 迁移，并只改写活动文件指针。所有业务表行数不变，只有 schema_migrations 从 42 增至 43；学生176、家长344、合同222、学校1079、账号7。账号密码与角色保持原值；验收登录会正常更新登录时间和审计记录。

- 最终准备清单：`runtime/production/PREPARATION.json`
- 迁移前数据库：`runtime/production/backups/before-cutover-*.db`
- 验收报告：`runtime/production/reports/cutover-closed-verification.json`、`cutover-business-verification.json`、`production-auxiliary-acceptance.json`、`network-acceptance.json`
- 冻结代码哈希、旧进程及回退命令：`runtime/cutover-20261101/`
- 原准备目录和上线前探测副本：`runtime/archives/`
- 演练实例：`runtime/staging/production-rehearsal-20261101`，验收后已停止，数据与证据保留。

`writes-opened.json` 已记录开放写入。现在不能直接恢复旧数据库快照；若需回退，须先保存新业务数据并做数据收敛。旧部署文件保留，但旧服务保持停止，禁止双写。原旧部署 PID 记录不再代表存活进程。

已验证登录、学生列表/详情、合同/报告页面、5个真实附件、2份报告及1份合同下载内容、OCR、Agent 只读连接、局域网与浏览器公网健康身份。命令行公网请求被 Cloudflare 1010 拒绝，正常浏览器访问已通过。

保留的已有问题：Agent 热点运行历史存在 backend_response_invalid，旧网关处理同一数据也失败；其他四项网关读取正常。43条上传暂存路径在原库中已缺失，按历史状态保留；审计与不可变报告快照未盲目重写。微信 OAuth App ID/Secret 原本未配置，本次未新增，不能声称已验证真实微信授权或签约。

## 后续代码更新

本次修改未提交或推送。后续更新前需审查并按授权整合本地修改，不要强制 reset。源码与数据分离不等于数据库迁移可自动跳过，更新后的迁移仍需独立备份和批准。原 Git AppleDouble 元数据警告未处理。
