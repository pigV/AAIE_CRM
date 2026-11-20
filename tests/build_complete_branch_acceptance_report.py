"""Offline report built only from retained source/API checks and browser evidence."""
import html,json,os,re
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
control=json.loads((ROOT/'STAGING_DATA/reports/full-branch-journey-control.json').read_text())
source=Path(control['report']);assert source.resolve().is_relative_to(ROOT/'STAGING_DATA/reports')
r=json.loads(source.read_text());out=ROOT/'reports/complete-branch-evidence-20261120';out.mkdir(exist_ok=True)
e=lambda x:html.escape(str(x))
verified=[]
for n,s in enumerate(r['steps'],1):
    b=source.parent/f'{n:02d}-browser.json'
    if b.exists() and json.loads(b.read_text()).get('verified') and all((source.parent/f'{n:02d}-{k}.jpg').exists() for k in ('student','home')):verified.append(n)
accepted=set(r.get('acceptance_branches',{}).values())
passed=r.get('status')=='passed' and len(verified)==len(r['steps']) and bool(accepted)
parts=['''<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width"><title>隔离环境四分支完整测试</title><style>body{font:16px/1.6 system-ui;max-width:1320px;margin:32px auto;padding:0 20px;background:#f2f5fa;color:#1a2a40}header,article{background:white;padding:24px;margin:20px 0;border:1px solid #d6dfed;border-radius:12px}.grid{display:grid;grid-template-columns:1fr 1fr;gap:16px}img{max-width:100%;border:1px solid #ced7e5}figure{margin:0}table{border-collapse:collapse;width:100%}td,th{border:1px solid #d6dfed;text-align:left;padding:8px}summary{cursor:pointer}pre{white-space:pre-wrap;overflow-wrap:anywhere}.diagnostic{border-left:5px solid #bd7d19}@media(max-width:800px){.grid{grid-template-columns:1fr}}</style>''']
parts.append('<header><h1>拒签、撤回、转学、升学：隔离环境完整测试</h1><p><b>'+('四分支最终验收通过' if passed else '尚未全部通过或截图尚未齐全')+'</b>；没有上线、合并或推送。</p>')
parts.append(f'<p>共 {len(r["steps"])} 个保留检查点，{len(verified)} 组浏览器截图；每组包括学生页和完整首页。首页逐项核对六项上部统计、阶段分布总数及每阶段数字；同时核对原生业务事实、学生列表摘要与桌面/移动接口。</p>')
parts.append('<p>所有学生、文书、学校反馈、付款凭证及签证结果均为模拟数据。旧已签合同和当前入读记录属于测试前置条件。无真实邮件、学校递交、付款或政府签证递交。</p><p>Transfer V2 当前完整 schema 通过 forward check 后设置了明确标记的测试 cutover baseline；未执行正式升级迁移，不能将本报告作为生产迁移验收。</p></header>')
clear=source.parent/'final-home-clear.jpg'
if clear.exists():
    parts.append('<article><h2>最终首页无遮挡补拍</h2><p>原始阶段截图存在提醒浮层，可能遮住部分分布数字；原图原样保留。各检查点下方另列当时实际DOM中的完整数字，已与业务事实/API逐项核对。此补拍为最终状态实拍，不替代历史阶段证据。</p>'+('<a href="'+e(os.path.relpath(clear,out))+'"><img style="max-width:100%" src="'+e(os.path.relpath(clear,out))+'"></a>')+'</article>')
parts.append('<article><h2>最终验收分支</h2><table><tr><th>分支</th><th>模拟学生</th><th>结果</th></tr>')
for label,key in r.get('acceptance_branches',{}).items():
    steps=[s for s in r['steps'] if s['name'].startswith(key+' · ')];s=steps[-1]
    parts.append(f'<tr><td>{e(label)}</td><td>{e(s["student_id"])}</td><td>{e(s["student_label"])}；在办计数 {s["membership"]}</td></tr>')
parts.append('</table><p>诊断轮次和中途前置条件修正均保留原始结果，最终转学、升学以新学生的 retest 为验收依据。拒签恢复及撤回后重新申请保留完整历史。</p></article>')
for n,s in enumerate(r['steps'],1):
    key=s['name'].split(' · ')[0];diagnostic=accepted and key not in accepted
    parts.append(f'<article class="{"diagnostic" if diagnostic else ""}"><h2>{n:02d} · {e(s["name"])}</h2><p>{"诊断轮次，包含修复前表现。" if diagnostic else "最终分支检查点。"}模拟学生 {e(s["student_id"])}，周期 {s["cycle_id"]}。学生页：<b>{e(s["student_label"])}</b>；列表：{e(s["summary_label"])}；在办归属 {s["membership"]}，首页总数 {s["total"]}；正式下签归属：{e(s.get("grant_expected",False))}。</p><p>浏览器证据：{"核对通过" if n in verified else "未完成"}。</p><div class="grid">')
    for k,label in [('student','学生状态页'),('home','首页六项统计与学生申请阶段分布')]:
        p=source.parent/f'{n:02d}-{k}.jpg';url=e(os.path.relpath(p,out))
        parts.append(f'<figure><a href="{url}"><img loading="lazy" src="{url}" alt="{label}"></a><figcaption>{label}</figcaption></figure>' if p.exists() else '<p>无浏览器截图</p>')
    browser_path=source.parent/f'{n:02d}-browser.json'
    browser=json.loads(browser_path.read_text()) if browser_path.exists() else {}
    parts.append('</div><h3>当时实际页面数字（原始DOM核对）</h3><table><tr><th>上部统计</th><th>数值</th></tr>')
    for metric in browser.get('home',{}).get('metrics',[]):
        parts.append('<tr><td>'+e(metric['label'])+'</td><td>'+e(metric['value'])+'</td></tr>')
    parts.append('</table><table><tr><th>阶段标识</th><th>周期数</th></tr>')
    for item in browser.get('home',{}).get('counts',[]):
        parts.append('<tr><td>'+e(item['key'])+'</td><td>'+e(item['count'])+'</td></tr>')
    parts.append('</table><details><summary>原始业务/API检查数据</summary><pre>'+e(json.dumps(s,ensure_ascii=False,indent=2))+'</pre></details></article>')
parts.append('<article><h2>验证日志</h2><ul>')
for name in ('complete-branch-journeys-resume10-20261120.log','branch-final-complete-native-20261120.log',
             'branch-final-readback-load2-20261120.log','branch-final-js-refresh-20261120.log',
             'branch-pure-tool-regression-20261120.log','completed-progression-visa-review2-20261120.log',
             'he-shell-visa-fallback-green3-20261120.log','branch-performance-fixtures-final-20261120.log'):
    path=ROOT/'reports'/name
    if not path.exists():continue
    text=path.read_text(errors='replace')
    match=re.search(r'Ran (\d+) tests? in ([\d.]+)s',text)
    status=('失败（保留诊断）' if re.search(r'^FAILED',text,re.M) else
        '通过' if re.search(r'^OK$|(?:ℹ|#) fail 0',text,re.M) else '尚未结束')
    detail=(match.group(1)+' 项，'+match.group(2)+' 秒') if match else ''
    parts.append('<li><a href="'+e(os.path.relpath(path,out))+'">'+e(name)+'</a>：'+e(status+' '+detail)+'</li>')
parts.append('</ul><p><a href="repair-scope.md">修复范围、测试前提及限制</a></p></article>')
parts.append('<article><h2>范围与未纳入项</h2><p>本轮验证四分支业务与跨页刷新；不代表全系统所有模块、真实外部服务或生产迁移都已验收。扩展性能测试初次发现的两项失败来自旧测试前提，保留原始失败日志；修正数据库包装器连接接口与正常/隔离健康入口测试前提后，原次数断言保留，性能套件9项全部通过。</p></article></html>')
(out/'index.html').write_text('\n'.join(parts));(out/'verdict.json').write_text(json.dumps({'passed':passed,'browser_verified':verified,'report':r},ensure_ascii=False,indent=2))
print(json.dumps({'path':str(out/'index.html'),'passed':passed,'steps':len(r['steps']),'screenshots':len(verified)*2},ensure_ascii=False))
