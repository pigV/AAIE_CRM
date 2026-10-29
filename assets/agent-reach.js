(() => {
  "use strict";
  const root = document.querySelector("[data-ar-workspace]");
  if (!root) return;
  const pending = new Set();
  let trendsPlanBlocked = false;
  let keywordSearchPending = false;
  const active = new Set();
  const readable = new Set();
  const labels = {search: "搜索", trends: "趋势"};
  const states = {pending: "等待中", queued: "等待中", running: "运行中", success: "已完成", succeeded: "已完成", completed: "已完成", partial: "部分完成", failed: "运行失败", error: "运行失败"};
  const sources = {xiaohongshu: "小红书", xhs: "小红书", weibo: "微博", zhihu: "知乎", trendradar: "微博、知乎"};
  const select = (kind, source) => root.querySelector(`[data-ar-${kind}="${source}"]`);
  function message(source, text, error = false) {
    const target = select("message", source);
    target.textContent = text;
    target.className = error ? "notice error" : "notice";
  }
  async function request(path, options = {}) {
    const response = await fetch(`/agent-reach/api/${path}`, {
      credentials: "same-origin", redirect: "error", cache: "no-store", ...options
    });
    if (!response.ok) { const error = new Error("unavailable"); error.status = response.status; throw error; }
    return response.json();
  }
  function rows(target, values, columns, empty) {
    target.replaceChildren();
    if (!values.length) {
      const row = document.createElement("tr"), cell = document.createElement("td");
      cell.colSpan = columns.length || 3; cell.className = "empty"; cell.textContent = empty;
      row.append(cell); target.append(row); return;
    }
    values.forEach(value => {
      const row = document.createElement("tr");
      columns.forEach(column => {
        const cell = document.createElement("td"); cell.textContent = column(value) || "—";
        row.append(cell);
      }); target.append(row);
    });
  }
  function items(data) {
    if (!data || !Array.isArray(data.items)) throw new Error("invalid_response");
    return data.items;
  }
  let monitoringKeywords = null;
  let platformResults = [];
  let activeTrendTab = 'xhs';
  const searchView = {items: [], enriched: [], asOf: null, metadata: {}};
  function publicationDate(value) { return window.AgentReachTime.publication(value); }
  function rawSearchRank(item, keyword) {
    const rank = keyword ? item.keyword_search_ranks?.find(row => row.keyword === keyword)?.search_rank : item.latest_search_rank;
    return Number.isInteger(rank) && rank > 0 ? String(rank) : '未知';
  }
  function applySearchView() {
    if (!searchView.asOf) return;
    const settings = {sort:root.querySelector('[data-search-sort]').value,days:Number(root.querySelector('[data-search-days]').value),keyword:root.querySelector('[data-search-keyword]').value};
    const selected = window.AgentReachRanking.select(searchView.enriched,settings,searchView.asOf);
    rows(select('results','search'), selected, [
      item => item.title || '未知标题', item => item.author || '未知',
      item => item.keywords?.length ? item.keywords.join('、') : '未知',
      item => publicationDate(item.published_at),
      item => Number.isFinite(item.parsed_likes) ? String(item.parsed_likes) : '未知',
      item => rawSearchRank(item, settings.keyword),
      item => Number.isFinite(item.viral_potential_score) ? `${item.viral_potential_score} · ${item.viral_potential_band || '参考分'}` : '未知',
      item => item.viral_potential_labels?.length ? item.viral_potential_labels.join('；') : '暂无参考标签',
      item => item.last_seen ? localTime(item.last_seen) : '未知'
    ], '当前筛选范围没有已加载结果。未知发布日期的内容仅在“全部日期”中显示。');
    const total = Number.isInteger(searchView.metadata.total_count) ? `已保存结果共 ${searchView.metadata.total_count} 条；` : '已保存结果总量未知；';
    const more = searchView.metadata.has_more === true ? '仍有未加载结果。' : '';
    root.querySelector('[data-search-scope]').textContent = `${total}已加载 ${searchView.items.length} 条，当前显示 ${selected.length} 条，内容范围：${root.querySelector('[data-search-days]').selectedOptions[0].textContent}。${more}当前范围最多 100 条最近采集结果，不代表全部历史；排序与筛选仅使用已加载数据，不产生搜索请求。`;
  }
  for (const attribute of ['data-search-sort','data-search-days','data-search-keyword']) {
    root.querySelector(`[${attribute}]`).addEventListener('change', () => {
      if (attribute === 'data-search-keyword') {
        const item = monitoringKeywords?.find(k => k.keyword === root.querySelector('[data-search-keyword]').value);
        if (item) root.querySelector('[data-search-days]').value = item.content_range === 'all' ? '0' : (item.content_range || '30');
      }
      applySearchView();
    });
  }

  function enabledKeywords() { return (monitoringKeywords || []).filter(k => k.enabled).map(k => k.keyword); }
  function applyTrendsView() {
    const enabled = enabledKeywords();
    const filter = root.querySelector('[data-xhs-keyword]'), previous = filter.value;
    filter.replaceChildren();
    for (const keyword of ['',...enabled]) { const option = document.createElement('option'); option.value = keyword; option.textContent = keyword || '全部启用关键词'; filter.append(option); }
    filter.value = enabled.includes(previous) ? previous : '';
    const options = {sort:root.querySelector('[data-xhs-sort]').value,days:Number(root.querySelector('[data-xhs-days]').value),keyword:filter.value};
    const selected = searchView.asOf ? window.AgentReachRanking.filterXhs(searchView.enriched,options,searchView.asOf,enabled) : [];
    rows(root.querySelector('[data-xhs-results]'),selected,[r=>r.title || '未知标题',r=>r.author || '未知',r=>publicationDate(r.published_at),r=>Number.isFinite(r.parsed_likes)?String(r.parsed_likes):'未知',r=>r.keywords?.join('、') || '未知',r=>String(r.monitored_keyword_count ?? 0),r=>`${r.viral_potential_score} · ${r.viral_potential_band}`,r=>rawSearchRank(r,options.keyword),r=>localTime(r.last_seen)],monitoringKeywords === null ? '监控关键词暂时不可用，无法生成小红书趋势。' : '当前范围没有匹配启用关键词的已加载内容。');
    root.querySelector('[data-xhs-scope]').textContent = `已加载 ${searchView.items.length} 条搜索结果，当前显示 ${selected.length} 条小红书趋势；最多100条，不代表全部历史。未知发布时间不进入有限时间范围。`;
    root.querySelector('[data-xhs-controls]').hidden = activeTrendTab !== 'xhs';
    root.querySelector('[data-xhs-table]').hidden = activeTrendTab !== 'xhs';
    root.querySelector('[data-platform-table]').hidden = activeTrendTab === 'xhs';
    rows(select('results','trends'),platformResults.filter(r=>r.platform===activeTrendTab),[r=>r.title,r=>sources[r.platform],r=>localTime(r.last_seen)],'当前来源暂无已加载结果。');
    const recent = searchView.asOf ? window.AgentReachRanking.filterXhs(searchView.enriched,{sort:'potential',days:30,keyword:''},searchView.asOf,enabled) : [];
    const latest = values => values.filter(v=>window.AgentReachTime.format(v)!=='未知').sort((a,b)=>Date.parse(b)-Date.parse(a))[0];
    root.querySelector('[data-source-summary="xhs"]').textContent = monitoringKeywords === null ? '监控记录暂时不可用' : `启用关键词 ${enabled.length} 个；已加载近30天 ${recent.length} 条；其中高潜力 ${recent.filter(r=>r.viral_potential_score>=80).length} 条；最近采集：${localTime(latest(recent.map(r=>r.last_seen)))}`;
    for (const source of ['weibo','zhihu']) { const values=platformResults.filter(r=>r.platform===source); root.querySelector(`[data-source-summary="${source}"]`).textContent=`已加载 ${values.length} 条；最近更新：${localTime(latest(values.map(r=>r.last_seen)))}`; }
  }
  root.querySelectorAll('[data-trend-tab]').forEach(button=>button.addEventListener('click',()=>{activeTrendTab=button.dataset.trendTab;root.querySelectorAll('[data-trend-tab]').forEach(b=>b.setAttribute('aria-selected',String(b===button)));applyTrendsView();}));
  for (const attribute of ['data-xhs-days','data-xhs-sort','data-xhs-keyword']) root.querySelector(`[${attribute}]`).addEventListener('change',applyTrendsView);

  async function read(source) {
    const button = select("read", source); button.disabled = true;
    try {
      const [runData, resultData] = await Promise.all([request(`${source}/runs`), request(`${source}/results`)]);
      const runs = items(runData), results = items(resultData);
      readable.add(source);
      if (runs.some(run => ["pending", "queued", "running"].includes(run.status))) active.add(source);
      else active.delete(source);
      rows(select("runs", source), runs, [r => source === "search" ? r.keyword : "微博、知乎", r => states[r.status] || "状态待确认", r => localTime(r.finished_at || r.started_at)], "暂无运行记录");
      if (source === 'search') {
        searchView.items = results; searchView.metadata = resultData; searchView.asOf = new Date().toISOString();
        searchView.enriched = window.AgentReachRanking.prepare(results, searchView.asOf);
        const filter = root.querySelector('[data-search-keyword]'), previous = filter.value;
        filter.replaceChildren();
        const all = document.createElement('option'); all.value = ''; all.textContent = '全部关键词'; filter.append(all);
        const keywords = [...new Set(results.flatMap(item => item.keywords || []))].sort((a,b)=>a.localeCompare(b,'zh-CN'));
        keywords.forEach(keyword => { const option = document.createElement('option'); option.value = keyword; option.textContent = keyword; filter.append(option); });
        filter.value = keywords.includes(previous) ? previous : '';
        applySearchView(); applyTrendsView();
      } else { platformResults = results; applyTrendsView(); }
      select("state", source).textContent = runs.length ? (states[runs[0].status] || "状态待确认") : "尚未运行";
    } catch (_) {
      readable.delete(source);
      if (source === 'trends') { platformResults = []; applyTrendsView(); }
      message(source, `${labels[source]}记录暂时无法读取，请联系管理员。`, true);
      rows(select("runs", source), [], [], "记录暂时不可用");
      rows(select("results", source), [], source === 'search' ? Array(9).fill(()=> '') : [], "结果暂时不可用");
      if (source === 'search') {
        searchView.items = []; searchView.enriched = []; searchView.asOf = null; applyTrendsView();
        root.querySelector('[data-search-scope]').textContent = '结果暂时无法读取，未载入可供排序与筛选的数据。';
      }
    } finally {
      button.disabled = pending.has(source);
      select("submit", source).disabled = pending.has(source) || active.has(source) || !readable.has(source) || (source === 'trends' && trendsPlanBlocked) || (source === 'search' && keywordSearchPending);
    }
  }
  for (const source of ["search", "trends"]) {
    const form = select("form", source), button = select("submit", source);
    form.addEventListener("submit", async event => {
      event.preventDefault();
      if (pending.has(source) || active.has(source) || !readable.has(source) || (source === 'trends' && trendsPlanBlocked) || (source === 'search' && keywordSearchPending)) return;
      const keyword = source === "search" ? form.elements.keyword.value.trim() : "";
      if (source === "search" && !keyword) { message(source, "请输入关键词。", true); return; }
      pending.add(source); refreshMonitorButtons(); button.disabled = true; select("read", source).disabled = true;
      form.setAttribute("aria-busy", "true");
      message(source, source === "search" ? "正在准备搜索环境…" : "正在采集趋势，请稍候…");
      let phaseObserver = null;
      if (source === 'search') phaseObserver = window.AgentReachProgress.watch({
        read: signal => request('monitoring',{signal}),
        update: data => {
          if (!data || !Array.isArray(data.keywords) || !Array.isArray(data.tasks) || !data.trends) throw new Error('invalid');
          renderMonitoring(data); refreshMonitorButtons();
          const task = data.tasks.find(t=>t.kind==='keyword' && t.keyword===keyword && t.status==='running');
          const item = data.keywords.find(k=>k.keyword===keyword && k.running);
          if (task || item) message(source,window.AgentReachProgress.label('running',(task || item).execution_phase));
        },
        onStop: () => message(source,'搜索状态暂待确认，请等待当前操作返回；不要重复提交。')
      });
      try {
        await request(source, {method: "POST", headers: {"Content-Type": "application/json", "X-CSRF-Token": root.dataset.csrf}, body: JSON.stringify(source === "search" ? {keyword} : {})});
        message(source, `${labels[source]}已提交。可使用“更新显示”查看已保存的记录。`);
        await read(source);
      } catch (_) {
        readable.delete(source);
        message(source, `${labels[source]}状态待确认，请先更新显示确认运行记录，或联系管理员。请勿重复提交。`, true);
      } finally {
        phaseObserver?.stop();
        pending.delete(source); button.disabled = active.has(source) || !readable.has(source) || (source === 'trends' && trendsPlanBlocked) || (source === 'search' && keywordSearchPending); select("read", source).disabled = false;
        refreshMonitorButtons();
        form.setAttribute("aria-busy", "false");
      }
    });
    select("read", source).addEventListener("click", () => { if (!pending.has(source)) read(source); });
    read(source);
  }
  // Monitoring is a separate fixed capability. An unavailable plan service does
  // not remove existing manual Search/Trends access on the older backend.
  const monitor = {
    data: null, busy: false, reading: false, ready: false, trendsDirty: false,
    editor: root.querySelector('[data-keyword-editor]'),
    dialog: root.querySelector('[data-keyword-dialog]'),
    removeDialog: root.querySelector('[data-keyword-delete-dialog]'),
    trends: root.querySelector('[data-trends-plan-form]')
  };
  const weekdaysCN = ['周一','周二','周三','周四','周五','周六','周日'];
  function localTime(value) { return window.AgentReachTime.format(value); }
  function monitorMessage(text, error = false) {
    const element = root.querySelector('[data-monitor-message]');
    element.textContent = text; element.className = error ? 'notice error' : 'notice';
    if (monitor.dialog.open) {
      const feedback = root.querySelector('[data-keyword-editor-message]');
      feedback.textContent = text; feedback.className = error ? 'notice error' : 'notice';
    }
  }
  function trendsMessage(text, error = false) {
    const element = root.querySelector('[data-trends-plan-message]');
    element.textContent = text; element.className = error ? 'notice error' : 'notice';
  }
  function scheduleLabel(schedule) {
    if (!schedule) return '未设置';
    const base = {daily:'每天', weekdays:'工作日', weekly:'每周', interval_days:`每隔 ${schedule.interval_days} 天`, custom_weekdays:'每周'}[schedule.frequency] || '未设置';
    const days = ['weekly','custom_weekdays'].includes(schedule.frequency) ? ' ' + schedule.weekdays.map(day => weekdaysCN[day-1]).join('、') : '';
    return `${base}${days} ${schedule.time}（墨尔本）`;
  }
  function scheduleVisibility(form) {
    const mode = form.elements.frequency.value;
    form.querySelectorAll('[data-schedule-weekly]').forEach(e => { e.hidden = mode !== 'weekly'; });
    form.querySelectorAll('[data-schedule-custom]').forEach(e => { e.hidden = mode !== 'custom_weekdays'; });
    form.querySelectorAll('[data-schedule-interval]').forEach(e => { e.hidden = mode !== 'interval_days'; });
    form.elements.anchor_date.required = mode === 'interval_days';
  }
  function fillPlan(form, plan) {
    const schedule = plan.schedule || {frequency:'daily',time:'09:00',weekdays:[],interval_days:null,anchor_date:null,timezone:'Australia/Melbourne'};
    form.elements.enabled.checked = plan.enabled;
    form.elements.auto_enabled.checked = plan.auto_enabled;
    form.elements.frequency.value = schedule.frequency;
    form.elements.time.value = schedule.time;
    form.elements.weekday.value = String(schedule.weekdays[0] || 1);
    form.elements.interval_days.value = schedule.interval_days || 2;
    form.elements.anchor_date.value = schedule.anchor_date || '';
    form.querySelectorAll('[name="days"]').forEach(e => { e.checked = schedule.weekdays.includes(Number(e.value)); });
    scheduleVisibility(form);
  }
  function planFromForm(form) {
    const frequency = form.elements.frequency.value;
    const days = frequency === 'weekly' ? [Number(form.elements.weekday.value)] : frequency === 'custom_weekdays' ? [...form.querySelectorAll('[name="days"]:checked')].map(e => Number(e.value)) : [];
    if (frequency === 'custom_weekdays' && !days.length) throw new Error('请选择执行星期。');
    return {enabled: form.elements.enabled.checked, auto_enabled: form.elements.auto_enabled.checked,
      schedule: {frequency, time: form.elements.time.value, weekdays: days,
        interval_days: frequency === 'interval_days' ? Number(form.elements.interval_days.value) : null,
        anchor_date: frequency === 'interval_days' ? form.elements.anchor_date.value : null, timezone:'Australia/Melbourne'}};
  }
  function refreshMonitorButtons() {
    const locked = monitor.busy || monitor.reading || !monitor.ready;
    select('submit','trends').disabled = pending.has('trends') || active.has('trends') || !readable.has('trends') || trendsPlanBlocked;
    select('submit','search').disabled = keywordSearchPending || pending.has('search') || active.has('search') || !readable.has('search');
    root.querySelector('[data-keyword-new]').disabled = locked;
    root.querySelector('[data-trends-plan-save]').disabled = locked || Boolean(monitor.data?.trends.running);
    root.querySelector('[data-monitor-read]').disabled = monitor.busy || monitor.reading;
    root.querySelector('[data-keyword-editor-read]').hidden = monitor.ready;
    root.querySelector('[data-keyword-editor-read]').disabled = monitor.busy || monitor.reading;
    for (const form of [monitor.editor, monitor.trends]) {
      form.querySelectorAll('input,select').forEach(input => { input.disabled = monitor.busy; });
    }
    root.querySelector('[data-keyword-save]').disabled = locked;
    root.querySelectorAll('[data-keyword-action]').forEach(button => {
      const item = monitor.data?.keywords.find(k => k.id === Number(button.dataset.keywordId));
      button.disabled = locked || pending.has('search') || !item || item.running || (button.dataset.keywordAction === 'run' && !item.enabled);
    });
  }
  function renderMonitoring(data) {
    monitor.data = data; monitoringKeywords = data.keywords; applyTrendsView();
    trendsPlanBlocked = data.trends.running || !data.trends.enabled;
    const enabled = data.keywords.filter(item => item.enabled);
    const now = Date.now();
    const future = enabled.filter(item => item.auto_enabled && item.next_run_at && window.AgentReachTime.format(item.next_run_at) !== '未知' && Date.parse(item.next_run_at) > now).map(item => item.next_run_at).sort((a,b) => Date.parse(a)-Date.parse(b));
    const trendsNext = data.trends.enabled && data.trends.auto_enabled && window.AgentReachTime.format(data.trends.next_run_at) !== '未知' && Date.parse(data.trends.next_run_at) > now ? data.trends.next_run_at : null;
    const summaries = {total:data.keywords.length, enabled:enabled.length, automatic:enabled.filter(item=>item.auto_enabled).length, manual:enabled.filter(item=>!item.auto_enabled).length, 'next-search':localTime(future[0]), 'next-trends':localTime(trendsNext)};
    for (const [key,value] of Object.entries(summaries)) root.querySelector(`[data-monitor-summary="${key}"]`).textContent = String(value);
    const target = root.querySelector('[data-keyword-list]');
    target.replaceChildren();
    if (!data.keywords.length) rows(target, [], Array(6).fill(() => ''), '还没有关键词，点击“新增关键词”开始设置。');
    data.keywords.forEach(item => {
      const row = document.createElement('tr');
      const texts = [item.keyword, item.auto_enabled ? scheduleLabel(item.schedule) : '仅手动运行', item.running ? window.AgentReachProgress.label('running',item.execution_phase) : item.enabled ? '已启用' : '已暂停', `上次：${localTime(item.last_run_at)}　下次：${localTime(item.next_run_at)}`, item.last_status ? `${states[item.last_status] || '状态待确认'} · 新增 ${item.last_new_count ?? 0} 条` : '尚未运行'];
      texts.forEach(text => { const cell = document.createElement('td'); cell.textContent = text; row.append(cell); });
      const cell = document.createElement('td'), actions = document.createElement('div'); actions.className = 'ar-row-actions';
      for (const [action, label] of [['edit','编辑'],['toggle',item.enabled ? '暂停':'启用'],['run','立即运行'],['delete','删除']]) {
        const button = document.createElement('button'); button.type = 'button'; button.className = 'secondary'; button.textContent = label;
        button.dataset.keywordAction = action; button.dataset.keywordId = String(item.id); button.setAttribute('aria-label', `${label}关键词：${item.keyword}`); actions.append(button);
      }
      cell.append(actions); row.append(cell); target.append(row);
    });
    if (!monitor.trendsDirty) fillPlan(monitor.trends, data.trends);
    root.querySelector('[data-trends-plan-state]').textContent = data.trends.running ? '运行中' : !data.trends.enabled ? '已暂停' : data.trends.auto_enabled ? '自动采集已启用':'仅手动采集';
    root.querySelector('[data-trends-plan-times]').textContent = `上次执行：${localTime(data.trends.last_run_at)}　下次执行：${localTime(data.trends.next_run_at)}`;
    rows(root.querySelector('[data-monitor-tasks]'), data.tasks, [task => task.kind === 'trends' ? '微博、知乎趋势' : task.keyword || '关键词搜索', task => task.trigger === 'automatic' ? '自动':'手动', task => window.AgentReachProgress.label(task.status,task.execution_phase), task => localTime(task.started_at), task => localTime(task.finished_at), task => task.new_count == null ? '—' : String(task.new_count)], '暂无手动或自动任务记录');
  }
  async function readMonitoring() {
    if (monitor.busy || monitor.reading) return false;
    monitor.reading = true; refreshMonitorButtons();
    monitorMessage('正在更新计划显示…');
    try {
      const data = await request('monitoring');
      if (!data || !Array.isArray(data.keywords) || !Array.isArray(data.tasks) || !data.trends) throw new Error('invalid');
      renderMonitoring(data); monitor.ready = true;
      monitorMessage('计划记录已更新。自动执行按墨尔本时间安排。');
      trendsMessage(monitor.trendsDirty ? '已更新运行记录；您尚未保存的趋势设置已保留。' : '计划显示已更新。');
      return true;
    } catch (_) {
      monitor.ready = false; monitoringKeywords = null; applyTrendsView();
      root.querySelectorAll('[data-monitor-summary]').forEach(element => { element.textContent = '—'; });
      root.querySelector('[data-trends-plan-state]').textContent = '暂时无法读取';
      root.querySelector('[data-trends-plan-times]').textContent = '上次执行：—　下次执行：—';
      monitorMessage('计划管理尚未启用或暂时不可用，请联系管理员。即时搜索与趋势结果仍可在下方查看。', true);
      rows(root.querySelector('[data-keyword-list]'), [], Array(6).fill(() => ''), '计划暂时不可用');
      rows(root.querySelector('[data-monitor-tasks]'), [], Array(6).fill(() => ''), '任务记录暂时不可用');
      trendsMessage('计划读取失败，尚未保存的设置已保留。请更新计划显示后再保存。', true);
      return false;
    } finally { monitor.reading = false; refreshMonitorButtons(); }
  }
  async function mutateMonitoring(path, payload) {
    if (monitor.busy || monitor.reading || !monitor.ready || (path === 'keywords/run' && pending.has('search'))) return false;
    keywordSearchPending = path === 'keywords/run';
    monitor.busy = true; refreshMonitorButtons(); monitorMessage('正在处理，请勿重复操作…');
    const isSearchRun = path === 'keywords/run';
    let observer = null;
    if (isSearchRun) {
      monitorMessage('正在准备搜索环境…');
      observer = window.AgentReachProgress.watch({
        read: signal => request('monitoring',{signal}),
        update: data => {
          if (!data || !Array.isArray(data.keywords) || !Array.isArray(data.tasks) || !data.trends) throw new Error('invalid');
          renderMonitoring(data); refreshMonitorButtons();
          const item = data.keywords.find(k=>k.id===payload.id);
          if (item?.running) monitorMessage(window.AgentReachProgress.label('running',item.execution_phase));
        },
        onStop: () => monitorMessage('搜索状态暂待确认，请等待当前操作返回；不要重复提交。')
      });
    }
    const isTrends = path === 'trends';
    if (isTrends) trendsMessage('正在保存趋势计划，请勿重复操作…');
    let succeeded = false, taskFailed = false, resultStatus = null;
    try {
      const result = await request(`monitoring/${path}`, {method:'POST', headers:{'Content-Type':'application/json','X-CSRF-Token':root.dataset.csrf}, body:JSON.stringify(payload)});
      succeeded = true; resultStatus = result.task?.status; taskFailed = resultStatus === 'failed';
    } catch (error) {
      const rejected = error.status === 400;
      monitor.ready = rejected;
      const text = error.status === 400 ? '设置未被接受，请检查关键词是否重复以及计划日期、时间和星期。'
        : error.status === 403 ? '安全验证已过期或无权操作，请刷新页面后再试。'
        : error.status === 409 ? '任务运行中或设置发生冲突，请更新计划显示后再试。'
        : '操作状态待确认，请先更新计划显示确认状态，或联系管理员。请勿重复提交。';
      monitorMessage(text, true);
      if (isTrends) trendsMessage(text, true);
    } finally { observer?.stop(); keywordSearchPending = false; monitor.busy = false; refreshMonitorButtons(); }
    if (succeeded) {
      if (isTrends) monitor.trendsDirty = false;
      const refreshed = await readMonitoring();
      if (path !== 'keywords/run') {
        const label = isTrends ? '趋势计划' : path === 'keywords/delete' ? '关键词删除操作' : '关键词';
        const feedback = refreshed ? `${label}已保存。` : `${label}已保存，但未能更新显示，请更新计划显示确认。`;
        monitorMessage(feedback, !refreshed);
        if (isTrends) trendsMessage(feedback, !refreshed);
      }
      if (isSearchRun && !taskFailed) monitorMessage(window.AgentReachProgress.label(resultStatus,null) + '，可查看最近任务与已保存结果。');
      if (taskFailed) monitorMessage('运行失败，本次任务未完成，请查看最近任务或联系管理员。', true);
    }
    return succeeded;
  }
  root.querySelector('[data-monitor-read]').addEventListener('click', readMonitoring);
  root.querySelector('[data-keyword-editor-read]').addEventListener('click', readMonitoring);
  monitor.trends.addEventListener('input', () => {
    monitor.trendsDirty = true; trendsMessage('趋势设置尚未保存，请点击“保存趋势计划”。');
  });
  monitor.trends.addEventListener('change', () => {
    monitor.trendsDirty = true; trendsMessage('趋势设置尚未保存，请点击“保存趋势计划”。');
  });
  root.querySelector('[data-keyword-new]').addEventListener('click', () => {
    if (!monitor.ready || monitor.busy) return;
    monitor.editor.reset(); monitor.editor.elements.id.value = '';
    root.querySelector('[data-keyword-editor-message]').textContent = '';
    root.querySelector('#ar-keyword-dialog-title').textContent = '新增关键词'; scheduleVisibility(monitor.editor); monitor.dialog.showModal();
  });
  root.querySelectorAll('[data-keyword-cancel]').forEach(button => button.addEventListener('click', () => { if (!monitor.busy) monitor.dialog.close(); }));
  for (const form of [monitor.editor,monitor.trends]) form.elements.frequency.addEventListener('change', () => scheduleVisibility(form));
  monitor.editor.addEventListener('submit', async event => {
    event.preventDefault();
    let body;
    try { body = {...planFromForm(monitor.editor),keyword:monitor.editor.elements.keyword.value.trim(),content_range:monitor.editor.elements.content_range.value}; }
    catch (_) { monitorMessage('请选择有效的执行日期和星期。',true); return; }
    const id = Number(monitor.editor.elements.id.value);
    if (id) body.id = id;
    if (await mutateMonitoring(id ? 'keywords/update':'keywords',body)) monitor.dialog.close();
  });
  monitor.trends.addEventListener('submit', async event => {
    event.preventDefault();
    try { await mutateMonitoring('trends',planFromForm(monitor.trends)); }
    catch (_) { monitorMessage('请选择有效的执行日期和星期。',true); }
  });
  root.querySelector('[data-keyword-list]').addEventListener('click', async event => {
    const button = event.target.closest('[data-keyword-action]');
    if (!button || button.disabled || monitor.busy || !monitor.ready) return;
    const item = monitor.data.keywords.find(k => k.id === Number(button.dataset.keywordId));
    if (!item || item.running) return;
    const action = button.dataset.keywordAction;
    if (action === 'edit') {
      monitor.editor.elements.id.value = item.id; monitor.editor.elements.keyword.value = item.keyword;
      fillPlan(monitor.editor,item); monitor.editor.elements.content_range.value = item.content_range || '30'; root.querySelector('[data-keyword-editor-message]').textContent = ''; root.querySelector('#ar-keyword-dialog-title').textContent = '编辑关键词'; monitor.dialog.showModal();
    } else if (action === 'toggle') {
      await mutateMonitoring('keywords/update',{id:item.id,keyword:item.keyword,content_range:item.content_range || '30',enabled:!item.enabled,auto_enabled:item.auto_enabled,schedule:item.schedule});
    } else if (action === 'run') {
      if (item.enabled) await mutateMonitoring('keywords/run',{id:item.id});
    } else if (action === 'delete') {
      monitor.removeDialog.dataset.keywordId = String(item.id); monitor.removeDialog.returnValue = 'cancel';
      root.querySelector('[data-keyword-delete-name]').textContent = item.keyword; monitor.removeDialog.showModal();
    }
  });
  monitor.removeDialog.addEventListener('close', async () => {
    if (monitor.removeDialog.returnValue === 'confirm') await mutateMonitoring('keywords/delete',{id:Number(monitor.removeDialog.dataset.keywordId)});
  });
  readMonitoring();

})();
