"""CRM-native business workspace. Remote values are rendered as text by the client."""
from html import escape


def _schedule_fields(prefix, *, trends=False):
    days = ''.join(f'<label class="ar-check"><input type="checkbox" name="days" value="{number}">{label}</label>' for number, label in enumerate(['周一','周二','周三','周四','周五','周六','周日'], 1))
    extra = '' if trends else '<option value="interval_days">每隔若干天</option><option value="custom_weekdays">自定义星期</option>'
    return f'''<div class="dialog-grid">
<label for="{prefix}-frequency">执行频率<select id="{prefix}-frequency" name="frequency"><option value="daily">每天</option><option value="weekdays">工作日（周一至周五）</option><option value="weekly">每周指定一天</option>{extra}</select></label>
<label for="{prefix}-time">执行时间（墨尔本时间）<input id="{prefix}-time" type="time" name="time" value="09:00" required></label>
<label data-schedule-weekly hidden>每周执行日<select name="weekday"><option value="1">周一</option><option value="2">周二</option><option value="3">周三</option><option value="4">周四</option><option value="5">周五</option><option value="6">周六</option><option value="7">周日</option></select></label>
<label data-schedule-interval hidden>每隔多少天<input name="interval_days" type="number" min="1" max="365" value="2"></label>
<label data-schedule-interval hidden>起始日期（墨尔本）<input name="anchor_date" type="date"></label>
<fieldset data-schedule-custom class="ar-weekdays" hidden><legend>执行星期（可多选）</legend>{days}</fieldset>
</div>'''


def _monitoring_workspace():
    return f'''<section aria-label="当前监控"><div class="panel-head"><h2>当前监控</h2><p>墨尔本时间（Australia/Melbourne）</p></div><div class="metrics ar-summary-grid"><div class="metric"><span>关键词总数</span><strong data-monitor-summary="total">—</strong></div><div class="metric"><span>启用关键词</span><strong data-monitor-summary="enabled">—</strong></div><div class="metric"><span>自动执行关键词</span><strong data-monitor-summary="automatic">—</strong></div><div class="metric"><span>仅手动关键词</span><strong data-monitor-summary="manual">—</strong></div><div class="metric"><span>下次搜索</span><strong class="ar-date" data-monitor-summary="next-search">—</strong></div><div class="metric"><span>下次趋势</span><strong class="ar-date" data-monitor-summary="next-trends">—</strong></div></div><p>自动与仅手动数量只统计已启用关键词，不含暂停项。</p></section><section class="panel" aria-labelledby="ar-monitor-title">
<div class="panel-head"><div><h2 id="ar-monitor-title">关键词管理</h2><p>保存关注的关键词，为每个关键词独立设置自动执行时间。所有时间均为墨尔本时间（Australia/Melbourne）。</p></div><div><button type="button" class="secondary" data-monitor-read>更新计划显示</button> <button type="button" class="primary" data-keyword-new disabled>新增关键词</button></div></div>
<p class="notice" role="status" aria-live="polite" data-monitor-message>正在读取计划…</p>
<div class="table-wrap ar-monitor-table"><table><thead><tr><th>关键词</th><th>自动计划</th><th>状态</th><th>上次执行 / 下次执行</th><th>最近结果</th><th>操作</th></tr></thead><tbody data-keyword-list><tr><td colspan="6" class="empty">正在读取关键词…</td></tr></tbody></table></div>
<dialog data-keyword-dialog aria-labelledby="ar-keyword-dialog-title"><form class="stack-form" data-keyword-editor>
<div class="dialog-head"><h2 id="ar-keyword-dialog-title">新增关键词</h2><button type="button" class="ghost" data-keyword-cancel aria-label="关闭关键词编辑">关闭</button></div>
<p class="notice" role="status" aria-live="polite" data-keyword-editor-message></p><button type="button" class="secondary" data-keyword-editor-read hidden>更新计划显示并确认状态</button><input type="hidden" name="id"><label for="ar-saved-keyword">关键词<input id="ar-saved-keyword" name="keyword" maxlength="120" required autocomplete="off"></label>
<label>默认内容范围<select name="content_range"><option value="7">最近7天</option><option value="30" selected>最近30天</option><option value="90">最近3个月</option><option value="180">最近6个月</option><option value="365">最近1年</option><option value="all">全部</option></select></label><p>内容范围仅筛选已采集结果，不限制平台采集时间；自动计划独立设置。</p><label class="ar-check"><input name="enabled" type="checkbox" checked>启用关键词（可手动运行）</label><label class="ar-check"><input name="auto_enabled" type="checkbox">启用自动执行</label><p>手动“立即运行”与自动计划分开。暂停的关键词不会自动执行。</p>
{_schedule_fields('ar-keyword-schedule')}
<div class="dialog-actions"><button type="button" class="secondary" data-keyword-cancel>取消</button><button type="submit" class="primary" data-keyword-save>保存关键词</button></div></form></dialog>
<dialog data-keyword-delete-dialog aria-labelledby="ar-keyword-delete-title"><form method="dialog"><div class="dialog-head"><h2 id="ar-keyword-delete-title">删除关键词</h2></div><p>删除后停止此关键词的自动计划，已采集的历史结果会保留。</p><p data-keyword-delete-name></p><div class="dialog-actions"><button value="cancel" class="secondary">取消</button><button value="confirm" class="primary">确认删除</button></div></form></dialog>
</section>
<section class="panel" aria-labelledby="ar-trends-plan-title"><div class="panel-head"><div><h2 id="ar-trends-plan-title">趋势自动采集</h2><p>微博、知乎使用独立于关键词的自动采集计划。</p></div><span class="status neutral" data-trends-plan-state>尚未读取</span></div>
<p class="notice" role="status" aria-live="polite" data-trends-plan-message></p><form class="stack-form" data-trends-plan-form><label class="ar-check"><input name="enabled" type="checkbox" checked>启用趋势采集</label><label class="ar-check"><input name="auto_enabled" type="checkbox">启用趋势自动采集</label>{_schedule_fields('ar-trends-schedule', trends=True)}<div><button class="primary" type="submit" data-trends-plan-save disabled>保存趋势计划</button></div></form><p data-trends-plan-times>上次执行：—　下次执行：—</p></section>
<section class="panel" aria-labelledby="ar-tasks-title"><div class="panel-head"><div><h2 id="ar-tasks-title">最近任务</h2><p>区分手动与自动执行，显示实际运行状态与本次新增结果。</p></div></div><div class="table-wrap ar-task-table"><table><thead><tr><th>任务</th><th>触发方式</th><th>状态</th><th>开始时间（墨尔本）</th><th>完成时间（墨尔本）</th><th>新增结果</th></tr></thead><tbody data-monitor-tasks><tr><td colspan="6" class="empty">正在读取最近任务…</td></tr></tbody></table></div></section>'''


def _search_results_workspace():
    return '''<h3>最近搜索结果</h3>
<p>时间范围仅筛选已采集结果；3个月、6个月、1年分别按90、180、365天计算，不限制平台搜索请求。</p><div class="ar-result-controls dialog-grid">
<label for="ar-result-sort">排序<select id="ar-result-sort" data-search-sort><option value="comprehensive">综合排序</option><option value="latest">最新发布</option><option value="likes">点赞最多</option><option value="potential">爆款潜力</option></select></label>
<label for="ar-result-days">内容时间范围<select id="ar-result-days" data-search-days><option value="7">最近7天</option><option value="30" selected>最近30天</option><option value="90">最近3个月</option><option value="180">最近6个月</option><option value="365">最近1年</option><option value="0">全部</option></select></label>
<label for="ar-result-keyword">关键词筛选<select id="ar-result-keyword" data-search-keyword><option value="">全部关键词</option></select></label>
</div><p class="notice" data-search-scope>已加载 0 条，当前范围最多 100 条最近采集结果，不代表全部历史。排序与筛选仅使用已加载数据。</p>
<p>综合排序按最新命中的搜索批次排列，再按该批次的原始名次排列；不是跨批次的平台总榜。爆款潜力是基于已采集数据的参考分，不代表实际传播效果。缺失的发布日期、点赞数或搜索名次显示为未知。缺失项不计分，不代表该内容实际热度低。点赞数为历史非空最大观测值，不是实时点赞或实时热度。</p>
<div class="table-wrap ar-search-results-table"><table><thead><tr><th>标题</th><th>作者</th><th>关键词</th><th>发布日期</th><th>已保存点赞数</th><th>原始搜索名次</th><th>爆款潜力分</th><th>参考标签</th><th>最近采集</th></tr></thead><tbody data-ar-results="search"><tr><td colspan="9" class="empty">正在读取最近结果…</td></tr></tbody></table></div>'''


def _source_summary():
    return '<section class="panel"><h2>来源摘要</h2><p>仅统计已加载结果，最多100条；时间均为墨尔本时间（Australia/Melbourne）。</p><div class="metrics ar-summary-grid">' + ''.join('<div class="metric"><span>'+label+'</span><p data-source-summary="'+source+'">正在读取…</p></div>' for source,label in [('xhs','小红书'),('weibo','微博'),('zhihu','知乎')]) + '</div></section>'


def _trends_results_workspace():
    return '''<h3>最近趋势结果</h3><div class="ar-source-tabs" role="tablist" aria-label="趋势来源"><button type="button" role="tab" aria-selected="true" data-trend-tab="xhs">小红书</button><button type="button" role="tab" aria-selected="false" data-trend-tab="weibo">微博</button><button type="button" role="tab" aria-selected="false" data-trend-tab="zhihu">知乎</button></div>
<div data-xhs-controls><p>小红书趋势基于 Agent Reach 监控关键词及已采集内容生成，不是小红书官方热榜。仅匹配当前启用关键词；切换来源、时间或排序不会重新搜索。</p><div class="dialog-grid"><label>内容时间范围<select data-xhs-days><option value="7">最近7天</option><option value="30" selected>最近30天</option><option value="90">最近3个月</option></select></label><label>排序<select data-xhs-sort><option value="potential">爆款潜力</option><option value="latest">最新</option><option value="likes">高赞</option><option value="multi">多关键词命中</option></select></label><label>监控关键词<select data-xhs-keyword><option value="">全部启用关键词</option></select></label></div><p data-xhs-scope>正在读取已保存数据…</p></div>
<div class="table-wrap ar-search-results-table" data-xhs-table><table><thead><tr><th>标题</th><th>作者</th><th>发布时间</th><th>已保存点赞</th><th>历史命中关键词</th><th>启用关键词命中数</th><th>爆款潜力</th><th>原始名次</th><th>最近采集</th></tr></thead><tbody data-xhs-results></tbody></table></div>
<div class="table-wrap" data-platform-table hidden><table><thead><tr><th>标题</th><th>来源</th><th>更新时间（墨尔本）</th></tr></thead><tbody data-ar-results="trends"><tr><td colspan="3" class="empty">正在读取最近结果…</td></tr></tbody></table></div>'''


def render_workspace(csrf):
    sections = []
    for source, title, action in [('search', '搜索', '运行搜索'), ('trends', '趋势', '采集趋势')]:
        fields = ('<label for="ar-keyword">关键词</label><input id="ar-keyword" name="keyword" maxlength="100" required autocomplete="off" placeholder="输入需要搜索的关键词">'
                  if source == 'search' else '<p>采集平台：<span class="status neutral">微博</span> <span class="status neutral">知乎</span></p>')
        results_section = _search_results_workspace() if source == 'search' else _trends_results_workspace()
        sections.append(f'''<section class="panel" aria-labelledby="ar-{source}-title">
<div class="panel-head"><div><h2 id="ar-{source}-title">{title}</h2><p>{'按关键词查找内容，查看最近结果。' if source == 'search' else '小红书来自已保存的监控内容；微博、知乎可独立采集。'}</p></div><span class="status neutral" data-ar-state="{source}">尚未运行</span></div>
<form class="stack-form" data-ar-form="{source}">{fields}<div><button class="primary" type="submit" data-ar-submit="{source}" disabled>{action}</button></div></form>
<p class="notice" role="status" aria-live="polite" data-ar-message="{source}"></p>
<div class="panel-head compact-head"><h3>最近运行状态</h3><button class="secondary" type="button" data-ar-read="{source}">更新显示</button></div>
<div class="table-wrap"><table><thead><tr><th>{'关键词' if source == 'search' else '采集平台'}</th><th>状态</th><th>{'运行时间' if source == 'search' else '最近更新时间'}</th></tr></thead><tbody data-ar-runs="{source}"><tr><td colspan="3" class="empty">正在读取最近运行记录…</td></tr></tbody></table></div>
{results_section}</section>''')
    return f'''<link rel="stylesheet" href="/assets/agent-reach.css">
<div class="agent-reach-workspace" data-ar-workspace data-csrf="{escape(str(csrf or ''), quote=True)}">
<p class="notice">在此运行搜索或采集趋势，并查看已保存的结果。运行期间请勿重复提交。</p>
<noscript><p class="notice error">请启用浏览器脚本后使用此工作台。</p></noscript>
{_source_summary()}
{_monitoring_workspace()}
{''.join(sections)}
<section class="panel" id="ar-deep" aria-labelledby="ar-deep-title"><div class="panel-head"><h2 id="ar-deep-title">深度内容</h2><span class="status neutral">暂未开放</span></div><p>深度内容功能暂未开放。</p><button type="button" disabled aria-disabled="true">暂未开放</button></section>
</div><script src="/assets/agent-reach-time.js" defer></script><script src="/assets/agent-reach-ranking.js" defer></script><script src="/assets/agent-reach-progress.js" defer></script><script src="/assets/agent-reach.js" defer></script>'''
