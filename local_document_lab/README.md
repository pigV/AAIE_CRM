# 本机申请文档识别验证原型

本目录保留本机识别实验；隔离开发 REVIEW 增强已通过根目录 application_document_extraction.py 接入，尚未生产发布。

- 解析：Docling 2.147.0，Apple Vision OCR（ocrmac），保留文字、表格和位置。
- 推理：MLX-LM，本地 Qwen3 4B Instruct 与 Qwen3 8B 的 4-bit 模型快照。
- 证据：字段必须有实际源块和原文引用。原文支持不代表业务含义正确，所有结果仍需人工审核。
- 隔离：处理进程由 sandbox-exec 禁止网络，禁止读取其他 CRM 目录，只能写 STAGING_DATA；任务 worker 不启动 HTTP 服务；隔离 CRM 页面可以在自己的回环端口运行。
- 资源：串行进程，30页/30MiB PDF上限，解析240秒/推理180秒，进程组RSS预算9GiB；宿主监测是周期检查，不能视为瞬时硬内存上限。
- 缓存：源PDF哈希、解析程序与环境/模型清单签名改变时重新解析。

公开模型下载单独执行，绝不把学生文件作为网络请求参数。HF、transformers 等在处理时固定离线状态，预下载资源缺失即失败，不回退云端。

所有虚拟环境、模型权重、PDF、结果、日志和性能指标保存在被 Git 忽略的 STAGING_DATA。不要提交它们。原型未读取生产数据库、未改生产服务。

运行器：从本独立工作目录使用 `python -B -m local_document_lab.run`。它会为子进程生成禁网配置；不要绕开运行器直接处理原件。运行前须完成依赖和模型预下载。

评估：`python -B -m local_document_lab.evaluate` 只评价人工已有依据的参考字段。已知回归样本及探索性样本不构成正式盲测；不能把少量已标字段的正确率表述为整个功能的准确率。

已知限制：跨页合并表格无法可靠还原单元格页码时，不冒填第一页坐标；相应字段会留待人工确认。原文中存在一个名称并不证明模型选对了学校角色，也不能单靠子串匹配证明名称完整，必须由参考标签或人工核对评估。

## 隔离开发 REVIEW 增强

开发入口仅允许本源码目录的 STAGING_DATA 运行根目录；需要本地 `STAGING_DATA/local-document-review.json` 内容包含 `{"enabled":true,"scope":"pdf-review-browser-20261105"}`，并且 web/broker 的 `AAIE_LOCAL_DOCUMENT_REVIEW_SCOPE` 与 scope 完全一致才启用。缺失时继续现有解析。模型 broker 在 web 沙箱之外单独运行：使用本机 Python 执行 `python -B -m application_document_extraction --serve`，每个真正处理进程由它启动到禁止网络的任务沙箱。

web 服务只提交/读取本地文件任务，不在 web 沙箱中嵌套启动 sandbox-exec。后台未启动、退出、超时、模型/来源校验失败均回退人工 REVIEW。本次隔离验收已创建 scoped 标记并启动独立测试 web/broker；生产未启用。不要把此说明当成生产部署命令。

最新验收与剩余限制见 `docs/superpowers/plans/2026-11-05-local-document-remaining-repairs-verification.md`。小学/中学阶段学校只作明确参考，实际业务保存学校仍以 Main Course / Host School 为准。

冻结依赖安装完成后，在只接触公开软件资源的离线准备环境中运行 `python -B -m local_document_lab.import_cache`。缓存仅含已安装库的公开元数据；处理 worker 会检查环境指纹，不匹配则使用原库路径。当前推理入口为 infer_v4，保留 v3 供历史回放；布局之外的页面文字仍保留且标明 page 级位置精度。

后续修复：CAAW 起止日期可通过同页最近明确 Welfare 标题补充独立语境证据；竞争标题阻止关联。命名语言课程的 from/to 范围只接受自身语句及地址连接，不借用后续课程日期。两项均保留人工审核。


2026-11-06新样本修复：原始文字层坏字体预检、逐页OCR分段、同页表格角色/日期关联、ISEC语言阶段及模型输入几何裁剪。来源包仍保留完整坐标；明确空值/竞争来源不会由后续恢复复活。最新20文件验证见 `docs/superpowers/plans/2026-11-06-new-pdf-sample-verification.md`；首次7份保留试验的失败保留，之后是已见样本回归，尚未生产上线。
