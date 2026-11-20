"""Offline gallery of actual browser captures and native/HTTP checkpoints."""
import hashlib,html,json,os,re
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1];DATA=ROOT/'STAGING_DATA/reports'
OUT=ROOT/'reports/student-status-four-surfaces-20261120';OUT.mkdir(exist_ok=True)
esc=lambda value:html.escape(str(value))
main=Path(json.loads((DATA/'four-surface-new-student-control.json').read_text())['resume_report'])
sources=[('新生从建档到下签',main,'main')]
for label,name in [('下签材料作废与恢复','four-surface-material-control.json'),('拒签、撤回、转学、升学','four-surface-branch-control.json')]:
    c=DATA/name
    if c.exists():sources.append((label,Path(json.loads(c.read_text())['report']),'branch'))
entries=[];manifest=[];images=0;all_passed=True
for title,path,kind in sources:
    assert path.resolve().is_relative_to(DATA)
    report=json.loads(path.read_text());all_passed &= report['status']=='passed'
    for n,step in enumerate(report['steps'],1):
        root=path.parent/('browser-observations' if kind=='main' else '')
        evidence=root/f'{n:02d}-four-surfaces.json'
        observed=json.loads(evidence.read_text()) if evidence.exists() else {}
        verified=observed.get('verified') is True;all_passed &= verified
        sid=step.get('student_id') or report.get('student_id')
        if kind=='main' and n==1:sid=None
        files={key:root/f'{n:02d}-{key}.jpg' for key in (('student','list','home') if sid else ('home',))}
        for p in files.values():
            if p.exists():
                manifest.append({'path':str(p),'bytes':p.stat().st_size,'sha256':hashlib.sha256(p.read_bytes()).hexdigest()});images+=1
            else:all_passed=False
        entries.append({'section':title,'index':n,'step':step,'observed':observed,'verified':verified,'student_id':sid,'files':files})
parts=['''<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width"><title>学生状态四处自动同步验收</title><style>body{font:16px/1.6 system-ui,sans-serif;max-width:1400px;margin:30px auto;padding:0 20px;background:#f3f6fa;color:#17263c}header,article{padding:24px;background:white;border:1px solid #d4dfed;border-radius:12px;margin:20px 0}.grid{display:grid;grid-template-columns:1fr 1fr;gap:18px}figure{margin:0}figure.home{grid-column:1/-1;max-width:1100px}img{max-width:100%;border:1px solid #d4dfed}figcaption{color:#425570}table{width:100%;border-collapse:collapse}td,th{padding:7px;border:1px solid #d4dfed;text-align:left}pre{white-space:pre-wrap;overflow-wrap:anywhere}summary{cursor:pointer}@media(max-width:800px){.grid{grid-template-columns:1fr}}</style>''']
parts.append(f'<header><h1>学生状态四处自动同步验收</h1><p><b>{"本轮全部检查点通过" if all_passed else "尚有流程或截图未完成"}</b>；仅隔离环境，未上线、合并或推送。</p><p>{len(entries)} 个检查点，{images} 张阶段截图。建档前的首个检查点仅核对首页；学生建立后每次核对学生详情、学生列表、首页六项统计、右下角阶段分布。详情/列表只在切换学生时导航，后续状态变化不手动刷新。</p><p>全部为新模拟学生和材料；无真实付款、邮件或学校/政府递交。转学、升学的旧合同及在读记录为明确前置条件；完整新生链另实际调用模拟签署归档。</p><p>另保留搜索交互失败/修复、取消勾选阻塞/修复及筛选自动退出的证据。早期诊断学生20260023不计入最终新生验收。</p></header>')
parts.append('<article><h2>最终页面补拍</h2><p>仅用于展示最终页面，不替代上面的历史阶段实拍。</p><ul>')
for name,label in [('final-list.jpg','最终学生列表'),('final-home.jpg','最终首页统计及阶段分布'),('tests.json','验证日志汇总')]:
    if (OUT/name).exists():parts.append('<li><a href="'+esc(name)+'">'+esc(label)+'</a></li>')
parts.append('</ul></article>')
last_section=None
for entry in entries:
    if entry['section']!=last_section:parts.append('<h2>'+esc(entry['section'])+'</h2>');last_section=entry['section']
    s=entry['step'];o=entry['observed'];home=o.get('home',{});label=s.get('student_label') or s.get('student_stage_label') or '尚未建立学生'
    parts.append(f'<article><h3>{entry["index"]:02d} · {esc(s["name"])}</h3><p>学生 {esc(entry["student_id"] or "未建档")}；学生页 <b>{esc(label)}</b>；原生/API及实际页面核对：{"通过" if entry["verified"] else "尚未完成"}。</p><div class="grid">')
    for key,p in entry['files'].items():
        label={'student':'学生详情页','list':'学生列表（固定搜索及排序）','home':'首页六项统计和阶段分布'}[key]
        if p.exists():
            url=esc(os.path.relpath(p,OUT));parts.append(f'<figure class="{key}"><a href="{url}"><img loading="lazy" src="{url}" alt="{label}"></a><figcaption>{label}</figcaption></figure>')
    parts.append('</div><h4>实际页面统计数字</h4><table><tr><th>统计</th><th>值</th></tr>')
    for key,value in home.get('metrics',{}).items():parts.append('<tr><td>'+esc(key)+'</td><td>'+esc(value)+'</td></tr>')
    parts.append('</table><p>阶段分布总数：'+esc(home.get('total','未核对'))+'</p><table><tr><th>阶段标识</th><th>在办周期</th></tr>')
    for key,value in home.get('counts',{}).items():parts.append('<tr><td>'+esc(key)+'</td><td>'+esc(value)+'</td></tr>')
    parts.append('</table><details><summary>原生业务/API与浏览器核对原始数据</summary><pre>'+esc(json.dumps({'source':s,'browser':o},ensure_ascii=False,indent=2))+'</pre></details></article>')
parts.append('<article><h2>专项交互证据</h2><ul>')
for filename in ['student-list-search-after-update-red.json','student-list-search-after-update-green.json','student-list-filter-auto-exit.json','student-list-filter-auto-exit.jpg','student-list-selection-stale-red.json','student-list-selection-stale-red.jpg','student-list-selection-auto-sync-green.json','student-list-selection-auto-sync-green.jpg','student-list-draft-preserved.json','student-list-draft-preserved.jpg','student-list-draft-discard-update.json','student-list-draft-discard-update.jpg']:
    p=ROOT/'reports'/filename
    if p.exists():parts.append('<li><a href="'+esc(os.path.relpath(p,OUT))+'">'+esc(filename)+'</a></li>')
parts.append('</ul><p><a href="changes.md">本轮修复范围与验证说明</a></p><h2>测试日志</h2><ul>')
for filename in ['four-surface-new-student-final-20261120.log','four-surface-material-20261120.log','four-surface-branches-20261120.log','four-surface-final-native-regression-20261120.log','four-surface-final-list-performance-20261120.log','four-surface-js-final-20261120.log','four-surface-list-css-20261120.log','four-surface-final-readback-load-20261120.log','four-surface-final-row-readback-20261120.log']:
    p=ROOT/'reports'/filename
    if not p.exists():continue
    text=p.read_text(errors='replace');m=re.search(r'Ran (\d+) tests? in ([\d.]+)s',text)
    status='失败' if re.search(r'^FAILED',text,re.M) else '通过' if re.search(r'^OK$|ℹ fail 0',text,re.M) else '尚未结束'
    parts.append('<li><a href="'+esc(os.path.relpath(p,OUT))+'">'+esc(filename)+'</a>：'+esc(status)+(('，'+m[1]+'项') if m else '')+'</li>')
parts.append('</ul><p>本报告验证学生状态及相关统计同步，不代表所有外部服务或生产升级已验收。原生和HTTP命令写入，真实浏览器独立读取；没有将事后重建画面当成历史截图。</p></article></html>')
(OUT/'index.html').write_text('\n'.join(parts));(OUT/'evidence-manifest.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2))
summary={'passed':bool(all_passed),'checkpoints':len(entries),'phase_images':images,'sources':[{'title':t,'path':str(p),'kind':k,'status':json.loads(p.read_text())['status']} for t,p,k in sources]}
(OUT/'verification.json').write_text(json.dumps(summary,ensure_ascii=False,indent=2));print(json.dumps({'path':str(OUT/'index.html'),**summary},ensure_ascii=False))
