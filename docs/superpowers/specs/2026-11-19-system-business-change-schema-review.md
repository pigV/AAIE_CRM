# 全系统变更机制：数据库结构评审稿

状态：仅为DDL提案，未应用到任何数据库；不在migrations/自动执行目录内。需授权后先用于本任务隔离测试库，生产迁移另行准备候选包和确认。

## 为什么需要新增结构
现有合同提醒事件只服务通知，财务outbox由Revenue事务拥有，不能拿来存全系统状态同步事件。仅用内存回调或浏览器广播，进程在业务提交后崩溃会丢通知。拟新增5张辅助表；不改现有学生、材料、合同、财务、工作流表结构，不复制材料正文。

## 1. business_change_commands：命令去重与结果
同一命令重试时核对request_hash及actor/type，返回已提交结果；不同内容复用相同key报冲突。command_key是服务端按命令类型、授权主体和客户端幂等键计算的SHA-256，不将令牌、正文、密码写入事件表。

```sql
CREATE TABLE business_change_commands (
 command_key TEXT PRIMARY KEY NOT NULL CHECK(length(command_key)=64),
 command_type TEXT NOT NULL CHECK(length(command_type) BETWEEN 1 AND 128),
 actor_kind TEXT NOT NULL CHECK(actor_kind IN ('user','parent','system','integration')),
 actor_ref TEXT NOT NULL CHECK(length(actor_ref) BETWEEN 1 AND 200),
 request_hash TEXT NOT NULL CHECK(length(request_hash)=64),
 result_json TEXT NOT NULL CHECK(json_valid(result_json)),
 committed_at TEXT NOT NULL
);
```
只存成功命令；与事实、审计、事件在同一外层事务内写入。result_json仅允许业务ID、版本和安全问题码，不保存HTTP请求/材料内容；权限检查在返回重试结果前再次执行。

## 2. business_change_events：不可变变更事实
```sql
CREATE TABLE business_change_events (
 seq INTEGER PRIMARY KEY AUTOINCREMENT,
 event_id TEXT NOT NULL UNIQUE,
 command_key TEXT NOT NULL REFERENCES business_change_commands(command_key) ON DELETE RESTRICT,
 event_type TEXT NOT NULL CHECK(length(event_type) BETWEEN 1 AND 128),
 source_kind TEXT NOT NULL CHECK(length(source_kind) BETWEEN 1 AND 64),
 source_id TEXT NOT NULL CHECK(length(source_id) BETWEEN 1 AND 200),
 source_version INTEGER NOT NULL CHECK(source_version>0),
 occurred_at TEXT NOT NULL,
 UNIQUE(source_kind,source_id,source_version,event_type)
);
CREATE INDEX business_change_events_command_idx ON business_change_events(command_key);
```
seq只作提交后扫描游标，不能作为某用户有权看到的全局变化详情。轮询端按授权主体和可见影响查询，不暴露全库活动量或未授权ID。

## 3. business_change_versions：主体的已提交版本
```sql
CREATE TABLE business_change_versions (
 subject_kind TEXT NOT NULL CHECK(length(subject_kind) BETWEEN 1 AND 64),
 subject_id TEXT NOT NULL CHECK(length(subject_id) BETWEEN 1 AND 200),
 version INTEGER NOT NULL CHECK(version>0),
 last_event_seq INTEGER NOT NULL REFERENCES business_change_events(seq) ON DELETE RESTRICT,
 updated_at TEXT NOT NULL,
 PRIMARY KEY(subject_kind,subject_id)
);
```
同一事件对相同主体只递增一次；关联调整时将旧、新主体均列入影响表并递增。此版本表示源事实提交版本，不冒充异步处理已完成版本。

## 4. business_change_impacts：精确影响范围
```sql
CREATE TABLE business_change_impacts (
 event_seq INTEGER NOT NULL REFERENCES business_change_events(seq) ON DELETE RESTRICT,
 subject_kind TEXT NOT NULL,
 subject_id TEXT NOT NULL,
 subject_version INTEGER NOT NULL CHECK(subject_version>0),
 relation TEXT NOT NULL CHECK(relation IN ('source','current','previous','both')),
 PRIMARY KEY(event_seq,subject_kind,subject_id),
 FOREIGN KEY(subject_kind,subject_id) REFERENCES business_change_versions(subject_kind,subject_id) ON DELETE RESTRICT
);
CREATE INDEX business_change_impacts_subject_idx
 ON business_change_impacts(subject_kind,subject_id,event_seq);
```
保存该次事件的主体版本；同一主体兼具旧新角色时合并为both，防止重复调度。命令必须使用当前事务验证每个主体的真实归属。

## 5. business_change_deliveries：处理状态、重试和租约
```sql
CREATE TABLE business_change_deliveries (
 event_seq INTEGER NOT NULL,
 handler_name TEXT NOT NULL CHECK(length(handler_name) BETWEEN 1 AND 64),
 handler_version TEXT NOT NULL CHECK(length(handler_version) BETWEEN 1 AND 64),
 subject_kind TEXT NOT NULL,
 subject_id TEXT NOT NULL,
 state TEXT NOT NULL CHECK(state IN ('pending','processing','done','failed','superseded')),
 attempts INTEGER NOT NULL DEFAULT 0 CHECK(attempts>=0),
 next_attempt_ms INTEGER NOT NULL DEFAULT 0 CHECK(next_attempt_ms>=0),
 lease_token TEXT,
 lease_until_ms INTEGER,
 dependency_keys_json TEXT NOT NULL DEFAULT '[]' CHECK(json_valid(dependency_keys_json)),
 last_error_code TEXT,
 processed_subject_version INTEGER CHECK(processed_subject_version>0),
 updated_at TEXT NOT NULL,
 PRIMARY KEY(event_seq,handler_name,handler_version,subject_kind,subject_id),
 FOREIGN KEY(event_seq,subject_kind,subject_id)
  REFERENCES business_change_impacts(event_seq,subject_kind,subject_id) ON DELETE RESTRICT,
 CHECK((state='processing' AND lease_token IS NOT NULL AND lease_until_ms IS NOT NULL)
    OR (state<>'processing' AND lease_token IS NULL AND lease_until_ms IS NULL))
);
CREATE INDEX business_change_deliveries_due_idx ON business_change_deliveries(state,next_attempt_ms,event_seq);
CREATE INDEX business_change_deliveries_lease_idx ON business_change_deliveries(state,lease_until_ms);
```
时间字段_ms为UTC epoch毫秒；业务日期/ISO时间继续沿用现有规则。依赖键包含完整event/handler版本/主体身份，不能因处理器升级错误解锁旧任务。租约领取、完成和重试用CAS防止过期worker提交；只有最新源版本允许写回。至少一次处理不等于外部副作用恰好一次，发邮件/财务动作仍使用原模块授权和独立幂等键。

## 事务、兼容与失败行为
1. 同一conn开启外层BEGIN IMMEDIATE，校验命令与expected_version，写源事实、必要同步状态和审计，再写命令结果、事件、主体版本、影响与待处理条目；外层提交前任何worker都不能看到事件。
2. Revenue的savepoint只能加入外层事务，不能在RELEASE savepoint时向外广播成功。家长端、后台、导入同样必须遵守这个边界。
3. 文件先暂存/校验，只有可靠落盘后才登记正式记录；DB失败时执行可追溯补偿，不删除既有或其他业务文件。文件和SQLite不虚称原子提交。
4. 核心工作流正确性由事务内规则/同源读取保证；队列仅处理允许异步的派生数据与刷新提示。worker停止不应令核心状态停留旧值。
5. 先加表，后部署兼容读取/监控，最后按接入表启用正式入口；上线前提供全量初始快照与版本起点，旧事实不伪造新事件。
6. 表缺失/版本错误时禁止启用已接入的新写路径；不在运行请求或服务启动中自动建表、迁移或回退到其他库。

## 回滚
停止本任务新增消费者、撤回本任务代码/接入开关并恢复原服务入口；新增5表保留，不删除已提交命令与事件、不回滚正常业务事实。旧代码忽略新增表，恢复新代码后可核查未处理任务并继续。若必须撤销表结构，应先证明未启用/无业务数据及无依赖，另行授权；不提供默认DROP TABLE回滚。

## 授权后在隔离库的必测项
新增结构与旧schema兼容；主键/唯一/FK约束；外层回滚无事件；重复命令同结果与冲突拒绝；多对象影响版本；过期租约CAS；处理器版本升级；崩溃恢复；失败可见；无权限轮询；并发/乱序；不发送真实邮件或写财务副作用。

当前测试只覆盖不依赖数据库的事件格式、依赖计划与源码盘点；未声称本DDL已执行或持久化故障恢复已通过。
