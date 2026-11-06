> 迁移恢复说明（2026-11-05）：本文从旧 CRM workspace 恢复，用作历史功能保护清单。旧状态或路径不代表当前验收结果；当前规则以项目根 AGENTS.md 和用户确认内容为准，完成状态须看本轮实际验证记录。

# CRM Regression Checklist

Use this checklist with `scripts/regression_check.py` and manual browser checks.

## 测试 A：学生信息表流程

1. 生成学生信息表链接
2. 家长填写
3. 点击确认并提交
4. 后台收到数据
5. 自动生成 Word
6. Word 文件名正确
7. 文件归档到 Application
8. 预览和下载正常

Automated entry:

```bash
python3 scripts/regression_check.py --student-info
```

## 测试 B：合同流程

1. 生成合同
2. 生成合同链接
3. 家长微信授权
4. 家长在线签名
5. 签署后生成 signed contract
6. 家长签名出现在乙方签署栏
7. 最后一页合同签署确认页保留
8. 签名不出现在合同正文中间
9. 合同归档到 Application
10. 预览和下载正常
11. 原链接失效

Automated entry:

```bash
python3 scripts/regression_check.py --contract
```

Manual focus: real WeChat OAuth remains manual unless a test OAuth identity is configured.

## 测试 C：学校方案流程

1. 生成学校方案
2. 生成家长查看链接
3. 微信授权
4. 手机端查看
5. 水印正常但不遮挡
6. 学校对比看板手机端不横向滑动
7. 家长排序
8. 家长签名
9. 生成最终确认方案
10. 归档到 Application
11. 旧版本链接失效

Automated entry:

```bash
python3 scripts/regression_check.py --school-plan
```

Manual focus: real mobile device and real WeChat environment.

## 测试 D：文件预览流程

1. PDF 预览
2. DOCX 预览
3. 左侧缩略图
4. 页码跳转
5. 下载文件
6. 预览失败时显示友好提示

Automated entry:

```bash
python3 scripts/regression_check.py --preview
```

## 测试 E：页面交互

1. 在页面底部点击生成合同链接
2. 页面不跳到顶部
3. 在页面底部点击撤销链接
4. 页面不跳到顶部
5. 上传文件后不跳顶部
6. 删除文件后不跳顶部

Automated/static entry:

```bash
python3 scripts/regression_check.py --interaction
```

Manual focus: browser scroll position after bottom-page actions.

## Minimum Smoke Checklist

Run before reporting a feature change as ready:

```bash
python3 scripts/regression_check.py --core
```

Then run the suite matching the touched area.
