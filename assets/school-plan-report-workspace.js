(() => {
  const reportStatusLabel = (value) => ({draft:'草稿',active:'当前有效',generated:'已生成',shared:'已分享',viewed:'已查看',confirmed:'已确认',rejected:'家长不同意',superseded:'已被新版本替代',revoked:'已撤销',expired:'已过期',invalidated:'已作废',archived:'已归档'}[String(value || '').toLowerCase()] || value);
  const root = document.querySelector("[data-school-plan-report-workspace]");
  if (!root) return;

  const endpoint = root.dataset.endpoint;
  const source = root.querySelector("[data-school-plan-report-workspace-source]");
  const master = root.querySelector(".school-plan-workspace__master");
  const detail = root.querySelector("[data-school-plan-detail-workspace]");
  const body = root.querySelector(".school-plan-workspace__body");
  const announcer = root.querySelector("[data-workspace-announcer]");
  if (!endpoint || !source || !master || !detail || !body) return;

  let state = {};
  try {
    state = JSON.parse(source.textContent || "{}");
  } catch (_) {
    state = {};
  }
  let controller = null;
  let searchTimer = null;
  let focusTarget = "";

  const sectorLabel = (value) => ({ public: "公校", private: "私校" }[value] || "待分类");
  const parentLabel = (value) => ({
    not_opened: "未打开",
    opened: "已打开",
    logged_in: "已登录",
    confirmed: "已确认",
    expired: "已过期",
    revoked: "已撤销",
    missing: "待生成链接",
  }[value] || "状态未知");
  const integer = (value) => Number.isFinite(Number(value)) ? Math.trunc(Number(value)) : 0;
  const text = (value, fallback = "") => String(value ?? fallback);
  const element = (tagName, className = "", content = null) => {
    const node = document.createElement(tagName);
    if (className) node.className = className;
    if (content !== null) node.textContent = text(content);
    return node;
  };
  const setData = (node, name, value = "") => {
    node.setAttribute(`data-${name}`, text(value));
    return node;
  };
  const appendText = (parent, tagName, className, value) => {
    const node = element(tagName, className, value);
    parent.appendChild(node);
    return node;
  };
  const safeInternalHref = (value) => {
    const href = text(value);
    return href.startsWith("/") && !href.startsWith("//") ? href : "#";
  };
  const identity = (report) => (
    `v${integer(report?.version)} · ${text(report?.city_name || "待确认城市")} · ${sectorLabel(report?.sector)}`
  );
  const planLabel = (report) => (
    `${sectorLabel(report?.sector)} · ${text(report?.city_name || "待确认城市")} · v${integer(report?.version)}`
  );

  const announce = (message) => {
    if (!announcer) return;
    announcer.textContent = message;
  };

  const params = () => {
    const value = new URLSearchParams();
    if (state.search) value.set("q", state.search);
    value.set("status", state.status_filter || "needs_followup");
    value.set("page", String(state.page || 1));
    if (state.selected_report_id) value.set("report", String(state.selected_report_id));
    return value;
  };

  const link = (label, href, className = "secondary button-link") => {
    const node = element("a", className, label);
    node.setAttribute("href", safeInternalHref(href));
    return node;
  };

  const unavailable = (label) => element("span", "school-plan-workspace__unavailable", label);

  const renderActions = (report) => {
    const actions = element("div", "school-plan-workspace__actions");
    if (report?.can_copy_link && report.share_url) {
      const copy = element("button", "primary", "复制家长链接");
      copy.type = "button";
      setData(copy, "workspace-copy", report.share_url);
      actions.appendChild(copy);
    } else {
      actions.appendChild(unavailable("待生成链接"));
    }
    actions.appendChild(link("查看详情", report?.detail_url));
    if (report?.download_url) {
      actions.appendChild(link("下载 PDF", report.download_url));
    } else {
      actions.appendChild(unavailable("PDF 未生成"));
    }
    return actions;
  };

  const renderMaster = (workspace) => {
    const fragment = document.createElement("div");
    fragment.className = "school-plan-workspace__master-content";

    const searchLabel = element("label", "school-plan-workspace__search");
    appendText(searchLabel, "span", "", "搜索学生");
    const search = element("input");
    search.type = "search";
    search.value = text(workspace.search);
    search.placeholder = "姓名或 学生编号";
    search.setAttribute("autocomplete", "off");
    setData(search, "workspace-search");
    searchLabel.appendChild(search);
    fragment.appendChild(searchLabel);

    const filters = element("div", "school-plan-workspace__filters");
    filters.setAttribute("aria-label", "方案状态筛选");
    (workspace.filters || []).forEach((item) => {
      const button = element("button", "", `${text(item.label)} `);
      button.type = "button";
      setData(button, "workspace-filter", item.key);
      button.setAttribute(
        "aria-pressed",
        item.key === workspace.status_filter ? "true" : "false",
      );
      appendText(button, "span", "", integer(item.count));
      filters.appendChild(button);
    });
    fragment.appendChild(filters);

    const list = element("div", "school-plan-workspace__master-list");
    list.setAttribute("role", "listbox");
    setData(list, "school-plan-master-list");
    const selectedGroup = workspace.selected?.group_key;
    (workspace.groups || []).forEach((item) => {
      const option = element("button");
      option.type = "button";
      option.setAttribute("role", "option");
      setData(option, "workspace-group");
      setData(option, "report-id", integer(item.summary_report_id));
      option.setAttribute(
        "aria-selected",
        item.group_key === selectedGroup ? "true" : "false",
      );
      appendText(option, "strong", "", item.student_name);
      appendText(
        option,
        "span",
        "",
        `${text(item.student_id)} · 申请 ${text(item.application_id)}`,
      );
      appendText(option, "small", "", item.summary_activity_display || "暂无家长活动");
      list.appendChild(option);
    });
    if (!(workspace.groups || []).length) {
      const empty = element("div", "school-plan-workspace__empty");
      setData(empty, "workspace-empty");
      appendText(empty, "p", "", "当前筛选没有学生。");
      const clear = element("button", "secondary", "清除筛选");
      clear.type = "button";
      setData(clear, "workspace-clear");
      empty.appendChild(clear);
      list.appendChild(empty);
    }
    fragment.appendChild(list);

    const pageCount = Math.max(1, integer(workspace.page_count));
    const currentPage = Math.max(1, integer(workspace.page));
    if (pageCount > 1) {
      const pagination = element("nav", "school-plan-workspace__pagination");
      pagination.setAttribute("aria-label", "学生学校方案分页");
      const previous = element("button", "secondary", "上一页");
      previous.type = "button";
      previous.disabled = currentPage <= 1;
      setData(previous, "workspace-page", Math.max(1, currentPage - 1));
      setData(previous, "workspace-page-direction", "previous");
      const status = element(
        "span",
        "school-plan-workspace__page-status",
        `第 ${currentPage} / ${pageCount} 页 · 共 ${integer(workspace.total_groups)} 位学生`,
      );
      status.setAttribute("tabindex", "-1");
      const next = element("button", "secondary", "下一页");
      next.type = "button";
      next.disabled = currentPage >= pageCount;
      setData(next, "workspace-page", Math.min(pageCount, currentPage + 1));
      setData(next, "workspace-page-direction", "next");
      pagination.append(previous, status, next);
      fragment.appendChild(pagination);
    }
    master.replaceChildren(fragment);
  };

  const metric = (label, value, note = "") => {
    const card = element("div");
    appendText(card, "span", "", label);
    appendText(card, "strong", "", value);
    if (note) appendText(card, "small", "", note);
    return card;
  };

  const labelledField = (className, label, content, note = "") => {
    const field = element("div", className);
    field.setAttribute("data-label", label);
    appendText(field, "span", "", content);
    if (note) appendText(field, "small", "", note);
    return field;
  };

  const renderRecord = (record) => {
    const current = Boolean(record?.is_current);
    const lifecycle = current ? "当前" : "已过期 · 保留记录";
    const row = element(
      "article",
      `school-plan-workspace__record${current ? " is-current" : " is-history"}`,
    );
    row.setAttribute("data-current", current ? "true" : "false");
    row.setAttribute("aria-label", `${identity(record)} · ${lifecycle}`);

    const version = element("div", "school-plan-workspace__record-version");
    version.setAttribute("data-label", "版本 / 城市 / 类别");
    appendText(version, "span", "school-plan-workspace__identity", identity(record));
    appendText(version, "span", "school-plan-workspace__record-badge", lifecycle);
    row.appendChild(version);
    row.appendChild(labelledField(
      "school-plan-workspace__record-state",
      "方案状态",
      reportStatusLabel(record?.status) || "状态未知",
      `家长状态：${parentLabel(record?.parent_status)}`,
    ));
    row.appendChild(labelledField(
      "school-plan-workspace__record-file",
      "PDF 文件",
      record?.has_pdf && record.file_name ? record.file_name : "PDF 未生成",
    ));
    row.appendChild(labelledField(
      "school-plan-workspace__record-activity",
      "家长查看",
      `${integer(record?.viewed_count)} 次查看`,
      record?.last_viewed_display || "尚未打开",
    ));
    row.appendChild(labelledField(
      "school-plan-workspace__record-confirmation",
      "家长确认",
      record?.confirmed_display ? `已确认 · ${text(record.confirmed_display)}` : "未确认",
    ));
    const recordActions = element("div", "school-plan-workspace__record-actions");
    recordActions.setAttribute("data-label", "操作");
    recordActions.appendChild(renderActions(record));
    row.appendChild(recordActions);
    return row;
  };

  const renderDetail = (workspace) => {
    const selected = workspace.selected;
    if (!selected) {
      const empty = element("div", "school-plan-workspace__detail-empty");
      appendText(empty, "h3", "", "没有可显示的学校方案");
      appendText(empty, "p", "", "清除筛选或新建学校方案。");
      detail.replaceChildren(empty);
      return;
    }

    const content = element("div", "school-plan-workspace__detail-content");
    const back = element("button", "school-plan-workspace__back secondary", "返回学生列表");
    back.type = "button";
    setData(back, "workspace-back");
    content.appendChild(back);

    const student = element("header", "school-plan-workspace__student");
    appendText(student, "h2", "", selected.student_name);
    appendText(
      student,
      "p",
      "",
      `学生编号 ${text(selected.student_id)} · 申请 ${text(selected.application_id)}`,
    );
    content.appendChild(student);

    const currentPlans = selected.current_plans || [];
    const selectedReport = selected.selected_report;
    if (currentPlans.length > 1) {
      const switcher = element("label", "school-plan-workspace__plan-switcher");
      appendText(switcher, "span", "", "当前方案");
      const select = element("select");
      setData(select, "workspace-plan");
      currentPlans.forEach((plan) => {
        const option = element("option", "", planLabel(plan));
        option.value = String(integer(plan.report_id));
        if (integer(plan.report_id) === integer(selectedReport?.report_id)) {
          option.selected = true;
          option.setAttribute("selected", "selected");
        }
        select.appendChild(option);
      });
      select.value = String(integer(selectedReport?.report_id));
      switcher.appendChild(select);
      content.appendChild(switcher);
    }

    const selectedActions = element("div", "school-plan-workspace__selected-actions");
    setData(selectedActions, "workspace-selected-actions");
    if (selectedReport) {
      const summary = element("div", "school-plan-workspace__selected-plan");
      appendText(summary, "h3", "", planLabel(selectedReport));
      const metrics = element("div", "school-plan-workspace__metrics");
      metrics.append(
        metric("学校数量", integer(selectedReport.school_count)),
        metric(
          "家长打开",
          `${integer(selectedReport.viewed_count)} 次 · ${parentLabel(selectedReport.parent_status)}`,
          selectedReport.last_viewed_display || "尚未打开",
        ),
        metric(
          "家长确认",
          selectedReport.confirmed_display || selectedReport.parent_status === "confirmed" ? "已确认" : "未确认",
          selectedReport.confirmed_display || "暂无确认时间",
        ),
        metric(
          "报告创建",
          selectedReport.created_by_name || "待确认",
          selectedReport.created_display || "暂无创建时间",
        ),
      );
      summary.appendChild(metrics);
      content.appendChild(summary);
      selectedActions.appendChild(renderActions(selectedReport));
    } else {
      const empty = element("div", "school-plan-workspace__selected-empty");
      appendText(empty, "h3", "", "暂无当前有效方案");
      appendText(empty, "p", "", "历史版本仅供查阅，不会作为当前方案操作。");
      content.appendChild(empty);
    }
    selectedActions.appendChild(link("新建方案", selected.create_url));
    content.appendChild(selectedActions);

    const records = element("section", "school-plan-workspace__records");
    records.setAttribute("aria-label", "方案版本记录");
    appendText(records, "h3", "", "方案记录");
    if ((selected.records || []).length) {
      selected.records.forEach((record) => records.appendChild(renderRecord(record)));
    } else {
      appendText(records, "p", "school-plan-workspace__records-empty", "暂无方案记录");
    }
    content.appendChild(records);
    detail.replaceChildren(content);
  };

  const restoreFocus = () => {
    if (focusTarget === "search") {
      root.querySelector("[data-workspace-search]")?.focus();
    } else if (focusTarget === "group") {
      root.querySelector('[data-workspace-group][aria-selected="true"]')?.focus();
    } else if (focusTarget === "plan") {
      root.querySelector("[data-workspace-plan]")?.focus();
    } else if (focusTarget.startsWith("filter:")) {
      const key = focusTarget.slice("filter:".length);
      const filters = Array.from(root.querySelectorAll("[data-workspace-filter]"));
      const requested = filters.find((item) => item.dataset.workspaceFilter === key);
      const active = filters.find((item) => item.getAttribute("aria-pressed") === "true");
      (requested || active || filters[0])?.focus();
    } else if (focusTarget.startsWith("page:")) {
      const direction = focusTarget.slice("page:".length);
      const pagers = Array.from(root.querySelectorAll("[data-workspace-page]"));
      const requested = pagers.find((item) => (
        item.dataset.workspacePageDirection === direction && !item.disabled
      ));
      const stable = pagers.find((item) => !item.disabled);
      (requested || stable || root.querySelector(".school-plan-workspace__page-status"))?.focus();
    }
    focusTarget = "";
  };

  const render = (workspace) => {
    renderMaster(workspace || {});
    renderDetail(workspace || {});
    restoreFocus();
  };

  const showLoading = () => {
    root.querySelector("[data-workspace-loading-state]")?.remove();
    const loading = element("div", "school-plan-workspace__loading");
    setData(loading, "workspace-loading-state");
    loading.setAttribute("aria-hidden", "true");
    loading.append(
      element("div", "school-plan-workspace__loading-master"),
      element("div", "school-plan-workspace__loading-detail"),
    );
    body.appendChild(loading);
  };

  const renderReadError = () => {
    const error = element("div", "school-plan-workspace__read-error");
    appendText(error, "h3", "", "暂时无法更新学校方案");
    appendText(error, "p", "", "当前学生列表已保留，请稍后重试。");
    const retry = element("button", "primary", "重试");
    retry.type = "button";
    setData(retry, "workspace-retry");
    error.appendChild(retry);
    detail.replaceChildren(error);
    announce("学校方案读取失败，可重试");
  };

  const load = async (patch = {}, requestedFocus = "") => {
    state = { ...state, ...patch };
    focusTarget = requestedFocus;
    controller?.abort();
    const requestController = new AbortController();
    controller = requestController;
    root.dataset.loading = "true";
    showLoading();
    try {
      const response = await fetch(`${endpoint}?${params()}`, {
        credentials: "same-origin",
        cache: "no-store",
        headers: { "Accept": "application/json", "X-Requested-With": "fetch" },
        signal: requestController.signal,
      });
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      const payload = await response.json();
      if (!payload.ok) throw new Error(payload.error || "read_failed");
      state = payload.workspace || {};
      render(state);
      history.replaceState(null, "", `${location.pathname}?${params()}`);
    } catch (error) {
      if (error?.name !== "AbortError") renderReadError();
    } finally {
      if (controller === requestController) {
        delete root.dataset.loading;
        root.querySelector("[data-workspace-loading-state]")?.remove();
      }
    }
  };

  const showCopyFallback = (value) => {
    root.querySelector("[data-workspace-copy-fallback]")?.remove();
    const fallback = element("div", "school-plan-workspace__copy-fallback");
    setData(fallback, "workspace-copy-fallback");
    fallback.setAttribute("role", "status");
    appendText(fallback, "p", "", "自动复制失败，请手动复制以下链接：");
    const input = element("input");
    input.type = "text";
    input.readOnly = true;
    input.setAttribute("readonly", "readonly");
    input.value = value;
    fallback.appendChild(input);
    detail.appendChild(fallback);
    input.focus();
    input.select();
  };

  const copyLink = async (button) => {
    const value = button.dataset.workspaceCopy;
    try {
      await navigator.clipboard.writeText(value);
      button.textContent = "已复制";
      announce("家长链接已复制");
      window.setTimeout(() => { button.textContent = "复制家长链接"; }, 1600);
    } catch (_) {
      showCopyFallback(value);
      announce("自动复制失败，请手动复制链接");
    }
  };

  root.addEventListener("input", (event) => {
    const search = event.target.closest?.("[data-workspace-search]");
    if (!search) return;
    window.clearTimeout(searchTimer);
    searchTimer = window.setTimeout(() => {
      load(
        { search: search.value.trim(), page: 1, selected_report_id: null },
        "search",
      );
    }, 250);
  });

  root.addEventListener("change", (event) => {
    const plan = event.target.closest?.("[data-workspace-plan]");
    if (!plan) return;
    load({ selected_report_id: integer(plan.value) || null }, "plan");
  });

  root.addEventListener("click", (event) => {
    const target = event.target;
    const back = target.closest?.("[data-workspace-back]");
    if (back) {
      root.dataset.mobileView = "list";
      const selected = root.querySelector('[data-workspace-group][aria-selected="true"]');
      (selected || root.querySelector("[data-workspace-group]"))?.focus();
      return;
    }
    const copy = target.closest?.("[data-workspace-copy]");
    if (copy) {
      copyLink(copy);
      return;
    }
    const retry = target.closest?.("[data-workspace-retry]");
    if (retry) {
      load();
      return;
    }
    const clear = target.closest?.("[data-workspace-clear]");
    if (clear) {
      load(
        { search: "", status_filter: "needs_followup", page: 1, selected_report_id: null },
        "search",
      );
      return;
    }
    const filter = target.closest?.("[data-workspace-filter]");
    if (filter) {
      load(
        { status_filter: filter.dataset.workspaceFilter, page: 1, selected_report_id: null },
        `filter:${filter.dataset.workspaceFilter}`,
      );
      return;
    }
    const page = target.closest?.("[data-workspace-page]");
    if (page && !page.disabled) {
      load(
        { page: integer(page.dataset.workspacePage) || 1 },
        `page:${page.dataset.workspacePageDirection || ""}`,
      );
      return;
    }
    const group = target.closest?.("[data-workspace-group]");
    if (group) {
      root.dataset.mobileView = "detail";
      load({ selected_report_id: integer(group.dataset.reportId) || null }, "group");
    }
  });

  root.dataset.mobileView = root.dataset.mobileView || "list";
  render(state);
})();
