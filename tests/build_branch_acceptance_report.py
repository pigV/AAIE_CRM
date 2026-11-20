"""Build an offline evidence gallery; never imports the CRM app."""
import html
import json
import os
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
control=json.loads((ROOT/'STAGING_DATA/reports/branch-scenarios-control.json').read_text())
source=Path(control['report'])
assert source.resolve().is_relative_to(ROOT/'STAGING_DATA/reports')
r=json.loads(source.read_text())
out=ROOT/'reports/branch-evidence-20261120'
out.mkdir(exist_ok=True)
esc=lambda value:html.escape(str(value))
def media(path,label):
    if not path.exists():return '<p>无此阶段浏览器截图；仅有接口与业务数据核对。</p>'
    return f'<figure><a href="{esc(os.path.relpath(path,out))}"><img loading="lazy" src="{esc(os.path.relpath(path,out))}" alt="{esc(label)}"></a><figcaption>{esc(label)}</figcaption></figure>'
parts=['''<!doctype html><html lang="zh-CN"><meta charset="utf-8"><title>拒签、撤回、转学与升学补测</title>
<style>body{font:16px/1.65 system-ui,sans-serif;max-width:1280px;margin:36px auto;padding:0 24px;color:#172333;background:#f5f7fb}h1{font-size:28px}article,header{background:white;padding:24px;margin:22px 0;border:1px solid #d8dfec;border-radius:14px}.bad{color:#a01724}.grid{display:grid;grid-template-columns:1fr 1fr;gap:18px}img{max-width:100%;border:1px solid #d8dfec}figure{margin:0}figcaption{color:#596579}code{word-break:break-all}summary{cursor:pointer}table{border-collapse:collapse;width:100%}td,th{text-align:left;border:1px solid #d8dfec;padding:8px}li{margin:7px 0}</style>
<header><h1>拒签、撤回、转学与升学补测</h1><p class="bad"><b>结论：未全部通过。未部署生产。</b></p>
<p>原生业务回归 244 项通过；保留 14 个联调检查点。正式重新申请写入后，最终浏览器通过回退轮询更新，不能记为实时版本推送通过。</p>
<p>所有学生、签名文件、学校响应与业务结果均为隔离环境模拟数据。中途测试脚本前置条件出错的模拟记录也保留在隔离库，未混入生产。</p></header>''']
parts.append('<article><h2>发现的问题</h2><ul>'+''.join('<li>'+esc(x)+'</li>' for x in r['issues'])+'</ul>')
parts.append('<h2>阻断与验收范围</h2><ul>'+''.join('<li>'+esc(k+': '+v)+'</li>' for k,v in r.get('blocked_branches',{}).items())+''.join('<li>'+esc(x)+'</li>' for x in r['limits'])+'</ul></article>')
parts.append('<article><h2>修复建议（等待确认，尚未修改业务代码）</h2><ol><li>统一拒签终态在学生页、学生列表和工作流中的映射；取消“存在签证记录就推断已下签”的兜底。相关文件：student_workflow_projection.py、student_management_summary.py，核对 workflow_projection.py 的终态过滤。</li><li>在家长表单创建、重新生成及重新申请的原写事务中原子发布变更，确保学生与首页刷新版本一并递增。相关文件：parent_form_repository.py、parent_form_service.py 及 business_changes 对应适配器。</li><li>经单独确认后，为隔离测试账号配置转学、升学所需的测试权限及开关，补齐当前 Transfer V2 和升学的完整页面完成流程。保留历史周期并检查不重复计数。</li><li>新增失败回归转绿后，重复多页面并发测试，每次状态改变保存学生页与完整首页截图。仍不部署、不修改生产。</li></ol></article>')
parts.append('<article><h2>已确认的拒签页面异常</h2><div class="grid">'+media(out/'refused-student.jpg','模拟学生 20260008：拒签后显示申请阶段异常')+media(out/'refused-home.jpg','拒签后首页不增加下签人数，等待结果减一')+'</div></article>')
for i,s in enumerate(r['steps'],1):
    bp=source.parent/f'{i:02d}-browser.json'
    browser=json.loads(bp.read_text()) if bp.exists() else {}
    verified=browser.get('verified') is True
    note='浏览器画面与业务数据核对一致' if verified else '未完成此检查点的浏览器同步验收'
    if i==7:note+='；本检查点手动刷新，不能证明自动推送'
    if i==14:note+='；约 30 秒回退轮询后更新，刷新版本未递增'
    if i in (11,12):note+='；旧转学兼容路径，不是当前 Transfer V2 完整验收'
    parts.append(f'<article><h2>{i:02d} · {esc(s["name"])}</h2><p>模拟学生 <b>{esc(s["student_id"])}</b>；周期 {s["cycle_id"]}；学生页 {esc(s["student_label"])}；列表摘要 {esc(s["summary_label"])}。</p><p>在办归属 {s["membership"]} 次；首页总数 {s["total"]}；阶段 {esc(s["dashboard_stage"])}；周期 {esc(s["cycle_status"])} / {esc(s["outcome"])}；该学生不计入下签人数。</p><p>{esc(note)}</p><div class="grid">'+media(source.parent/f'{i:02d}-student.jpg','学生状态页')+media(source.parent/f'{i:02d}-home.jpg','首页上部统计及右下角阶段分布')+'</div><details><summary>原始检查数据</summary><pre>'+esc(json.dumps(s,ensure_ascii=False,indent=2))+'</pre></details></article>')
parts.append('</html>')
(out/'index.html').write_text('\n'.join(parts))
(out/'verdict.json').write_text(json.dumps(r,ensure_ascii=False,indent=2))
print(out/'index.html')
