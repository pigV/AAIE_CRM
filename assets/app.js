(function () {
  const q = (selector, root = document) => root.querySelector(selector);
  const qa = (selector, root = document) => Array.from(root.querySelectorAll(selector));
  const cssEscape = (value) => {
    if (window.CSS && typeof window.CSS.escape === "function") return window.CSS.escape(value);
    return String(value || "").replace(/["\\]/g, "\\$&");
  };
  const escapeHtml = (value) => String(value ?? "").replace(/[&<>"']/g, (char) => ({
    "&": "&amp;",
    "<": "&lt;",
    ">": "&gt;",
    '"': "&quot;",
    "'": "&#39;",
  })[char]);

  qa("form[data-unsaved-form]").forEach((form) => {
    let dirty = false;
    let submitted = false;
    form.addEventListener("input", () => { dirty = true; });
    form.addEventListener("change", () => { dirty = true; });
    form.addEventListener("submit", () => { submitted = true; });
    window.addEventListener("beforeunload", (event) => {
      if (!dirty || submitted) return;
      event.preventDefault();
      event.returnValue = "";
    });
  });

  const sidebarToggle = q("[data-sidebar-toggle]");
  const sidebar = q(".sidebar");
  const sidebarDesktopQuery = window.matchMedia("(min-width: 768px)");
  let sidebarKeyboardNavigationPending = false;
  const clearSidebarKeyboardFocus = () => {
    sidebarKeyboardNavigationPending = false;
    sidebar?.removeAttribute("data-sidebar-keyboard-focus");
  };
  ["pointerdown", "mousedown", "touchstart"].forEach((eventName) => {
    document.addEventListener(eventName, clearSidebarKeyboardFocus, true);
  });
  document.addEventListener("keydown", (event) => {
    if (event.key === "Tab" && sidebarDesktopQuery.matches) sidebarKeyboardNavigationPending = true;
  }, true);
  sidebar?.addEventListener("focusin", () => {
    if (sidebarDesktopQuery.matches && sidebarKeyboardNavigationPending) {
      sidebar.setAttribute("data-sidebar-keyboard-focus", "true");
    }
  });
  sidebar?.addEventListener("focusout", (event) => {
    if (!sidebarDesktopQuery.matches) {
      clearSidebarKeyboardFocus();
      return;
    }
    if (event.relatedTarget instanceof Node && sidebar.contains(event.relatedTarget)) return;
    setTimeout(() => {
      if (!sidebar.contains(document.activeElement)) clearSidebarKeyboardFocus();
    }, 0);
  });
  sidebarDesktopQuery.addEventListener("change", () => {
    if (!sidebarDesktopQuery.matches) clearSidebarKeyboardFocus();
  });
  const setSidebarOpen = (open) => {
    document.body.classList.toggle("sidebar-open", Boolean(open));
    sidebarToggle?.setAttribute("aria-expanded", open ? "true" : "false");
  };
  sidebarToggle?.addEventListener("click", () => {
    setSidebarOpen(!document.body.classList.contains("sidebar-open"));
  });
  qa("[data-sidebar-close], .sidebar nav a").forEach((target) => {
    target.addEventListener("click", () => setSidebarOpen(false));
  });
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape") setSidebarOpen(false);
  });

  const topbarStatus = q("[data-topbar-status]");
  if (topbarStatus) {
    const healthBadge = q("[data-topbar-health]", topbarStatus);
    const healthSymbol = q("[data-topbar-health-symbol]", topbarStatus);
    const healthText = q("[data-topbar-health-text]", topbarStatus);
    const onlineCount = q("[data-topbar-online-count]", topbarStatus);
    const onlineNames = q("[data-topbar-online-names]", topbarStatus);
    const refreshTopbarStatus = async () => {
      try {
        const response = await fetch("/api/topbar-status", {
          credentials: "same-origin",
          headers: { "Accept": "application/json" },
          cache: "no-store",
        });
        if (!response.ok) return;
        const data = await response.json();
        const health = data.health || {};
        const activeUsers = data.active_users || {};
        healthBadge?.classList.toggle("warn", health.state !== "ok");
        if (healthSymbol) healthSymbol.textContent = health.symbol === "OK" ? "正常" : (health.symbol || (health.overall_ok ? "正常" : "!"));
        if (healthText) healthText.textContent = health.headline || "状态未知";
        if (onlineCount) onlineCount.textContent = String(activeUsers.count ?? 0);
        if (onlineNames) onlineNames.textContent = activeUsers.names_text || "暂无在线用户";
      } catch (_) {}
    };
    refreshTopbarStatus();
    setInterval(refreshTopbarStatus, 30000);
    document.addEventListener("visibilitychange", () => {
      if (!document.hidden) refreshTopbarStatus();
    });
  }

  qa("[data-student-followup-quick-add]").forEach((form) => {
    const details = form.closest("details");
    const cancel = q("[data-student-followup-quick-add-cancel]", form);
    const status = q("[data-student-followup-quick-add-status]", form);
    cancel?.addEventListener("click", (event) => {
      event.preventDefault();
      form.reset();
      if (status) status.textContent = "";
      if (details) details.open = false;
    });
    form.addEventListener("submit", async (event) => {
      event.preventDefault();
      event.stopPropagation();
      const submit = q('button[type="submit"]', form);
      if (submit?.disabled) return;
      if (submit) submit.disabled = true;
      if (status) status.textContent = "正在添加提醒…";
      try {
        const response = await fetch(form.action, {
          method: "POST",
          credentials: "same-origin",
          body: new FormData(form),
          headers: {
            "Accept": "application/json",
            "X-Requested-With": "fetch",
          },
        });
        let data;
        try {
          data = await response.json();
        } catch (_) {
          throw new Error("服务器返回了无法读取的响应，请重试。");
        }
        if (!response.ok || data.ok !== true) {
          throw new Error(data.message || `提交失败（${response.status}），请重试。`);
        }
        if (status) status.textContent = data.message || "留言和提醒已添加。";
        window.location.hash = "student-summary-reminders";
        window.location.reload();
      } catch (error) {
        if (status) {
          status.textContent = error?.message || "提交失败，请检查网络后重试。";
        }
      } finally {
        if (submit) submit.disabled = false;
      }
    });
  });

  const initContentExtractionProgress = () => {
    const terminalStatuses = new Set(["已完成", "失败", "需要上传文件", "待确认"]);
    qa("[data-content-extraction-progress]").forEach((root) => {
      if (root.dataset.pollInitialized === "true" || root.dataset.shouldPoll !== "true") return;
      root.dataset.pollInitialized = "true";
      const jobId = root.dataset.contentExtractionProgress || "";
      const statusUrl = root.dataset.statusUrl || `/content/extractions/${encodeURIComponent(jobId)}/status`;
      const statusEl = q("[data-progress-status]", root);
      const barEl = q("[data-progress-bar]", root);
      const messageEl = q("[data-progress-message]", root);
      const stepEls = new Map(qa("[data-progress-step]", root).map((el) => [el.dataset.progressStep, el]));

      const replaceDetailIfFresh = (data) => {
        if (!data.detail_html || !data.detail_updated_at) return false;
        const detail = document.querySelector(`[data-content-extraction-detail="${cssEscape(String(data.job_id || jobId))}"]`);
        if (!detail || detail.dataset.detailUpdatedAt === data.detail_updated_at) return false;
        detail.outerHTML = data.detail_html;
        initContentExtractionProgress();
        window.AccurateTranscriptCRMUI?.initAccurateTranscriptEditors?.(document);
        return true;
      };

      const poll = async () => {
        try {
          const response = await fetch(statusUrl, {
            method: "GET",
            credentials: "same-origin",
            cache: "no-store",
            headers: {
              "Accept": "application/json",
              "X-Requested-With": "fetch",
            },
          });
          if (!response.ok) throw new Error("status request failed");
          const contentType = response.headers.get("content-type") || "";
          if (!contentType.includes("application/json")) throw new Error("status response was not json");
          const data = await response.json();
          if (statusEl) statusEl.textContent = data.process_status || "待处理";
          if (barEl) barEl.style.width = `${data.progress_percent || 0}%`;
          if (messageEl) {
            const updated = data.updated_at ? ` 更新时间：${String(data.updated_at).slice(0, 19)}` : "";
            messageEl.textContent = `${data.error_message || data.progress_message || "正在处理。"}${updated}`;
          }
          (data.progress_steps || []).forEach((step) => {
            const el = stepEls.get(step.key);
            if (!el) return;
            el.className = `content-extraction-progress-step ${step.state || "pending"}`;
          });
          const replaced = replaceDetailIfFresh(data);
          if (terminalStatuses.has(data.process_status || "")) {
            root.classList.remove("is-processing");
            window.setTimeout(() => window.location.reload(), replaced ? 900 : 600);
            return;
          }
          root.classList.add("is-processing");
          if (replaced) return;
        } catch (_) {
          if (messageEl) messageEl.textContent = "进度读取失败，系统会继续尝试。";
        }
        window.setTimeout(poll, 1000);
      };

      window.setTimeout(poll, 1000);
    });
  };
  initContentExtractionProgress();

  const hydrateLazyImages = (root = document) => {
    qa("img[data-lazy-src]", root).forEach((image) => {
      const panel = image.closest("[data-panel]");
      if (panel && !panel.classList.contains("active")) return;
      image.src = image.dataset.lazySrc || "";
      image.removeAttribute("data-lazy-src");
    });
  };

  const preservePanelMinHeight = (panel, height) => {
    if (!panel || !height || height < 1) return 0;
    const rounded = Math.ceil(height);
    panel.style.minHeight = `${rounded}px`;
    panel.dataset.preservedMinHeight = String(rounded);
    return rounded;
  };

  const releasePanelMinHeightIfStable = (panel) => {
    if (!panel || !panel.dataset.preservedMinHeight) return;
    const preserved = Number(panel.dataset.preservedMinHeight || 0);
    if (!preserved || panel.scrollHeight >= preserved) {
      panel.style.minHeight = "";
      delete panel.dataset.preservedMinHeight;
    }
  };

  let initTransferSchoolPickers = () => {};

  const initDynamicPanel = (root = document) => {
    hydrateLazyImages(root);
    if (typeof initDateInputMasks === "function") initDateInputMasks(root);
    if (typeof initOfferPanel === "function") initOfferPanel(root);
    if (typeof initStudentDetailAjaxForms === "function") initStudentDetailAjaxForms(root);
    if (typeof initContractFeeForms === "function") initContractFeeForms(root);
    if (typeof initContractGuardianNames === "function") initContractGuardianNames(root);
    if (typeof initContractTypeForms === "function") initContractTypeForms(root);
    if (typeof initTransferSchoolPickers === "function") initTransferSchoolPickers(root);
  };

  const fetchLazyTabPanel = async (panel) => {
    const response = await fetch(panel.dataset.lazyTabUrl, {
      credentials: "same-origin",
      cache: "no-store",
      headers: { "Accept": "text/html", "X-Requested-With": "fetch" },
    });
    if (!response.ok) throw new Error("加载失败");
    return response.text();
  };

  const replaceLazyTabPanel = async (panel) => {
    panel.innerHTML = await fetchLazyTabPanel(panel);
    panel.dataset.lazyTabLoaded = "true";
    initDynamicPanel(panel);
  };

  const refreshLazyTabPanel = async (panel) => {
    if (!panel || !panel.dataset.lazyTabUrl) return false;
    preservePanelMinHeight(panel, panel.getBoundingClientRect().height);
    try {
      await replaceLazyTabPanel(panel);
      releasePanelMinHeightIfStable(panel);
      return true;
    } catch (_) {
      return false;
    }
  };

  const loadLazyTabPanel = async (panel) => {
    if (!panel || !panel.dataset.lazyTabUrl || panel.dataset.lazyTabLoaded === "true") {
      hydrateLazyImages(panel || document);
      return;
    }
    const status = q("[data-lazy-tab-status]", panel);
    panel.dataset.lazyTabLoading = "true";
    if (status) status.textContent = "正在加载...";
    const startedAt = performance.now();
    try {
      await replaceLazyTabPanel(panel);
      const elapsed = Math.round(performance.now() - startedAt);
      if (window.console && elapsed > 500) console.info("student tab fetch", panel.dataset.panel, `${elapsed}ms`);
      releasePanelMinHeightIfStable(panel);
    } catch (error) {
      panel.innerHTML = `<div class="notice error">内容加载失败，请刷新页面后重试。</div>`;
    } finally {
      panel.dataset.lazyTabLoading = "false";
    }
  };

  qa("[data-tabs]").forEach((tabs) => {
    const buttons = qa("[data-tab]", tabs);
    const panelScope = tabs.parentElement || document;
    const panels = qa("[data-panel]", panelScope);
    const open = (name, updateUrl = false) => {
      const activePanel = panels.find((panel) => panel.classList.contains("active"));
      const nextPanel = panels.find((panel) => panel.dataset.panel === name);
      if (nextPanel && nextPanel !== activePanel && nextPanel.dataset.lazyTabLoaded !== "true") {
        preservePanelMinHeight(nextPanel, activePanel?.getBoundingClientRect().height || 0);
      }
      buttons.forEach((button) => button.classList.toggle("active", button.dataset.tab === name));
      panels.forEach((panel) => panel.classList.toggle("active", panel.dataset.panel === name));
      panels.filter((panel) => panel.dataset.panel === name).forEach((panel) => loadLazyTabPanel(panel));
      if (updateUrl) {
        const url = new URL(window.location.href);
        url.searchParams.set("tab", name);
        window.history.replaceState(null, "", `${url.pathname}${url.search}${url.hash}`);
      }
    };
    buttons.forEach((button) => button.addEventListener("click", () => open(button.dataset.tab, true)));
    const requested = new URLSearchParams(location.search).get("tab");
    if (requested && q(`[data-tab="${cssEscape(requested)}"]`, tabs)) open(requested);
  });
  hydrateLazyImages(document);

  document.addEventListener("click", (event) => {
    const openButton = event.target.closest("[data-dialog-open]");
    if (openButton) {
      const dialog = document.getElementById(openButton.dataset.dialogOpen);
      if (dialog) dialog.showModal();
      return;
    }
    const closeButton = event.target.closest("[data-dialog-close]");
    if (closeButton) closeButton.closest("dialog")?.close();
  });

  const showContractDeleteNotice = (form, message, type = "error") => {
    const section = form.closest("#contracts-section") || form.closest("section") || document.body;
    let notice = q("[data-contract-delete-notice]", section);
    if (!notice) {
      notice = document.createElement("div");
      notice.dataset.contractDeleteNotice = "true";
      section.insertBefore(notice, section.firstElementChild || null);
    }
    notice.className = `notice ${type}`;
    notice.textContent = message;
  };

  const addEmptyContractRowIfNeeded = (tbody) => {
    if (!tbody || q("[data-contract-row]", tbody)) return;
    const table = tbody.closest("table");
    const colCount = qa("thead th", table).length || 1;
    const row = document.createElement("tr");
    row.innerHTML = `<td colspan="${colCount}" class="empty">暂无合同</td>`;
    tbody.appendChild(row);
  };

  qa("[data-contract-delete-form]").forEach((form) => {
    form.addEventListener("submit", async (event) => {
      event.preventDefault();
      const message = form.dataset.confirmMessage || "确定要删除这份合同吗？删除后该合同链接将失效，家长不能再访问或签署。";
      if (!window.confirm(message)) return;
      const button = q('button[type="submit"], button:not([type])', form);
      const originalText = button?.textContent || "";
      if (button) {
        button.disabled = true;
        button.textContent = "处理中...";
      }
      try {
        const response = await fetch(form.action, {
          method: "POST",
          body: new FormData(form),
          credentials: "same-origin",
          headers: { "Accept": "application/json", "X-Requested-With": "fetch" },
        });
        const data = await response.json().catch(() => ({}));
        if (!response.ok || data.ok === false) {
          throw new Error(data.message || "删除失败，请稍后重试或联系管理员");
        }
        showContractDeleteNotice(form, data.message || "合同已处理。", "success");
        const row = form.closest("[data-contract-row]") || form.closest("tr");
        const tbody = row?.parentElement;
        if (data.status === "Deleted") {
          row?.remove();
          addEmptyContractRowIfNeeded(tbody);
        } else {
          const badge = row ? q(".status", row) : null;
          if (badge) {
            badge.className = "status warn";
            badge.textContent = "已作废";
          }
          const firstCell = row ? q("td", row) : null;
          if (firstCell && !q(".contract-void-note", firstCell)) {
            firstCell.insertAdjacentHTML("beforeend", '<div class="contract-void-note">VOID / 已作废</div>');
          }
          form.remove();
        }
      } catch (error) {
        showContractDeleteNotice(form, error?.message || "删除失败，请稍后重试或联系管理员", "error");
        if (button) {
          button.disabled = false;
          button.textContent = originalText || "删除";
        }
      }
    });
  });

  qa("[data-file-drop-zone]").forEach((zone) => {
    const input = q('input[type="file"]', zone);
    const summary = q("[data-file-drop-summary]", zone);
    if (!input) return;
    const describeFiles = () => {
      const files = Array.from(input.files || []);
      if (!summary) return;
      if (!files.length) {
        summary.textContent = "未选择文件";
        return;
      }
      const names = files.slice(0, 3).map((file) => file.name).join("、");
      summary.textContent = files.length > 3 ? `${names} 等 ${files.length} 个文件` : names;
    };
    input.addEventListener("change", describeFiles);
    ["dragenter", "dragover"].forEach((eventName) => {
      zone.addEventListener(eventName, (event) => {
        event.preventDefault();
        zone.classList.add("is-dragover");
      });
    });
    ["dragleave", "drop"].forEach((eventName) => {
      zone.addEventListener(eventName, (event) => {
        event.preventDefault();
        zone.classList.remove("is-dragover");
      });
    });
    zone.addEventListener("drop", (event) => {
      const files = event.dataTransfer?.files;
      if (!files || !files.length) return;
      input.files = files;
      input.dispatchEvent(new Event("change", { bubbles: true }));
    });
    describeFiles();
  });

  qa("[data-school-state-filter]").forEach((stateSelect) => {
    const form = stateSelect.closest("form");
    const stateCategoryField = form ? q("[data-school-state-category-filter]", form) : null;
    const regionSelect = stateCategoryField ? q("[data-school-region-select]", stateCategoryField) : null;
    const qldPublicTypeSelect = stateCategoryField ? q("[data-school-qld-public-type-select]", stateCategoryField) : null;
    const sectorSelect = form ? q("[data-school-sector-filter]", form) : null;
    const yearPatternSelect = form ? q("[data-school-year-pattern-filter]", form) : null;
    if (!stateCategoryField && !yearPatternSelect) return;
    const syncStateSectorFilters = () => {
      const stateValue = String(stateSelect.value || "").trim().toUpperCase();
      const isPublic = String(sectorSelect?.value || "").trim().toLowerCase() === "public";
      const isNsw = stateValue === "NSW";
      const canUseWaRegion = stateValue === "WA";
      const canUseRegion = (isNsw || canUseWaRegion) && isPublic;
      const canUseQldPublicType = stateValue === "QLD" && isPublic;
      if (stateCategoryField) {
        stateCategoryField.hidden = !(canUseRegion || canUseQldPublicType);
      }
      if (regionSelect) {
        qa("option[data-region-state]", regionSelect).forEach((option) => {
          const optionState = String(option.getAttribute("data-region-state") || "").trim().toUpperCase();
          option.hidden = Boolean(optionState) && optionState !== stateValue;
        });
        regionSelect.hidden = !canUseRegion;
        regionSelect.disabled = !canUseRegion;
        const selectedOption = regionSelect.selectedOptions && regionSelect.selectedOptions[0];
        const selectedState = String(selectedOption?.getAttribute("data-region-state") || "").trim().toUpperCase();
        if (!canUseRegion || (selectedState && selectedState !== stateValue)) regionSelect.value = "";
      }
      if (qldPublicTypeSelect) {
        qldPublicTypeSelect.hidden = !canUseQldPublicType;
        qldPublicTypeSelect.disabled = !canUseQldPublicType;
        if (!canUseQldPublicType) qldPublicTypeSelect.value = "";
      }
      if (yearPatternSelect) {
        const isAct = stateValue === "ACT";
        qa("option", yearPatternSelect).forEach((option) => {
          const defaultLabel = option.getAttribute("data-default-label");
          const actLabel = option.getAttribute("data-act-label");
          const isActOption = option.getAttribute("data-act-year-pattern") === "true";
          if (defaultLabel) option.textContent = isAct && actLabel ? actLabel : defaultLabel;
          option.hidden = isAct && option.value !== "" && !isActOption;
          option.disabled = option.hidden;
        });
        const selected = yearPatternSelect.selectedOptions && yearPatternSelect.selectedOptions[0];
        if (isAct && selected && selected.value && selected.getAttribute("data-act-year-pattern") !== "true") {
          yearPatternSelect.value = "";
        }
      }
    };
    stateSelect.addEventListener("change", syncStateSectorFilters);
    sectorSelect?.addEventListener("change", syncStateSectorFilters);
    syncStateSectorFilters();
  });

  qa("[data-school-library-name-jump]").forEach((input) => {
    const listId = input.getAttribute("list");
    const list = listId ? document.getElementById(listId) : null;
    if (!list) return;
    const openSelectedSchool = () => {
      const selected = qa("option", list).find((option) => option.value === input.value);
      const schoolUrl = selected?.getAttribute("data-school-url");
      if (!schoolUrl) return;
      window.location.assign(schoolUrl);
    };
    input.addEventListener("input", openSelectedSchool);
    input.addEventListener("change", openSelectedSchool);
  });

  qa("[data-student-name-search]").forEach((input) => {
    const form = input.closest("form");
    const sourceId = input.dataset.studentSuggestionsId || "";
    const source = sourceId ? document.getElementById(sourceId) : null;
    const field = input.closest(".filter-search-field") || input.parentElement;
    const suggestionsRoot = field ? q("[data-student-search-suggestions]", field) : null;
    if (!form || !source || !suggestionsRoot) return;
    let suggestions = [];
    try {
      suggestions = JSON.parse(source.textContent || "[]");
    } catch (_) {
      suggestions = [];
    }
    let lastSubmittedValue = "";
    let activeIndex = -1;
    let visibleSuggestions = [];
    let renderTimer = 0;

    const closeSuggestions = () => {
      suggestionsRoot.hidden = true;
      suggestionsRoot.innerHTML = "";
      input.setAttribute("aria-expanded", "false");
      activeIndex = -1;
      visibleSuggestions = [];
    };

    const submitSelectedStudent = (student) => {
      if (!student || student.value === lastSubmittedValue) return;
      input.value = student.value || "";
      lastSubmittedValue = input.value;
      closeSuggestions();
      if (typeof form.requestSubmit === "function") {
        form.requestSubmit();
      } else {
        form.submit();
      }
    };

    const setActive = (index) => {
      activeIndex = index;
      qa("button", suggestionsRoot).forEach((button, buttonIndex) => {
        button.classList.toggle("active", buttonIndex === activeIndex);
      });
    };

    const renderSuggestions = () => {
      const query = input.value.trim().toLowerCase();
      if (query.length < 1) {
        closeSuggestions();
        return;
      }
      visibleSuggestions = suggestions
        .filter((item) => String(item.search || item.value || "").toLowerCase().includes(query))
        .slice(0, 8);
      if (!visibleSuggestions.length) {
        closeSuggestions();
        return;
      }
      suggestionsRoot.innerHTML = "";
      visibleSuggestions.forEach((item, index) => {
        const button = document.createElement("button");
        button.type = "button";
        button.dataset.studentSuggestionIndex = String(index);
        button.innerHTML = `<strong>${escapeHtml(item.value || "")}</strong><span>${escapeHtml(item.student_id || "")}</span>`;
        suggestionsRoot.appendChild(button);
      });
      suggestionsRoot.hidden = false;
      input.setAttribute("aria-expanded", "true");
      setActive(-1);
    };

    const scheduleRender = () => {
      window.clearTimeout(renderTimer);
      renderTimer = window.setTimeout(renderSuggestions, 120);
    };

    input.addEventListener("input", scheduleRender);
    input.addEventListener("focus", () => {
      if (input.value.trim().length >= 1) scheduleRender();
    });
    input.addEventListener("keydown", (event) => {
      if (suggestionsRoot.hidden || !visibleSuggestions.length) {
        if (event.key === "Escape") closeSuggestions();
        return;
      }
      if (event.key === "ArrowDown") {
        event.preventDefault();
        setActive(Math.min(activeIndex + 1, visibleSuggestions.length - 1));
      } else if (event.key === "ArrowUp") {
        event.preventDefault();
        setActive(Math.max(activeIndex - 1, 0));
      } else if (event.key === "Enter" && activeIndex >= 0) {
        event.preventDefault();
        submitSelectedStudent(visibleSuggestions[activeIndex]);
      } else if (event.key === "Escape") {
        closeSuggestions();
      }
    });
    suggestionsRoot.addEventListener("mousedown", (event) => {
      const button = event.target.closest("button[data-student-suggestion-index]");
      if (!button) return;
      event.preventDefault();
      submitSelectedStudent(visibleSuggestions[Number(button.dataset.studentSuggestionIndex)]);
    });
    document.addEventListener("click", (event) => {
      if (field && !field.contains(event.target)) closeSuggestions();
    });
    closeSuggestions();
  });

  qa("[data-school-radius-name-input]").forEach((input) => {
    const form = input.closest("form");
    const stateInput = form ? q("[data-school-radius-state-input]", form) : null;
    const listId = input.getAttribute("list");
    const list = listId ? document.getElementById(listId) : null;
    if (!form || !stateInput || !list) return;
    const syncRadiusStateFromSchool = () => {
      const selected = qa("option", list).find((option) => option.value === input.value);
      const state = selected?.getAttribute("data-school-state");
      if (state) stateInput.value = state.trim().toUpperCase();
    };
    input.addEventListener("input", syncRadiusStateFromSchool);
    input.addEventListener("change", syncRadiusStateFromSchool);
    syncRadiusStateFromSchool();
  });

  const initContractActionFilePickers = (root = document) => {
    qa("[data-contract-signed-file]", root).forEach((input) => {
      const card = input.closest("[data-contract-action-card]");
      const status = card ? q("[data-contract-file-name]", card) : null;
      if (!status) return;
      input.addEventListener("change", () => {
        const file = Array.from(input.files || [])[0];
        status.textContent = file?.name || "尚未选择文件";
      });
    });
  };
  // End contract action file pickers
  initContractActionFilePickers();

  qa('input[type="file"]').forEach((input) => {
    if (input.matches("[data-contract-signed-file]")) return;
    if (input.closest("[data-file-drop-zone]")) return;
    let zone = input.closest("label");
    if (!zone || zone.classList.contains("choice")) {
      zone = document.createElement("label");
      zone.className = "global-file-drop-zone";
      input.parentNode?.insertBefore(zone, input);
      zone.appendChild(input);
    } else {
      zone.classList.add("global-file-drop-zone");
    }
    input.classList.add("global-file-drop-input");
    let summary = q("[data-file-drop-summary]", zone);
    if (!summary) {
      summary = document.createElement("small");
      summary.dataset.fileDropSummary = "";
      summary.className = "global-file-drop-summary";
      summary.textContent = "可点击选择，也可拖拽文件到这里";
      zone.appendChild(summary);
    }
    const describeFiles = () => {
      const files = Array.from(input.files || []);
      if (!files.length) {
        summary.textContent = "可点击选择，也可拖拽文件到这里";
        return;
      }
      const names = files.slice(0, 3).map((file) => file.name).join("、");
      summary.textContent = files.length > 3 ? `${names} 等 ${files.length} 个文件` : names;
    };
    input.addEventListener("change", describeFiles);
    ["dragenter", "dragover"].forEach((eventName) => {
      zone.addEventListener(eventName, (event) => {
        if (input.disabled) return;
        event.preventDefault();
        zone.classList.add("is-dragover");
      });
    });
    ["dragleave", "drop"].forEach((eventName) => {
      zone.addEventListener(eventName, (event) => {
        event.preventDefault();
        zone.classList.remove("is-dragover");
      });
    });
    zone.addEventListener("drop", (event) => {
      if (input.disabled) return;
      const files = event.dataTransfer?.files;
      if (!files || !files.length) return;
      input.files = files;
      input.dispatchEvent(new Event("change", { bubbles: true }));
    });
    describeFiles();
  });

  const parseAustralianAddress = (address) => {
    const statePattern = /\b(ACT|NSW|NT|QLD|SA|TAS|VIC|WA)\b/i;
    const match = String(address || "").match(statePattern);
    if (!match) return null;
    const state = match[1].toUpperCase();
    const beforeState = String(address || "").slice(0, match.index).replace(/\s+/g, " ").trim();
    const afterState = String(address || "").slice(match.index + match[0].length);
    const postcodeMatch = afterState.match(/\b(\d{4})\b/);
    const parts = beforeState.split(",").map((part) => part.trim()).filter(Boolean);
    let suburb = parts.length ? parts[parts.length - 1] : "";
    suburb = suburb.replace(/\b(Australia|AU)\b/ig, "").replace(/\d{4}/g, "").trim();
    return {
      state,
      suburb,
      postcode: postcodeMatch ? postcodeMatch[1] : "",
    };
  };

  const schoolStateCertificateByState = {
    VIC: "VCE",
    NSW: "HSC",
    QLD: "QCE",
    WA: "WACE",
    SA: "SACE",
    TAS: "TCE",
    ACT: "ACT Senior Secondary Certificate",
    NT: "NTCET",
  };

  const schoolYearPatternLevels = {
    PrimaryOnly: ["Year 1", "Year 2", "Year 3", "Year 4", "Year 5", "Year 6"],
    Year7Plus: ["Year 7", "Year 8", "Year 9", "Year 10", "Year 11", "Year 12"],
    Year8Plus: ["Year 8", "Year 9", "Year 10", "Year 11", "Year 12"],
    Year9Plus: ["Year 9", "Year 10", "Year 11", "Year 12"],
    Year10Plus: ["Year 10", "Year 11", "Year 12"],
    Year11Plus: ["Year 11", "Year 12"],
    AllYears: ["Prep / Foundation", "Year 1", "Year 2", "Year 3", "Year 4", "Year 5", "Year 6", "Year 7", "Year 8", "Year 9", "Year 10", "Year 11", "Year 12"],
    PrepToYear12: ["Prep / Foundation", "Year 1", "Year 2", "Year 3", "Year 4", "Year 5", "Year 6", "Year 7", "Year 8", "Year 9", "Year 10", "Year 11", "Year 12"],
    Year1ToYear12: ["Year 1", "Year 2", "Year 3", "Year 4", "Year 5", "Year 6", "Year 7", "Year 8", "Year 9", "Year 10", "Year 11", "Year 12"],
    Year7ToYear12: ["Year 7", "Year 8", "Year 9", "Year 10", "Year 11", "Year 12"],
    Year9ToYear12: ["Year 9", "Year 10", "Year 11", "Year 12"],
    Year10ToYear12: ["Year 10", "Year 11", "Year 12"],
  };

  qa("[data-school-address-autofill]").forEach((form) => {
    const address = q("[data-school-address-input]", form);
    const state = q("[data-school-state-input]", form);
    const suburb = q("[data-school-suburb-input]", form);
    const postcode = q("[data-school-postcode-input]", form);
    const certificate = q("[data-school-state-certificate-input]", form);
    const city = q("[data-school-city-input]", form);
    const schoolType = q("[data-school-type-input]", form);
    const interviewRequired = q("[data-school-interview-required]", form);
    const yearPattern = q("[data-school-year-pattern]", form);
    const minYear = q("[data-school-year-min]", form);
    const maxYear = q("[data-school-year-max]", form);
    const privateFeeFields = q("[data-private-school-fees]", form);
    const publicFeeNote = q("[data-school-fee-public-note]", form);
    if (!address || !state || !suburb) return;
    const fill = (input, value) => {
      if (!input || !value) return;
      const lastAuto = input.dataset.autoFilled || "";
      if (input.value && input.value !== lastAuto) return;
      input.value = value;
      input.dataset.autoFilled = value;
    };
    const syncCertificate = () => {
      if (!certificate || !state.value) return;
      const suggested = schoolStateCertificateByState[String(state.value).trim().toUpperCase()];
      if (!suggested) return;
      const lastAuto = certificate.dataset.autoFilled || "";
      if (certificate.value && certificate.value !== "Unknown" && certificate.value !== lastAuto) return;
      certificate.value = suggested;
      certificate.dataset.autoFilled = suggested;
    };
    const syncSchoolInterviewDefault = () => {
      if (!interviewRequired) return;
      const lastAuto = interviewRequired.dataset.autoFilled || "";
      const stateValue = String(state?.value || "").trim().toUpperCase();
      const typeValue = String(schoolType?.value || "").trim().toLowerCase();
      const isPublic = typeValue === "public" || typeValue === "government" || typeValue.includes("public") || typeValue.includes("government");
      const yearNumber = (value) => {
        const match = String(value || "").match(/(\d+)/);
        return match ? Number(match[1]) : null;
      };
      const levels = schoolYearPatternLevels[yearPattern?.value] || [];
      const selectedLevels = qa('input[name="accepted_international_year_levels"]:checked', form).map((input) => input.value);
      const allLevels = levels.length ? levels : selectedLevels;
      const hasYear7Plus = allLevels.some((level) => {
        const number = yearNumber(level);
        return number !== null && number >= 7;
      }) || [minYear?.value, maxYear?.value].some((level) => {
        const number = yearNumber(level);
        return number !== null && number >= 7;
      });
      let suggested = "Unknown";
      let forced = false;
      if (isPublic && ["NSW", "SA", "QLD"].includes(stateValue)) {
        suggested = "No";
        forced = true;
      } else if (isPublic && stateValue === "VIC" && hasYear7Plus) {
        suggested = "Yes";
        forced = true;
      } else if (typeValue === "private") {
        suggested = "Yes";
      }
      if (!forced && interviewRequired.value && interviewRequired.value !== "Unknown" && interviewRequired.value !== lastAuto) return;
      interviewRequired.value = suggested;
      interviewRequired.dataset.autoFilled = suggested;
    };
    form.aaieSyncSchoolInterviewDefault = syncSchoolInterviewDefault;
    const syncPrivateFeeFields = () => {
      const isPrivate = String(schoolType?.value || "").trim().toLowerCase() === "private";
      if (privateFeeFields) privateFeeFields.hidden = !isPrivate;
      if (publicFeeNote) publicFeeNote.hidden = isPrivate;
    };
    const sync = () => {
      const parsed = parseAustralianAddress(address.value);
      if (!parsed) return;
      fill(state, parsed.state);
      fill(suburb, parsed.suburb);
      fill(postcode, parsed.postcode);
      syncCertificate();
      syncSchoolInterviewDefault();
    };
    address.addEventListener("input", sync);
    address.addEventListener("blur", sync);
    state.addEventListener("change", () => {
      syncCertificate();
      syncSchoolInterviewDefault();
    });
    state.addEventListener("input", () => {
      syncCertificate();
      syncSchoolInterviewDefault();
    });
    city?.addEventListener("change", syncSchoolInterviewDefault);
    schoolType?.addEventListener("change", () => {
      syncSchoolInterviewDefault();
      syncPrivateFeeFields();
    });
    sync();
    syncSchoolInterviewDefault();
    syncPrivateFeeFields();
  });

  qa("[data-school-year-pattern]").forEach((select) => {
    const form = select.closest("form");
    if (!form) return;
    const minSelect = q("[data-school-year-min]", form);
    const maxSelect = q("[data-school-year-max]", form);
    const accommodationSelect = q('select[name="accommodation_type"]', form);
    const customYearOptions = q("[data-school-custom-year-options]", form);
    const yearCheckboxes = () => qa('input[name="accepted_international_year_levels"]', form);
    const schoolYearOrder = Object.values(schoolYearPatternLevels)
      .flat()
      .filter((value, index, array) => value !== "Unknown" && array.indexOf(value) === index);
    const setPatternForLevels = (levels) => {
      const signature = levels.join("|");
      const match = Object.entries(schoolYearPatternLevels).find(([, values]) => values.join("|") === signature);
      if (match) select.value = match[0];
    };
    const syncYearBoundsFromCheckboxes = () => {
      const selected = yearCheckboxes().filter((input) => input.checked).map((input) => input.value);
      const ordered = schoolYearOrder.filter((level) => selected.includes(level));
      if (!ordered.length) return;
      if (minSelect) minSelect.value = ordered[0];
      if (maxSelect) maxSelect.value = ordered[ordered.length - 1];
      setPatternForLevels(ordered);
      form.aaieSyncSchoolInterviewDefault?.();
    };
    const syncYearCheckboxesFromBounds = () => {
      if (!minSelect || !maxSelect) return;
      const minIndex = schoolYearOrder.indexOf(minSelect.value);
      const maxIndex = schoolYearOrder.indexOf(maxSelect.value);
      if (minIndex < 0 || maxIndex < 0) return;
      const start = Math.min(minIndex, maxIndex);
      const end = Math.max(minIndex, maxIndex);
      const selected = new Set(schoolYearOrder.slice(start, end + 1));
      yearCheckboxes().forEach((input) => {
        input.checked = selected.has(input.value);
      });
      setPatternForLevels(Array.from(selected));
      form.aaieSyncSchoolInterviewDefault?.();
    };
    const toggleCustomYearOptions = () => {
      if (customYearOptions) customYearOptions.hidden = select.value !== "Custom";
    };
    const syncPrimaryAccommodation = () => {
      if (!accommodationSelect || select.value !== "PrimaryOnly") return;
      const suggested = "ParentOrGuardianOnly";
      const lastAuto = accommodationSelect.dataset.autoFilled || "";
      if (accommodationSelect.value && accommodationSelect.value !== "Unknown" && accommodationSelect.value !== lastAuto) return;
      accommodationSelect.value = suggested;
      accommodationSelect.dataset.autoFilled = suggested;
    };
    const syncYears = () => {
      toggleCustomYearOptions();
      const levels = schoolYearPatternLevels[select.value];
      if (!levels) return;
      const levelSet = new Set(levels);
      yearCheckboxes().forEach((input) => {
        input.checked = levelSet.has(input.value);
      });
      if (minSelect && levels[0]) minSelect.value = levels[0];
      if (maxSelect && levels[levels.length - 1]) maxSelect.value = levels[levels.length - 1];
      syncPrimaryAccommodation();
      form.aaieSyncSchoolInterviewDefault?.();
    };
    select.addEventListener("change", syncYears);
    minSelect?.addEventListener("change", () => {
      syncYearCheckboxesFromBounds();
      toggleCustomYearOptions();
    });
    maxSelect?.addEventListener("change", () => {
      syncYearCheckboxesFromBounds();
      toggleCustomYearOptions();
    });
    yearCheckboxes().forEach((input) => input.addEventListener("change", syncYearBoundsFromCheckboxes));
    toggleCustomYearOptions();
    syncPrimaryAccommodation();
  });

  qa("[data-school-language-availability]").forEach((select) => {
    const form = select.closest("form");
    const languageType = form ? q("[data-school-language-program]", form) : null;
    const syncLanguageType = () => {
      if (!languageType) return;
      if (select.value === "No") languageType.value = "ExternalLanguageProvider";
    };
    select.addEventListener("change", syncLanguageType);
    syncLanguageType();
  });

  qa("[data-school-exam-requirements]").forEach((group) => {
    const boxes = qa('input[type="checkbox"]', group);
    const noneBox = boxes.find((input) => input.value === "None");
    boxes.forEach((box) => {
      box.addEventListener("change", () => {
        if (box.value === "None" && box.checked) {
          boxes.forEach((other) => {
            if (other !== box) other.checked = false;
          });
          return;
        }
        if (box.checked && noneBox) noneBox.checked = false;
        if (!boxes.some((input) => input.value !== "None" && input.checked) && noneBox) noneBox.checked = true;
      });
    });
  });

  qa('input[name="available_intake_terms"][value="NoRequirement"]').forEach((noRequirementBox) => {
    const group = noRequirementBox.closest(".school-multi-choice");
    if (!group) return;
    const termBoxes = qa('input[name="available_intake_terms"]', group).filter((input) => input.value !== "NoRequirement");
    noRequirementBox.addEventListener("change", () => {
      if (!noRequirementBox.checked) return;
      termBoxes.forEach((input) => {
        input.checked = true;
      });
      noRequirementBox.checked = false;
    });
  });

  qa("[data-school-feature-category-select]").forEach((categorySelect) => {
    const scope = categorySelect.closest("form") || document;
    const tagSelect = q("[data-school-feature-tag-select]", scope);
    if (!tagSelect) return;
    const tagOptions = Array.from(tagSelect.options);
    const syncFeatureTags = () => {
      const categoryId = String(categorySelect.value || "");
      let firstVisible = null;
      tagOptions.forEach((option) => {
        const optionCategory = option.dataset.categoryId || "";
        const isEmpty = option.value === "";
        const visible = isEmpty || !categoryId || optionCategory === categoryId;
        option.hidden = !visible;
        option.disabled = !visible;
        if (visible && !isEmpty && !firstVisible) firstVisible = option;
      });
      const selected = tagSelect.selectedOptions[0];
      if (selected && selected.disabled) {
        tagSelect.value = "";
        if (categoryId && firstVisible && tagSelect.required) tagSelect.value = firstVisible.value;
      }
    };
    categorySelect.addEventListener("change", syncFeatureTags);
    syncFeatureTags();
  });

  qa("[data-reminder-toast-close]").forEach((button) => {
    button.addEventListener("click", () => {
      const toast = button.closest("[data-reminder-toast]");
      if (toast) toast.hidden = true;
    });
  });
  document.addEventListener("click", (event) => {
    const button = event.target.closest?.("[data-reminder-toast-close]");
    if (!button) return;
    const toast = button.closest("[data-reminder-toast]");
    if (toast) toast.hidden = true;
  });

  const reminderSyncEnabled = Boolean(q("[data-reminder-sync-enabled]"));
  const reminderToastChannel = reminderSyncEnabled && typeof BroadcastChannel === "function"
    ? new BroadcastChannel("aaie-reminder-actionable-v1")
    : null;
  let reminderToastSyncing = null;
  let reminderToastSyncGeneration = 0;

  const applyReminderToastState = (state) => {
    if (!state?.ok) return;
    const current = q("[data-reminder-toast]");
    const template = document.createElement("template");
    template.innerHTML = String(state.toast_html || "").trim();
    const next = q("[data-reminder-toast]", template.content);
    if (!next) {
      current?.remove();
      return;
    }
    if (current) current.replaceWith(next);
    else document.body.append(next);
  };

  const fetchActionableReminderState = async () => {
    const response = await fetch("/api/reminders/actionable", {
      credentials: "same-origin",
      cache: "no-store",
      headers: { "Accept": "application/json", "X-Requested-With": "fetch" },
    });
    if (!response.ok) throw new Error("提醒状态刷新失败");
    const state = await response.json();
    if (state.ok === false) throw new Error(state.error || "提醒状态刷新失败");
    return state;
  };

  const syncReminderToast = (force = false) => {
    if (!force && reminderToastSyncing) return reminderToastSyncing;
    const generation = ++reminderToastSyncGeneration;
    let request = null;
    request = fetchActionableReminderState()
      .then((state) => {
        if (generation === reminderToastSyncGeneration) applyReminderToastState(state);
        return state;
      })
      .catch(() => null)
      .finally(() => {
        if (reminderToastSyncing === request) reminderToastSyncing = null;
      });
    reminderToastSyncing = request;
    return request;
  };

  if (reminderSyncEnabled) {
    reminderToastChannel?.addEventListener("message", () => syncReminderToast());
    window.addEventListener("pageshow", () => syncReminderToast());
    window.addEventListener("focus", () => syncReminderToast());
    document.addEventListener("visibilitychange", () => {
      if (!document.hidden) syncReminderToast();
    });
    syncReminderToast();
  }

  document.addEventListener("submit", async (event) => {
    const form = event.target;
    if (!(form instanceof HTMLFormElement) || !form.matches("[data-reminder-toast-action]")) return;
    event.preventDefault();
    event.stopImmediatePropagation();
    if (form.dataset.submitting === "true") return;
    const toast = form.closest("[data-reminder-toast]");
    const buttons = qa("button", form);
    const originalLabels = buttons.map((button) => button.textContent || "");
    form.dataset.submitting = "true";
    form.setAttribute("aria-busy", "true");
    buttons.forEach((button) => {
      button.disabled = true;
      button.textContent = "处理中...";
    });
    try {
      const currentState = await fetchActionableReminderState();
      const reminderId = String(toast?.dataset.reminderId || "");
      const actionableIds = (currentState.reminder_ids || []).map(String);
      if (reminderId && !actionableIds.includes(reminderId)) {
        applyReminderToastState(currentState);
        return;
      }
      const response = await fetch(form.action, {
        method: "POST",
        body: new FormData(form),
        credentials: "same-origin",
        headers: { "Accept": "application/json", "X-Requested-With": "fetch" },
      });
      const data = await response.json().catch(() => ({}));
      if (!response.ok || data.ok === false) throw new Error(data.error || data.message || "操作失败");
      if (toast) toast.hidden = true;
      reminderToastChannel?.postMessage({ type: "refresh", reminderId });
      await syncReminderToast(true);
    } catch (error) {
      const message = q("[data-reminder-toast-error]", toast || form);
      if (message) {
        message.textContent = error?.message || "操作失败，请稍后再试。";
        message.hidden = false;
      } else {
        alert(error?.message || "操作失败，请稍后再试。");
      }
      buttons.forEach((button, index) => {
        button.disabled = false;
        button.textContent = originalLabels[index] || button.textContent;
      });
      form.dataset.submitting = "false";
      form.removeAttribute("aria-busy");
    } finally {
      if (form.isConnected) {
        buttons.forEach((button, index) => {
          button.disabled = false;
          button.textContent = originalLabels[index] || button.textContent;
        });
        form.dataset.submitting = "false";
        form.removeAttribute("aria-busy");
      }
    }
  });

  const CRM_TEMPORARY_TOAST_TIMEOUT_MS = 3000;
  const dismissCrmTemporaryToast = (toast) => {
    if (!toast) return;
    toast.remove();
  };
  const bindCrmTemporaryToast = (toast, persistent = false) => {
    if (!toast || toast.dataset.crmTemporaryToastBound === "true") return;
    toast.dataset.crmTemporaryToastBound = "true";
    const timer = persistent
      ? null
      : window.setTimeout(() => {
        dismissCrmTemporaryToast(toast);
      }, CRM_TEMPORARY_TOAST_TIMEOUT_MS);
    qa("[data-crm-toast-close], [data-crm-temporary-toast-close]", toast).forEach((button) => {
      button.addEventListener("click", () => {
        if (timer !== null) window.clearTimeout(timer);
        dismissCrmTemporaryToast(toast);
      });
    });
  };

  const copyText = async (text) => {
    if (navigator.clipboard && window.isSecureContext) {
      try {
        await navigator.clipboard.writeText(text);
        return true;
      } catch (_) {}
    }
    const textarea = document.createElement("textarea");
    textarea.value = text;
    textarea.setAttribute("readonly", "");
    textarea.style.position = "fixed";
    textarea.style.left = "-9999px";
    textarea.style.top = "0";
    document.body.appendChild(textarea);
    textarea.focus();
    textarea.select();
    let copied = false;
    try {
      copied = document.execCommand("copy");
    } catch (_) {
      copied = false;
    }
    textarea.remove();
    return copied;
  };

  const copyTextAuthoritatively = async (text) => {
    if (!navigator.clipboard || !window.isSecureContext) return false;
    try {
      await navigator.clipboard.writeText(text);
      return true;
    } catch (_) {
      return false;
    }
  };

  const crmToastStack = () => {
    let stack = q("[data-crm-toast-stack]");
    if (!stack) {
      stack = document.createElement("aside");
      stack.className = "crm-toast-stack";
      stack.dataset.crmToastStack = "true";
      stack.setAttribute("aria-live", "polite");
      stack.setAttribute("aria-label", "CRM 通知");
      document.body.appendChild(stack);
    }
    return stack;
  };

  const showCrmToast = (payload = {}) => {
    const message = String(payload.message || payload.title || "").trim();
    if (!message) return null;
    const messageHtml = escapeHtml(message).replace(/\r?\n/g, "<br>");
    const kind = ["success", "error", "warn", "info"].includes(payload.kind) ? payload.kind : "info";
    const title = String(payload.title || (kind === "success" ? "操作成功" : "CRM 通知")).trim();
    const metaParts = [payload.entry_type, payload.event_at].filter(Boolean);
    const meta = String(payload.meta || metaParts.join(" · ")).trim();
    const toast = document.createElement("section");
    toast.className = `crm-toast crm-toast-${kind}`;
    toast.dataset.crmToast = "true";
    toast.innerHTML = `
      <div class="crm-toast-accent"></div>
      <div class="crm-toast-body">
        <div class="crm-toast-head">
          <strong>${escapeHtml(title)}</strong>
          <button type="button" class="icon-button crm-toast-close" data-crm-toast-close aria-label="关闭通知">×</button>
        </div>
        <p>${messageHtml}</p>
        ${meta ? `<small>${escapeHtml(meta)}</small>` : ""}
        ${(payload.href || payload.copyText) ? `<div class="crm-toast-actions">
          ${payload.href ? `<a class="secondary button-link compact" href="${escapeHtml(payload.href)}">打开</a>` : ""}
          ${payload.copyText ? `<button class="${payload.copyPrimary ? "primary" : "secondary"} compact" type="button" data-crm-toast-copy>复制链接</button>` : ""}
        </div>` : ""}
      </div>
    `;
    q("[data-crm-toast-copy]", toast)?.addEventListener("click", async (event) => {
      const button = event.currentTarget;
      const old = button.textContent;
      const copy = payload.copyAuthoritatively ? copyTextAuthoritatively : copyText;
      button.disabled = true;
      const copied = await copy(payload.copyText || "");
      button.disabled = false;
      button.textContent = copied ? "已复制" : (payload.copyAuthoritatively ? "复制失败，请重试" : "复制失败");
      button.classList.toggle("copy-failed", !copied);
      if (copied && payload.copySuccessMessage) {
        const messageNode = q("p", toast);
        if (messageNode) {
          messageNode.innerHTML = escapeHtml(payload.copySuccessMessage).replace(/\r?\n/g, "<br>");
        }
      }
      if (copied && payload.dismissAfterCopy) {
        window.setTimeout(() => dismissCrmTemporaryToast(toast), CRM_TEMPORARY_TOAST_TIMEOUT_MS);
      }
      if (payload.copyAuthoritatively) return;
      setTimeout(() => {
        if (!document.body.contains(button)) return;
        button.textContent = old;
        button.classList.remove("copy-failed");
      }, copied ? 1400 : 2600);
    });
    crmToastStack().appendChild(toast);
    bindCrmTemporaryToast(toast, Boolean(payload.persistent));
    return toast;
  };
  window.aaieShowToast = showCrmToast;
  qa("[data-crm-temporary-toast]").forEach(bindCrmTemporaryToast);

  document.addEventListener("click", async (event) => {
    const button = event.target.closest?.("[data-copy]");
    if (!button) return;
    const old = button.textContent;
    const copied = await copyText(button.dataset.copy || "");
    button.textContent = copied ? "已复制" : "复制失败，请手动选择链接";
    button.classList.toggle("copy-failed", !copied);
    setTimeout(() => {
      if (!document.body.contains(button)) return;
      button.textContent = old;
      button.classList.remove("copy-failed");
    }, copied ? 1400 : 2600);
  });
  qa("[data-auto-copy][data-copy]").forEach(async (button) => {
    const copied = await copyText(button.dataset.copy || "");
    button.textContent = copied
      ? (button.dataset.autoCopySuccess || "已自动复制")
      : (button.dataset.autoCopyFailure || "自动复制失败，请点击复制合同链接");
    button.classList.toggle("copy-failed", !copied);
  });

  const consumeRedirectToast = async () => {
    const params = new URLSearchParams(window.location.search || "");
    const initialMessage = params.get("toast_message");
    if (!initialMessage) return;
    const copyValue = params.get("toast_copy") || "";
    const copySuccessMessage = params.get("toast_copy_success") || "";
    const hasManualCopyOutcome = Boolean(copyValue && copySuccessMessage);
    const toast = showCrmToast({
      title: params.get("toast_title") || "操作成功",
      message: initialMessage,
      kind: params.get("toast_kind") || "success",
      href: params.get("toast_href") || "",
      copyText: copyValue,
      copyAuthoritatively: hasManualCopyOutcome,
      copyPrimary: hasManualCopyOutcome,
      copySuccessMessage,
      persistent: hasManualCopyOutcome,
      dismissAfterCopy: hasManualCopyOutcome,
    });
    ["toast_message", "toast_kind", "toast_title", "toast_copy", "toast_copy_success", "toast_copy_failure", "toast_href"].forEach((key) => params.delete(key));
    const query = params.toString();
    const nextUrl = `${window.location.pathname}${query ? `?${query}` : ""}${window.location.hash || ""}`;
    window.history.replaceState(null, "", nextUrl);
    if (!copyValue) return;
    const copyButton = toast ? q("[data-crm-toast-copy]", toast) : null;
    if (hasManualCopyOutcome) return;
    const copied = await copyText(copyValue);
    if (copyButton) copyButton.textContent = copied ? "已自动复制" : "点击复制链接";
    if (!copied) showCrmToast({ kind: "warn", title: "复制提醒", message: "浏览器未允许自动复制，请点击右侧通知里的复制链接。", copyText: copyValue });
  };
  consumeRedirectToast();

  const syncVisaDocumentTypeForFile = (form) => {
    if (!(form instanceof HTMLFormElement) || !form.matches("[data-visa-document-type-form]")) return;
    if (form.dataset.visaDocumentTypeHasFile === "true") return;
    const fileInput = q("[data-visa-document-type-file]", form);
    const select = q("[data-visa-document-type-select]", form);
    if (!(fileInput instanceof HTMLInputElement) || !(select instanceof HTMLSelectElement)) return;
    const hasNewFile = Boolean(fileInput.files && fileInput.files.length);
    select.disabled = !hasNewFile;
    select.required = hasNewFile;
  };
  qa("[data-visa-document-type-form]").forEach(syncVisaDocumentTypeForFile);
  document.addEventListener("change", (event) => {
    const fileInput = event.target;
    if (!(fileInput instanceof HTMLInputElement) || !fileInput.matches("[data-visa-document-type-file]")) return;
    syncVisaDocumentTypeForFile(fileInput.closest("[data-visa-document-type-form]"));
  });

  document.addEventListener("submit", (event) => {
    const form = event.target;
    if (!(form instanceof HTMLFormElement) || !form.matches("[data-visa-financial-item-form]")) return;
    const elements = form.elements;
    const amount = String(elements.namedItem("amount")?.value || "").trim();
    const invalid = !elements.namedItem("item_type")?.value
      || !elements.namedItem("provider")?.value
      || !amount
      || !elements.namedItem("currency")?.value
      || !elements.namedItem("frequency")?.value
      || Number.isNaN(Number(amount))
      || Number(amount) < 0;
    if (!invalid) return;
    event.preventDefault();
    event.stopPropagation();
    const message = form.dataset.requiredMessage || "空白明细无法增加。请至少填写类型、提供人、金额、币种和频率。";
    showCrmToast({ kind: "error", title: "无法新增明细", message, timeout: 9000 });
    const firstInvalid = ["item_type", "provider", "amount", "currency", "frequency"]
      .map((name) => elements.namedItem(name))
      .find((control) => control && !String(control.value || "").trim());
    firstInvalid?.focus?.();
  }, true);

  document.addEventListener("submit", (event) => {
    const form = event.target;
    if (!(form instanceof HTMLFormElement) || !form.matches("[data-visa-asset-item-form]")) return;
    const elements = form.elements;
    const amount = String(elements.namedItem("estimated_value")?.value || "").trim();
    const invalid = !elements.namedItem("asset_type")?.value
      || !elements.namedItem("owner")?.value
      || !amount
      || !elements.namedItem("currency")?.value
      || Number.isNaN(Number(amount))
      || Number(amount) < 0;
    if (!invalid) return;
    event.preventDefault();
    event.stopPropagation();
    const message = form.dataset.requiredMessage || "空白资产明细无法增加。请至少填写资产类型、持有人、估值金额和币种。";
    showCrmToast({ kind: "error", title: "无法新增资产明细", message, timeout: 9000 });
    const firstInvalid = ["asset_type", "owner", "estimated_value", "currency"]
      .map((name) => elements.namedItem(name))
      .find((control) => control && !String(control.value || "").trim());
    firstInvalid?.focus?.();
  }, true);

  const refreshContractsLive = (() => {
    let timer = null;
    return () => {
      clearTimeout(timer);
      timer = setTimeout(async () => {
        const scrollY = window.scrollY;
        try {
          const panel = q('[data-panel="contracts"][data-lazy-tab-url][data-lazy-tab-loaded="true"]');
          if (panel) {
            const response = await fetch(panel.dataset.lazyTabUrl, {
              credentials: "same-origin",
              cache: "no-store",
              headers: { "Accept": "text/html", "X-Requested-With": "fetch" },
            });
            if (response.ok) {
              panel.innerHTML = await response.text();
              initDynamicPanel(panel);
              window.scrollTo({ top: scrollY });
            }
          }
          const centerSection = q(".contract-center #contracts-section");
          if (centerSection) {
            const response = await fetch(window.location.href, {
              credentials: "same-origin",
              cache: "no-store",
              headers: { "Accept": "text/html", "X-Requested-With": "fetch" },
            });
            if (response.ok) {
              const doc = new DOMParser().parseFromString(await response.text(), "text/html");
              const next = q(".contract-center #contracts-section", doc);
              if (next) {
                centerSection.replaceWith(next);
                window.scrollTo({ top: scrollY });
              }
            }
          }
        } catch (_) {}
      }, 350);
    };
  })();

  const refreshSchoolPlanDetail = (() => {
    let timer = null;
    return () => {
      clearTimeout(timer);
      timer = setTimeout(async () => {
        const current = q("[data-live-school-plan-detail]");
        if (!current) return;
        const scrollY = window.scrollY;
        try {
          const response = await fetch(window.location.href, {
            credentials: "same-origin",
            cache: "no-store",
            headers: { "Accept": "text/html", "X-Requested-With": "fetch" },
          });
          if (!response.ok) return;
          const doc = new DOMParser().parseFromString(await response.text(), "text/html");
          const next = q("[data-live-school-plan-detail]", doc);
          if (next) current.replaceWith(next);
          window.scrollTo({ top: scrollY });
        } catch (_) {}
      }, 350);
    };
  })();

  const initParentActivityNotifications = () => {
    if (!q(".app-shell")) return;
    let cursor = "";
    const seenKey = "aaie-parent-activity-seen-v1";
    const cursorKey = "aaie-parent-activity-cursor-v1";
    const seen = new Set(JSON.parse(sessionStorage.getItem(seenKey) || "[]"));
    try {
      cursor = sessionStorage.getItem(cursorKey) || "";
    } catch (_) {
      cursor = "";
    }
    let polling = false;

    const rememberSeen = () => {
      try {
        sessionStorage.setItem(seenKey, JSON.stringify(Array.from(seen).slice(-120)));
      } catch (_) {}
    };

    const handleEvents = (events) => {
      (events || []).forEach((event) => {
        if (!event?.id || seen.has(event.id)) return;
        seen.add(event.id);
        showCrmToast(event);
        if ((event.refresh || []).includes("contracts")) refreshContractsLive();
        if ((event.refresh || []).includes("school_plan")) refreshSchoolPlanDetail();
        if ((event.refresh || []).includes("visa_information_collection")) {
          window.setTimeout(() => window.location.reload(), 900);
        }
      });
      rememberSeen();
    };

    const poll = async () => {
      if (polling) return;
      polling = true;
      try {
        const url = cursor ? `/api/parent-activity-notifications?since=${encodeURIComponent(cursor)}` : "/api/parent-activity-notifications";
        const response = await fetch(url, {
          credentials: "same-origin",
          cache: "no-store",
          headers: { "Accept": "application/json", "X-Requested-With": "fetch" },
        });
        if (!response.ok) return;
        const data = await response.json();
        handleEvents(data.events || []);
        if (data.cursor) {
          cursor = data.cursor;
          try { sessionStorage.setItem(cursorKey, cursor); } catch (_) {}
        }
      } catch (_) {
      } finally {
        polling = false;
      }
    };

    poll();
    setInterval(() => {
      if (!document.hidden) poll();
    }, 10000);
    document.addEventListener("visibilitychange", () => {
      if (!document.hidden) poll();
    });
  };
  initParentActivityNotifications();

  document.addEventListener("submit", async (event) => {
    const form = event.target;
    if (!(form instanceof HTMLFormElement) || !form.matches("[data-stay-on-submit]")) return;
    event.preventDefault();
    event.stopImmediatePropagation();
    if (form.dataset.submitting === "true") return;
    const button = q('button[type="submit"], input[type="submit"]', form);
    const oldLabel = button?.textContent || "";
    form.dataset.submitting = "true";
    if (button) {
      button.disabled = true;
      if (button.tagName === "BUTTON") button.textContent = "处理中...";
    }
    try {
      const response = await fetch(form.action, {
        method: "POST",
        body: new FormData(form),
        credentials: "same-origin",
        headers: { "Accept": "application/json", "X-Requested-With": "fetch" },
      });
      const data = await response.json().catch(() => ({}));
      if (!response.ok || data.ok === false) throw new Error(data.error || data.message || "操作失败");
      if (form.matches("[data-remove-row]")) {
        const row = form.closest("tr");
        if (row) row.remove();
      }
    } catch (error) {
      alert(error?.message || "操作失败，请稍后再试。");
      form.dataset.submitting = "false";
      if (button) {
        button.disabled = false;
        if (button.tagName === "BUTTON") button.textContent = oldLabel;
      }
    }
  });

  document.addEventListener("submit", (event) => {
    const form = event.target;
    if (!(form instanceof HTMLFormElement)) return;
    if (event.defaultPrevented || !form.checkValidity()) return;
    if (form.dataset.submitting === "true") {
      event.preventDefault();
      return;
    }
    form.dataset.submitting = "true";
    form.setAttribute("aria-busy", "true");
    qa('button[type="submit"], input[type="submit"]', form).forEach((button) => {
      button.disabled = true;
      if (button.tagName === "BUTTON" && !button.dataset.originalText) {
        button.dataset.originalText = button.textContent || "";
        button.textContent = "处理中...";
      }
    });
  });

  document.addEventListener("click", (event) => {
    const button = event.target.closest("[data-scope-select]");
    if (!button) return;
    const form = button.closest("form") || document;
    const checked = button.dataset.scopeSelect === "all";
    qa('input[name="service_scope"]', form).forEach((item) => {
      if (!item.disabled) item.checked = checked;
    });
  });

  document.addEventListener("click", (event) => {
    const choice = event.target.closest("[data-scope-choice]");
    if (!choice || !choice.contains(event.target)) return;
    if (event.target.closest("input, textarea, select, button, a, summary, details")) return;
    const input = q('input[name="service_scope"]', choice);
    if (!input || input.disabled) return;
    event.preventDefault();
    input.checked = !input.checked;
    input.dispatchEvent(new Event("change", { bubbles: true }));
  });

  qa("[data-module-permissions]").forEach((form) => {
    let presets = {};
    try { presets = JSON.parse(form.dataset.rolePresets || "{}"); } catch (_) { presets = {}; }
    const roleSelect = q("[data-role-select]", form);
    const moduleInputs = () => qa('input[name="modules"]', form);
    const applyKeys = (keys) => {
      const selected = new Set(keys || []);
      moduleInputs().forEach((input) => {
        input.checked = selected.has(input.value);
      });
    };
    qa("[data-permission-select]", form).forEach((button) => {
      button.addEventListener("click", () => {
        const action = button.dataset.permissionSelect;
        if (action === "all") {
          moduleInputs().forEach((input) => { input.checked = true; });
          return;
        }
        if (action === "none") {
          moduleInputs().forEach((input) => { input.checked = false; });
          return;
        }
        const roleName = roleSelect?.selectedOptions?.[0]?.dataset.roleName || roleSelect?.selectedOptions?.[0]?.textContent || "";
        applyKeys(presets[roleName] || []);
      });
    });
    roleSelect?.addEventListener("change", () => {
      const roleName = roleSelect.selectedOptions?.[0]?.dataset.roleName || roleSelect.selectedOptions?.[0]?.textContent || "";
      applyKeys(presets[roleName] || []);
    });
  });

  const parseMoney = (value) => {
    const cleaned = String(value || "").replace(/[, $AUD]/g, "").trim();
    if (!cleaned) return null;
    const parsed = Number(cleaned);
    return Number.isFinite(parsed) ? parsed : null;
  };
  const formatMoney = (value) => {
    if (!Number.isFinite(value)) return "";
    const rounded = Math.round(value * 100) / 100;
    return Number.isInteger(rounded) ? String(rounded) : rounded.toFixed(2).replace(/0+$/, "").replace(/\.$/, "");
  };
  function calculatePublishedContractPricing({standardFee, paymentMethod, discountAmount = 0, gstFree = false}) {
    const cents = (value) => Math.round((Number(value) || 0) * 100);
    const standardCents = cents(standardFee);
    const discountCents = Math.min(Math.max(cents(discountAmount), 0), standardCents);
    const netCents = standardCents - discountCents;
    if (!paymentMethod) {
      return {net: netCents / 100, gst: null, total: null, treatment: "UNSELECTED"};
    }
    const publicAccount = paymentMethod === "澳洲公户";
    const treatment = publicAccount ? (gstFree ? "GST_FREE" : "EXCLUSIVE") : "NOT_APPLICABLE";
    const gstCents = treatment === "EXCLUSIVE" ? Math.round(netCents * 0.1) : 0;
    return {net: netCents / 100, gst: gstCents / 100, total: (netCents + gstCents) / 100, treatment};
  }
  const syncContractPaymentPlan = (form) => {
    const enabled = q("[data-contract-payment-plan-toggle]", form)?.value === "yes";
    qa("[data-contract-payment-plan-fields]", form).forEach((field) => { field.hidden = !enabled; });
    qa("[data-contract-payment-plan-input]", form).forEach((control) => {
      control.disabled = !enabled;
      if (!enabled) control.value = "";
    });
  };
  const syncPublishedContractFees = (form) => {
    if (!form) return;
    const selectedType = q("[data-contract-type-radio]:checked", form)?.value || "";
    const sourceAuthoritativeTransfer = selectedType === "TransferApplicationContract";
    syncContractPaymentPlan(form);
    const sourceFields = q("[data-contract-transfer-source-fields]", form);
    if (sourceFields) {
      sourceFields.hidden = !sourceAuthoritativeTransfer;
      qa("select", sourceFields).forEach((control) => {
        control.disabled = !sourceAuthoritativeTransfer || control.dataset.sourceEmpty === "true"
          || control.dataset.sourceLocked === "true";
      });
    }
    const sourceFee = parseMoney(form.dataset.contractTransferFeeAuthority || "");
    const sourceHold = sourceAuthoritativeTransfer && (
      form.dataset.contractTransferSourceHold === "true" || sourceFee === null
    );
    const standard = sourceAuthoritativeTransfer
      ? sourceFee
      : parseMoney(q("[data-contract-published-standard-fee]", form)?.value);
    const payment = q("[data-contract-published-payment-method]", form)?.value || "";
    const discountToggle = q("[data-contract-discount-toggle]", form);
    const discountInput = q("[data-contract-discount-input]", form);
    const gstFree = q("[data-contract-published-gst-free]", form);
    const publicAccount = payment === "澳洲公户";
    if (gstFree) {
      gstFree.disabled = !publicAccount;
      if (!publicAccount) gstFree.value = "no";
    }
    const result = calculatePublishedContractPricing({
      standardFee: standard || 0,
      paymentMethod: payment,
      discountAmount: discountToggle?.value === "yes" ? (parseMoney(discountInput?.value) || 0) : 0,
      gstFree: gstFree?.value === "yes",
    });
    const net = q("[data-contract-published-net-fee]", form);
    const gst = q("[data-contract-published-gst-amount]", form);
    const total = q("[data-contract-published-final-total]", form);
    const treatment = q("[data-contract-published-gst]", form);
    if (sourceHold) {
      if (net) net.value = "SOURCE VERIFICATION HOLD";
      if (gst) gst.value = "HOLD";
      if (total) total.value = "HOLD";
      if (treatment) treatment.value = "请确认本次业务资料后生成合同";
      return;
    }
    const displayAmount = (value) => Number.isFinite(value) ? `AUD ${value.toFixed(2)}` : "待计算";
    if (net) net.value = `AUD ${result.net.toFixed(2)}`;
    if (gst) gst.value = displayAmount(result.gst);
    if (total) total.value = displayAmount(result.total);
    if (treatment) treatment.value = {
      UNSELECTED: "请选择付款方式",
      EXCLUSIVE: "按折后金额收取10% GST",
      GST_FREE: "GST-free（已批准）",
      NOT_APPLICABLE: "此付款方式不收取GST",
    }[result.treatment];
  };
  const syncContractFees = (form) => {
    if (!form) return;
    const paymentMethod = q("[data-contract-payment-method]", form);
    const serviceFee = q("[data-contract-service-fee]", form);
    const discountToggle = q("[data-contract-discount-toggle]", form);
    const discountInput = q("[data-contract-discount-input]", form);
    const finalFee = q("[data-contract-final-fee]", form);
    if (!serviceFee || !finalFee) return;
    const editable = ["支付宝", "微信", "国内银行"].includes(paymentMethod?.value || "");
    const authoritative = serviceFee.dataset.contractFeeAuthority === "source";
    serviceFee.readOnly = authoritative || !editable;
    if (!editable && !authoritative) serviceFee.value = serviceFee.dataset.fixedFee || "2400";
    const base = parseMoney(serviceFee.value);
    let discount = discountToggle?.value === "yes" ? (parseMoney(discountInput?.value) || 0) : 0;
    if (base !== null && discount > base) {
      discount = Math.min(discount, base);
      if (discountInput) discountInput.value = formatMoney(discount);
    }
    finalFee.value = base === null ? "" : formatMoney(Math.max(base - discount, 0));
  };
  const syncContractDiscount = (select) => {
    const form = select.closest("form") || document;
    const amountField = q("[data-contract-discount-amount]", form);
    if (!amountField) return;
    const visible = select.value === "yes";
    amountField.hidden = !visible;
    qa("input, select, textarea", amountField).forEach((control) => {
      if (!visible) control.value = "";
    });
    syncContractFees(form);
    syncPublishedContractFees(form);
  };
  const syncContractGuardianName = (relationship) => {
    const form = relationship.closest("form") || document;
    const name = q("[data-contract-guardian-name]", form);
    if (!name) return;
    let nextName = "";
    if (relationship.value === "父亲") nextName = name.dataset.fatherGuardianName || "";
    else if (relationship.value === "母亲") nextName = name.dataset.motherGuardianName || "";
    else return;
    if (!nextName) return;
    name.value = nextName;
  };
  const syncContractSigner = (sourceInput) => {
    const form = sourceInput.closest("form") || document;
    const source = q('[data-contract-signer-source]:checked', form)?.value || "mother";
    const fields = {
      name: q("[data-contract-guardian-name]", form),
      relationship: q("[data-contract-parent-relationship]", form),
      phone: q("[data-contract-parent-phone]", form),
      email: q("[data-contract-parent-email]", form),
    };
    Object.entries(fields).forEach(([fieldName, field]) => {
      if (!field) return;
      const key = `${source}${fieldName.charAt(0).toUpperCase()}${fieldName.slice(1)}`;
      field.value = field.dataset[key] || "";
    });
  };
  function initContractGuardianNames(root = document) {
    qa("[data-contract-signer-source]", root).forEach((radio) => {
      if (radio.dataset.contractSignerSourceBound !== "true") {
        radio.dataset.contractSignerSourceBound = "true";
        radio.addEventListener("change", () => syncContractSigner(radio));
      }
      if (radio.checked) syncContractSigner(radio);
    });
    qa("[data-contract-parent-relationship]", root).forEach((select) => {
      if (q("[data-contract-signer-source]", select.closest("form") || document)) return;
      if (select.dataset.contractGuardianNameBound !== "true") {
        select.dataset.contractGuardianNameBound = "true";
        select.addEventListener("change", () => syncContractGuardianName(select));
      }
      syncContractGuardianName(select);
    });
  }
  function initContractTypeForms(root = document) {
    qa("[data-contract-type-radio]", root).forEach((radio) => {
      const syncSelectedContractType = () => {
        const form = radio.closest("form") || document;
        const selectedType = q("[data-contract-type-radio]:checked", form)?.value || radio.value;
        const selectedRadio = q(`[data-contract-type-radio][value="${selectedType}"]`, form) || radio;
        const title = q("[data-contract-title]", form);
        if (title) title.value = selectedRadio.dataset.contractTitleValue || "";
        const genericFee = q("[data-contract-service-fee]", form);
        if (genericFee) {
          if (selectedType === "TransferApplicationContract") {
            genericFee.dataset.contractFeeAuthority = "source";
            genericFee.value = form.dataset.contractTransferFeeAuthority || "";
          } else {
            delete genericFee.dataset.contractFeeAuthority;
            genericFee.value = genericFee.dataset.fixedFee || "2400";
          }
          syncContractFees(form);
        }
        if (form.dataset.contractPublishedCommercials) {
          try {
            const commercials = JSON.parse(form.dataset.contractPublishedCommercials);
            const commercial = commercials[selectedType] || {};
            const fee = q("[data-contract-published-standard-fee]", form);
            const gst = q("[data-contract-published-gst]", form);
            const terms = q("[data-contract-published-payment-terms]", form);
            const refund = q("[data-contract-published-refund]", form);
            const payment = q("[data-contract-published-payment-method]", form);
            const transferFee = form.dataset.contractTransferFeeAuthority || "";
            const displayedFee = selectedType === "TransferApplicationContract"
              ? transferFee
              : (commercial.standard_service_fee || "");
            if (fee) fee.value = displayedFee ? `AUD ${displayedFee}` : "SOURCE VERIFICATION HOLD";
            if (gst) gst.value = commercial.gst_display || "未配置";
            if (terms) terms.value = commercial.payment_terms || "";
            if (refund) refund.textContent = commercial.refund_policy || "";
            if (payment) {
              const previous = payment.value;
              payment.replaceChildren(new Option("请选择", ""));
              (commercial.payment_methods || []).forEach((value) => {
                payment.append(new Option(value, value));
              });
              payment.value = (commercial.payment_methods || []).includes(previous) ? previous : "";
            }
            syncPublishedContractFees(form);
          } catch (_error) {
            // The server validates the exact Published commercial package before persistence.
          }
        }
        qa("[data-contract-service-types]", form).forEach((scope) => {
          const allowedTypes = (scope.dataset.contractServiceTypes || "").split(",").filter(Boolean);
          const defaultTypes = (scope.dataset.contractServiceDefaultTypes || "").split(",").filter(Boolean);
          const allowed = allowedTypes.includes(selectedType);
          scope.disabled = !allowed;
          scope.checked = allowed && defaultTypes.includes(selectedType);
          const choice = scope.closest("label");
          if (choice) choice.hidden = !allowed;
          const label = q("[data-contract-service-label]", choice || scope.parentElement);
          if (label && scope.dataset.contractServiceLabels) {
            try {
              const labels = JSON.parse(scope.dataset.contractServiceLabels);
              label.textContent = labels[selectedType] || label.textContent;
            } catch (_error) {
              // Invalid label metadata is harmless here; the server remains authoritative.
            }
          }
        });
        qa("[data-contract-refund-types]", form).forEach((refund) => {
          const allowedTypes = (refund.dataset.contractRefundTypes || "").split(",").filter(Boolean);
          const allowed = allowedTypes.includes(selectedType);
          refund.disabled = !allowed;
          if (!allowed) refund.checked = false;
          const card = refund.closest("label");
          if (card) card.hidden = !allowed;
        });
      };
      if (radio.dataset.contractTypeBound !== "true") {
        radio.dataset.contractTypeBound = "true";
        radio.addEventListener("change", syncSelectedContractType);
      }
      if (radio.checked) syncSelectedContractType();
    });
  }
  function initContractFeeForms(root = document) {
    qa("[data-contract-service-fee]", root).forEach((input) => {
      const form = input.closest("form");
      if (input.dataset.contractFeeBound !== "true") {
        input.dataset.contractFeeBound = "true";
        input.addEventListener("input", () => syncContractFees(form));
      }
      syncContractFees(form);
    });
    qa("[data-contract-payment-method]", root).forEach((select) => {
      const form = select.closest("form");
      if (select.dataset.contractPaymentBound !== "true") {
        select.dataset.contractPaymentBound = "true";
        select.addEventListener("change", () => syncContractFees(form));
      }
      syncContractFees(form);
    });
    qa("[data-contract-discount-input]", root).forEach((input) => {
      const form = input.closest("form");
      if (input.dataset.contractDiscountInputBound !== "true") {
        input.dataset.contractDiscountInputBound = "true";
        input.addEventListener("input", () => {
          syncContractFees(form);
          syncPublishedContractFees(form);
        });
      }
    });
    qa("[data-contract-discount-toggle]", root).forEach((select) => {
      if (select.dataset.contractDiscountToggleBound !== "true") {
        select.dataset.contractDiscountToggleBound = "true";
        select.addEventListener("change", () => syncContractDiscount(select));
      }
      syncContractDiscount(select);
    });
    qa("[data-contract-published-payment-method], [data-contract-published-gst-free], [data-contract-payment-plan-toggle]", root).forEach((control) => {
      const form = control.closest("form");
      if (control.dataset.contractPublishedPricingBound !== "true") {
        control.dataset.contractPublishedPricingBound = "true";
        control.addEventListener("change", () => syncPublishedContractFees(form));
      }
      syncPublishedContractFees(form);
    });
  }
  initContractFeeForms(document);
  initContractGuardianNames(document);
  initContractTypeForms(document);

  const previewDialog = q("[data-preview-dialog]");
  if (previewDialog) {
    const previewTitle = q("[data-preview-title]", previewDialog);
    const previewDownload = q("[data-preview-download]", previewDialog);
    const previewLayout = q("[data-preview-layout]", previewDialog);
    const previewMain = q("[data-preview-main]", previewDialog);
    const previewThumbnails = q("[data-preview-thumbnails]", previewDialog);
    const previewPrev = q("[data-preview-prev]", previewDialog);
    const previewNext = q("[data-preview-next]", previewDialog);
    const previewIndicator = q("[data-preview-page-indicator]", previewDialog);
    const previewCurrentPage = q("[data-preview-current-page]", previewDialog);
    const previewTotalPages = q("[data-preview-total-pages]", previewDialog);
    const previewJumpInput = q("[data-preview-jump-input]", previewDialog);
    const previewJumpButton = q("[data-preview-jump-button]", previewDialog);
    const previewMessage = q("[data-preview-message]", previewDialog);
    const previewZoomSelect = q("[data-preview-zoom]", previewDialog);
    const previewThumbnailToggle = q("[data-preview-thumbnail-toggle]", previewDialog);
    let previewBaseSrc = "";
    let previewPage = 1;
    let previewPageCount = 1;
    let previewZoom = 1;
    let renderedPageCount = 0;
    let renderedThumbnailCount = 0;
    let previewPageObserver = null;
    let previewThumbnailObserver = null;
    const previewObjectUrls = new Map();
    const previewThumbObjectUrls = new Map();
    const previewLoadingPages = new Set();
    const previewLoadingThumbs = new Set();
    const previewUrl = (page, size = "large") => {
      if (!previewBaseSrc) return "";
      const url = new URL(previewBaseSrc, window.location.origin);
      url.searchParams.set("page", String(page));
      url.searchParams.set("size", size);
      return `${url.pathname}${url.search}${url.hash}`;
    };
    const setPreviewMessage = (message, kind = "") => {
      if (!previewMessage) return;
      previewMessage.textContent = message || "";
      previewMessage.dataset.kind = kind;
      if (message) {
        window.setTimeout(() => {
          if (previewMessage.textContent === message) previewMessage.textContent = "";
        }, 2600);
      }
    };
    const syncPreviewControls = () => {
      if (previewIndicator) previewIndicator.textContent = `${previewPage} / ${previewPageCount}`;
      if (previewCurrentPage) previewCurrentPage.textContent = String(previewPage);
      if (previewTotalPages) previewTotalPages.textContent = String(previewPageCount);
      if (previewPrev) previewPrev.disabled = previewPage <= 1;
      if (previewNext) previewNext.disabled = previewPage >= previewPageCount;
      if (previewZoomSelect) previewZoomSelect.value = String(previewZoom);
      if (previewJumpInput) {
        previewJumpInput.placeholder = String(previewPage);
        previewJumpInput.max = String(previewPageCount);
      }
      qa("[data-preview-page]", previewMain || document).forEach((pageEl) => {
        pageEl.classList.toggle("active", Number(pageEl.dataset.previewPage) === previewPage);
      });
      qa("[data-preview-thumbnail-page]", previewThumbnails || document).forEach((button) => {
        button.classList.toggle("active", Number(button.dataset.previewThumbnailPage) === previewPage);
      });
    };
    const applyPreviewZoom = () => {
      qa("[data-preview-page-image]", previewMain || document).forEach((image) => {
        image.style.width = `${Math.round(previewZoom * 100)}%`;
        image.style.maxWidth = "none";
        image.style.maxHeight = "none";
      });
      syncPreviewControls();
    };
    const setPreviewZoom = (value) => {
      const numeric = Number(value);
      previewZoom = Number.isFinite(numeric) ? Math.max(0.5, Math.min(3, numeric)) : 1;
      applyPreviewZoom();
    };
    const clearPreviewObjectUrls = () => {
      previewObjectUrls.forEach((url) => URL.revokeObjectURL(url));
      previewThumbObjectUrls.forEach((url) => URL.revokeObjectURL(url));
      previewObjectUrls.clear();
      previewThumbObjectUrls.clear();
      previewLoadingPages.clear();
      previewLoadingThumbs.clear();
      renderedThumbnailCount = 0;
    };
    const pageLayer = (page) => q(`[data-preview-page="${page}"]`, previewMain || document);
    const thumbnailButton = (page) => q(`[data-preview-thumbnail-page="${page}"]`, previewThumbnails || document);
    const setPreviewPage = (page) => {
      const safePage = Math.max(1, Math.min(previewPageCount || 1, Number(page) || 1));
      previewPage = safePage;
      syncPreviewControls();
    };
    const observePreviewPages = () => {
      if (!previewMain || !("IntersectionObserver" in window)) return;
      if (previewPageObserver) previewPageObserver.disconnect();
      previewPageObserver = new IntersectionObserver((entries) => {
        const visible = entries
          .filter((entry) => entry.isIntersecting)
          .sort((a, b) => b.intersectionRatio - a.intersectionRatio)[0];
        if (!visible) return;
        setPreviewPage(Number(visible.target.dataset.previewPage));
      }, { root: previewMain, threshold: [0.35, 0.55, 0.75] });
      qa("[data-preview-page]", previewMain).forEach((layer) => previewPageObserver.observe(layer));
    };
    const loadPreviewThumbnail = async (page) => {
      const button = thumbnailButton(page);
      if (!button || previewThumbObjectUrls.has(page) || previewLoadingThumbs.has(page)) return;
      previewLoadingThumbs.add(page);
      try {
        const response = await fetch(previewUrl(page, "thumb"), { credentials: "same-origin", cache: "no-store" });
        if (!response.ok || !String(response.headers.get("Content-Type") || "").startsWith("image/")) throw new Error("thumbnail unavailable");
        const blob = await response.blob();
        const objectUrl = URL.createObjectURL(blob);
        previewThumbObjectUrls.set(page, objectUrl);
        const image = q("img", button);
        if (image) image.src = objectUrl;
      } catch (_) {
        button.dataset.error = "true";
      } finally {
        previewLoadingThumbs.delete(page);
      }
    };
    const observePreviewThumbnails = () => {
      if (!previewThumbnails) return;
      if (previewThumbnailObserver) previewThumbnailObserver.disconnect();
      if (!("IntersectionObserver" in window)) {
        qa("[data-preview-thumbnail-page]", previewThumbnails).slice(0, 12).forEach((button) => loadPreviewThumbnail(Number(button.dataset.previewThumbnailPage)));
        return;
      }
      previewThumbnailObserver = new IntersectionObserver((entries) => {
        entries.forEach((entry) => {
          if (!entry.isIntersecting) return;
          const page = Number(entry.target.dataset.previewThumbnailPage);
          loadPreviewThumbnail(page);
        });
      }, { root: previewThumbnails, rootMargin: "180px 0px" });
      qa("[data-preview-thumbnail-page]", previewThumbnails).forEach((button) => previewThumbnailObserver.observe(button));
    };
    const renderPreviewThumbnails = () => {
      if (!previewThumbnails || renderedThumbnailCount === previewPageCount) return;
      previewThumbnails.innerHTML = "";
      renderedThumbnailCount = previewPageCount;
      for (let page = 1; page <= previewPageCount; page += 1) {
        const button = document.createElement("button");
        button.type = "button";
        button.dataset.previewThumbnailPage = String(page);
        button.innerHTML = `<span>${page}</span><img alt="第 ${page} 页缩略图">`;
        const image = q("img", button);
        const cachedUrl = previewThumbObjectUrls.get(page);
        if (cachedUrl) image.src = cachedUrl;
        button.addEventListener("click", () => scrollPreviewToPage(page));
        previewThumbnails.appendChild(button);
      }
      observePreviewThumbnails();
      syncPreviewControls();
    };
    const renderPreviewPages = () => {
      if (!previewMain || renderedPageCount === previewPageCount) return;
      previewMain.innerHTML = "";
      renderedPageCount = previewPageCount;
      for (let page = 1; page <= previewPageCount; page += 1) {
        const layer = document.createElement("section");
        layer.className = "preview-page-layer";
        layer.dataset.previewPage = String(page);
        layer.innerHTML = `<div class="preview-page-label">第 ${page} 页</div><div class="preview-page-placeholder">正在准备第 ${page} 页</div><img data-preview-page-image alt="第 ${page} 页预览" hidden>`;
        previewMain.appendChild(layer);
      }
      observePreviewPages();
      applyPreviewZoom();
    };
    const previewErrorText = (code) => {
      const messages = {
        source_missing: "文件不存在，请检查归档路径。",
        pdf_page_count_failed: "PDF 页数读取失败，请检查文件是否损坏。",
        pdf_render_failed: "文件预览暂时不可用，请下载原文件查看。",
        image_render_failed: "文件预览暂时不可用，请下载原文件查看。",
        docx_render_failed: "文件预览暂时不可用，请下载原文件查看。",
        unsupported: "文件预览暂时不可用，请下载原文件查看。"
      };
      return messages[code] || "文件预览暂时不可用，请下载原文件查看。";
    };
    const loadPreviewPageImage = async (page) => {
      if (!previewBaseSrc || previewObjectUrls.has(page) || previewLoadingPages.has(page)) return;
      previewLoadingPages.add(page);
      const layer = pageLayer(page);
      if (layer) layer.setAttribute("aria-busy", "true");
      try {
        const response = await fetch(previewUrl(page), { credentials: "same-origin", cache: "no-store" });
        if (!response.ok) throw new Error("preview unavailable");
        if (!String(response.headers.get("Content-Type") || "").startsWith("image/")) {
          throw new Error("preview is not an image");
        }
        const status = response.headers.get("X-Preview-Status") || "ok";
        const errorCode = response.headers.get("X-Preview-Error-Code") || "";
        const total = parseInt(response.headers.get("X-Preview-Page-Count") || "1", 10);
        const current = parseInt(response.headers.get("X-Preview-Page") || String(page), 10);
        previewPageCount = Number.isFinite(total) && total > 0 ? total : 1;
        renderPreviewPages();
        renderPreviewThumbnails();
        const safePage = Number.isFinite(current) && current > 0 ? current : page;
        const targetLayer = pageLayer(safePage);
        const image = q("[data-preview-page-image]", targetLayer || document);
        const placeholder = q(".preview-page-placeholder", targetLayer || document);
        const blob = await response.blob();
        const objectUrl = URL.createObjectURL(blob);
        previewObjectUrls.set(safePage, objectUrl);
        if (image) {
          image.hidden = false;
          image.src = objectUrl;
        }
        if (placeholder) placeholder.hidden = true;
        if (status !== "ok") {
          const message = previewErrorText(errorCode);
          setPreviewMessage(message, "error");
          if (previewMain) previewMain.dataset.error = message;
        }
        setPreviewPage(safePage);
        applyPreviewZoom();
        window.requestAnimationFrame(loadVisiblePreviewPages);
      } catch (error) {
        const targetLayer = pageLayer(page);
        const placeholder = q(".preview-page-placeholder", targetLayer || document);
        const message = error?.message || "文件预览暂时不可用，请下载原文件查看。";
        if (placeholder) placeholder.textContent = message;
        if (previewMain) previewMain.dataset.error = message;
      } finally {
        const targetLayer = pageLayer(page);
        if (targetLayer) targetLayer.setAttribute("aria-busy", "false");
        previewLoadingPages.delete(page);
      }
    };
    const loadVisiblePreviewPages = () => {
      if (!previewMain) return;
      const viewport = previewMain.getBoundingClientRect();
      qa("[data-preview-page]", previewMain).forEach((layer) => {
        const rect = layer.getBoundingClientRect();
        const near = rect.bottom >= viewport.top - 900 && rect.top <= viewport.bottom + 900;
        if (near) loadPreviewPageImage(Number(layer.dataset.previewPage));
      });
    };
    function scrollPreviewToPage(page) {
      const safePage = Math.max(1, Math.min(previewPageCount || 1, Number(page) || 1));
      loadPreviewPageImage(safePage);
      const layer = pageLayer(safePage);
      if (layer) layer.scrollIntoView({ block: "start" });
      setPreviewPage(safePage);
      const thumb = thumbnailButton(safePage);
      if (thumb) thumb.scrollIntoView({ block: "nearest" });
    }
    const loadPreviewPage = async (page) => {
      scrollPreviewToPage(page);
    };
    const jumpPreviewPage = () => {
      const raw = String(previewJumpInput?.value || "").trim();
      const target = Number(raw);
      if (!raw || !Number.isInteger(target) || target < 1) {
        setPreviewMessage("请输入有效页码", "error");
        return;
      }
      if (target > previewPageCount) {
        setPreviewMessage("页码超出范围", "error");
        return;
      }
      setPreviewMessage("");
      scrollPreviewToPage(target);
      if (previewJumpInput) previewJumpInput.value = "";
    };
    document.addEventListener("click", (event) => {
      const button = event.target.closest("[data-preview-open]");
      if (!button) return;
      previewBaseSrc = button.dataset.previewSrc || "";
      previewPage = 1;
      previewPageCount = 1;
      previewZoom = 1;
      renderedPageCount = 0;
      clearPreviewObjectUrls();
      if (previewMain) {
        previewMain.innerHTML = "";
        previewMain.dataset.error = "";
      }
      if (previewThumbnails) previewThumbnails.innerHTML = "";
      if (previewJumpInput) previewJumpInput.value = "";
      setPreviewMessage("");
      if (previewTitle) previewTitle.textContent = button.dataset.previewTitle || "文件预览";
      if (previewDownload) previewDownload.href = button.dataset.downloadUrl || "#";
      previewDialog.classList.remove("thumbnails-collapsed");
      if (previewLayout) previewLayout.classList.remove("thumbnails-collapsed");
      if (previewThumbnailToggle) previewThumbnailToggle.textContent = "隐藏缩略图";
      syncPreviewControls();
      previewDialog.showModal();
      loadPreviewPage(1);
    });
    if (previewPrev) previewPrev.addEventListener("click", () => loadPreviewPage(Math.max(1, previewPage - 1)));
    if (previewNext) previewNext.addEventListener("click", () => loadPreviewPage(Math.min(previewPageCount, previewPage + 1)));
    if (previewJumpButton) previewJumpButton.addEventListener("click", jumpPreviewPage);
    previewJumpInput?.addEventListener("keydown", (event) => {
      if (event.key !== "Enter") return;
      event.preventDefault();
      jumpPreviewPage();
    });
    previewMain?.addEventListener("scroll", () => {
      window.requestAnimationFrame(loadVisiblePreviewPages);
    });
    q("[data-preview-zoom-in]", previewDialog)?.addEventListener("click", () => setPreviewZoom(previewZoom + 0.25));
    q("[data-preview-zoom-out]", previewDialog)?.addEventListener("click", () => setPreviewZoom(previewZoom - 0.25));
    q("[data-preview-fit-width]", previewDialog)?.addEventListener("click", () => setPreviewZoom(1));
    previewZoomSelect?.addEventListener("change", () => setPreviewZoom(previewZoomSelect.value));
    previewThumbnailToggle?.addEventListener("click", () => {
      if (!previewLayout) return;
      const collapsed = previewLayout.classList.toggle("thumbnails-collapsed");
      previewDialog.classList.toggle("thumbnails-collapsed", collapsed);
      previewThumbnailToggle.textContent = collapsed ? "显示缩略图" : "隐藏缩略图";
    });
    qa(".doc-thumb img").forEach((image) => {
      image.addEventListener("error", () => {
        const button = image.closest(".doc-thumb");
        image.remove();
        if (button) button.textContent = "预览";
      });
    });
    qa("[data-preview-close]", previewDialog).forEach((button) => {
      button.addEventListener("click", () => previewDialog.close());
    });
    previewDialog.addEventListener("close", () => {
      if (previewPageObserver) previewPageObserver.disconnect();
      if (previewThumbnailObserver) previewThumbnailObserver.disconnect();
      clearPreviewObjectUrls();
      previewBaseSrc = "";
      previewPage = 1;
      previewPageCount = 1;
      previewZoom = 1;
      renderedPageCount = 0;
      renderedThumbnailCount = 0;
      syncPreviewControls();
      if (previewMain) {
        previewMain.innerHTML = "";
        previewMain.dataset.error = "";
      }
      if (previewThumbnails) previewThumbnails.innerHTML = "";
      setPreviewMessage("");
    });
  }

  qa("[data-manual-placement]").forEach((root) => {
    const paper = q("[data-manual-paper]", root);
    const image = q("[data-manual-page-image]", root);
    const layer = q("[data-manual-layer]", root);
    const pageSelect = q("[data-manual-page-select]", root);
    const form = q("[data-manual-form]", root);
    const message = q("[data-manual-message]", root);
    const inputs = {};
    qa("[data-manual-input]", root).forEach((input) => { inputs[input.dataset.manualInput] = input; });
    let zoom = 1;
    let rotation = 0;
    const basePageWidth = parseFloat(root.dataset.pageWidth || "595");
    const basePageHeight = parseFloat(root.dataset.pageHeight || "842");

    const viewSize = () => (rotation % 180 === 0
      ? { width: basePageWidth, height: basePageHeight }
      : { width: basePageHeight, height: basePageWidth });

    const setInput = (name, value) => {
      if (inputs[name]) inputs[name].value = Number.isFinite(value) ? value.toFixed(2) : "";
    };

    const placeDefaults = () => {
      if (!layer) return;
      const rect = layer.getBoundingClientRect();
      qa("[data-manual-object]", root).forEach((item) => {
        const left = parseFloat(item.dataset.left || "20") / 100 * rect.width;
        const top = parseFloat(item.dataset.top || "60") / 100 * rect.height;
        item.style.left = `${left}px`;
        item.style.top = `${top}px`;
        item.style.width = `${parseFloat(item.dataset.width || "100")}px`;
        item.style.height = `${parseFloat(item.dataset.height || "60")}px`;
        if (item.dataset.fontSize) item.style.fontSize = `${parseFloat(item.dataset.fontSize)}px`;
      });
      updateHidden();
    };

    const updatePageSrc = () => {
      if (!image || !root.dataset.pageSrc) return;
      const page = pageSelect?.value || inputs.page_number?.value || "1";
      const separator = root.dataset.pageSrc.includes("?") ? "&" : "?";
      image.src = `${root.dataset.pageSrc}${separator}page=${encodeURIComponent(page)}&rotate=${rotation}`;
      if (inputs.page_number) inputs.page_number.value = page;
      if (inputs.page_rotation) inputs.page_rotation.value = String(rotation);
    };

    const updateHidden = () => {
      if (!layer) return;
      const rect = layer.getBoundingClientRect();
      if (!rect.width || !rect.height) return;
      const size = viewSize();
      const objectValues = (name) => {
        const item = q(`[data-manual-object="${name}"]`, root);
        if (!item) return null;
        const x = item.offsetLeft / rect.width * size.width;
        const top = item.offsetTop / rect.height * size.height;
        const width = item.offsetWidth / rect.width * size.width;
        const height = item.offsetHeight / rect.height * size.height;
        return { x, y: size.height - top - height, width, height, item };
      };
      ["stamp", "signature"].forEach((name) => {
        const values = objectValues(name);
        if (!values) return;
        setInput(`${name}_x`, values.x);
        setInput(`${name}_y`, values.y);
        setInput(`${name}_width`, values.width);
        setInput(`${name}_height`, values.height);
      });
      const dateValues = objectValues("date");
      if (dateValues) {
        setInput("date_x", dateValues.x);
        setInput("date_y", dateValues.y + dateValues.height * 0.22);
        const px = parseFloat(getComputedStyle(dateValues.item).fontSize || "16");
        setInput("date_font_size", px / rect.height * size.height);
      }
    };

    const clampItem = (item) => {
      if (!layer) return;
      const maxLeft = Math.max(0, layer.clientWidth - item.offsetWidth);
      const maxTop = Math.max(0, layer.clientHeight - item.offsetHeight);
      item.style.left = `${Math.min(maxLeft, Math.max(0, item.offsetLeft))}px`;
      item.style.top = `${Math.min(maxTop, Math.max(0, item.offsetTop))}px`;
    };

    qa("[data-manual-object]", root).forEach((item) => {
      item.addEventListener("pointerdown", (event) => {
        event.preventDefault();
        qa("[data-manual-object]", root).forEach((node) => node.classList.toggle("active", node === item));
        const resize = event.target.closest("[data-manual-resize]");
        const startX = event.clientX;
        const startY = event.clientY;
        const startLeft = item.offsetLeft;
        const startTop = item.offsetTop;
        const startWidth = item.offsetWidth;
        const startHeight = item.offsetHeight;
        const onMove = (moveEvent) => {
          const dx = moveEvent.clientX - startX;
          const dy = moveEvent.clientY - startY;
          if (resize) {
            item.style.width = `${Math.max(24, startWidth + dx)}px`;
            item.style.height = `${Math.max(18, startHeight + dy)}px`;
          } else {
            item.style.left = `${startLeft + dx}px`;
            item.style.top = `${startTop + dy}px`;
          }
          clampItem(item);
          updateHidden();
        };
        const onUp = () => {
          document.removeEventListener("pointermove", onMove);
          document.removeEventListener("pointerup", onUp);
          updateHidden();
        };
        document.addEventListener("pointermove", onMove);
        document.addEventListener("pointerup", onUp);
      });
    });

    q("[data-manual-zoom-in]", root)?.addEventListener("click", () => {
      zoom = Math.min(2.2, zoom + 0.15);
      if (paper) paper.style.width = `${Math.round(zoom * 900)}px`;
      updateHidden();
    });
    q("[data-manual-zoom-out]", root)?.addEventListener("click", () => {
      zoom = Math.max(0.55, zoom - 0.15);
      if (paper) paper.style.width = `${Math.round(zoom * 900)}px`;
      updateHidden();
    });
    q("[data-manual-rotate]", root)?.addEventListener("click", () => {
      rotation = (rotation + 90) % 360;
      updatePageSrc();
      if (message) message.textContent = `页面已旋转 ${rotation}°，请重新确认签名、公章和日期位置。`;
    });
    q("[data-manual-reset]", root)?.addEventListener("click", placeDefaults);
    q("[data-manual-preview]", root)?.addEventListener("click", () => {
      updateHidden();
      if (message) message.textContent = "当前画面就是最终版位置预览；确认无误后点击“确认生成最终合同”。";
    });
    pageSelect?.addEventListener("change", () => {
      updatePageSrc();
      if (message) message.textContent = "已切换页面，请确认只在 AAIE 甲方区域放置签名、公章和日期。";
    });
    image?.addEventListener("load", () => {
      placeDefaults();
    });
    form?.addEventListener("submit", () => updateHidden());
    if (image?.complete) placeDefaults();
  });

  qa("form[data-duplicate-upload]").forEach((form) => {
    const fileInput = q('input[type="file"]', form);
    const actionInput = q('input[name="duplicate_action"]', form);
    const dialog = q("[data-duplicate-dialog]");
    const duplicateList = q("[data-duplicate-list]", dialog || document);
    let existing = [];
    try {
      existing = JSON.parse(form.dataset.existingFiles || "[]").map((name) => String(name).toLowerCase());
    } catch (_) {
      existing = [];
    }
    const existingSet = new Set(existing);

    form.addEventListener("submit", (event) => {
      if (!fileInput || !actionInput || actionInput.value) return;
      const duplicates = Array.from(fileInput.files || []).filter((file) => existingSet.has(file.name.toLowerCase()));
      if (!duplicates.length) {
        actionInput.value = "version";
        return;
      }
      event.preventDefault();
      if (!dialog || !duplicateList) {
        const choice = window.prompt(`发现同名文件：\n${duplicates.map((file) => file.name).join("\n")}\n\n请输入 1、2 或 3：\n1. 保留原文件，上传为新版本\n2. 替换原文件\n3. 取消上传`, "1");
        if (choice === "1") actionInput.value = "version";
        if (choice === "2") actionInput.value = "replace";
        if (actionInput.value) form.requestSubmit();
        return;
      }
      duplicateList.innerHTML = "";
      duplicates.forEach((file) => {
        const item = document.createElement("li");
        item.textContent = file.name;
        duplicateList.appendChild(item);
      });
      dialog.showModal();
    });

    qa("[data-duplicate-action]", dialog || document).forEach((button) => {
      button.addEventListener("click", () => {
        const action = button.dataset.duplicateAction;
        if (action === "cancel") {
          actionInput.value = "";
          dialog?.close();
          return;
        }
        actionInput.value = action;
        dialog?.close();
        form.requestSubmit();
      });
    });
  });

  const initUploadReviewWorkbench = (root) => {
    const previewRoot = q("[data-review-preview-root]", root);
    if (!previewRoot) return;
    const image = q("[data-review-preview-image]", previewRoot);
    const canvas = q("[data-review-preview-canvas]", previewRoot);
    const indicator = q("[data-review-preview-indicator]", previewRoot);
    const prev = q("[data-review-preview-prev]", previewRoot);
    const next = q("[data-review-preview-next]", previewRoot);
    const zoomSelect = q("[data-review-preview-zoom]", previewRoot);
    const thumbnails = q("[data-review-preview-thumbnails]", previewRoot);
    const thumbnailToggle = q("[data-review-preview-thumbnail-toggle]", previewRoot);
    const jumpInput = q("[data-review-preview-jump-input]", previewRoot);
    const jumpButton = q("[data-review-preview-jump-button]", previewRoot);
    const message = q("[data-review-preview-message]", previewRoot);
    const highlight = q("[data-review-source-highlight]", previewRoot);
    const search = q("[data-review-preview-search]", previewRoot);
    const baseSrc = previewRoot.dataset.previewSrc || "";
    let page = 1;
    let pageCount = 1;
    let zoom = 1;
    let rotation = 0;
    let objectUrl = "";
    let thumbnailObserver = null;
    const thumbnailUrls = new Map();
    const thumbnailLoading = new Set();
    const previewUrl = (targetPage) => {
      const url = new URL(baseSrc, window.location.origin);
      url.searchParams.set("page", String(targetPage));
      return `${url.pathname}${url.search}${url.hash}`;
    };
    const previewSizeUrl = (targetPage, size = "large") => {
      const url = new URL(baseSrc, window.location.origin);
      url.searchParams.set("page", String(targetPage));
      url.searchParams.set("size", size);
      return `${url.pathname}${url.search}${url.hash}`;
    };
    const setReviewMessage = (value, kind = "") => {
      if (!message) return;
      message.textContent = value || "";
      message.dataset.kind = kind;
      if (value) {
        window.setTimeout(() => {
          if (message.textContent === value) message.textContent = "";
        }, 2600);
      }
    };
    const clearObjectUrl = () => {
      if (objectUrl) URL.revokeObjectURL(objectUrl);
      objectUrl = "";
    };
    const clearThumbnailUrls = () => {
      thumbnailUrls.forEach((url) => URL.revokeObjectURL(url));
      thumbnailUrls.clear();
      thumbnailLoading.clear();
      if (thumbnailObserver) thumbnailObserver.disconnect();
    };
    const applyTransform = () => {
      if (!image) return;
      image.style.transform = `rotate(${rotation}deg) scale(${zoom})`;
      image.style.transformOrigin = "top center";
      if (zoomSelect) zoomSelect.value = String(zoom);
    };
    const updateControls = () => {
      if (indicator) indicator.textContent = `${page} / ${pageCount}`;
      if (prev) prev.disabled = page <= 1;
      if (next) next.disabled = page >= pageCount;
      if (thumbnails) {
        qa("[data-review-thumbnail-page]", thumbnails).forEach((button) => {
          button.classList.toggle("active", Number(button.dataset.reviewThumbnailPage) === page);
        });
      }
      if (jumpInput) {
        jumpInput.placeholder = String(page);
        jumpInput.max = String(pageCount);
      }
    };
    const loadReviewThumbnail = async (targetPage) => {
      if (!thumbnails || thumbnailUrls.has(targetPage) || thumbnailLoading.has(targetPage)) return;
      const button = q(`[data-review-thumbnail-page="${targetPage}"]`, thumbnails);
      if (!button) return;
      thumbnailLoading.add(targetPage);
      try {
        const response = await fetch(previewSizeUrl(targetPage, "thumb"), { credentials: "same-origin", cache: "no-store" });
        if (!response.ok || !String(response.headers.get("Content-Type") || "").startsWith("image/")) throw new Error("thumbnail unavailable");
        const blob = await response.blob();
        const objectThumbUrl = URL.createObjectURL(blob);
        thumbnailUrls.set(targetPage, objectThumbUrl);
        const thumbImage = q("img", button);
        if (thumbImage) thumbImage.src = objectThumbUrl;
      } catch (_) {
        button.dataset.error = "true";
      } finally {
        thumbnailLoading.delete(targetPage);
      }
    };
    const observeReviewThumbnails = () => {
      if (!thumbnails) return;
      if (thumbnailObserver) thumbnailObserver.disconnect();
      if (!("IntersectionObserver" in window)) {
        qa("[data-review-thumbnail-page]", thumbnails).slice(0, 12).forEach((button) => loadReviewThumbnail(Number(button.dataset.reviewThumbnailPage)));
        return;
      }
      thumbnailObserver = new IntersectionObserver((entries) => {
        entries.forEach((entry) => {
          if (!entry.isIntersecting) return;
          loadReviewThumbnail(Number(entry.target.dataset.reviewThumbnailPage));
        });
      }, { root: thumbnails, rootMargin: "180px 0px" });
      qa("[data-review-thumbnail-page]", thumbnails).forEach((button) => thumbnailObserver.observe(button));
    };
    const renderReviewThumbnails = () => {
      if (!thumbnails || thumbnails.dataset.pageCount === String(pageCount)) return;
      thumbnails.dataset.pageCount = String(pageCount);
      thumbnails.innerHTML = "";
      clearThumbnailUrls();
      for (let itemPage = 1; itemPage <= pageCount; itemPage += 1) {
        const item = document.createElement("button");
        item.type = "button";
        item.dataset.reviewThumbnailPage = String(itemPage);
        item.innerHTML = `<span>${itemPage}</span><img alt="第 ${itemPage} 页缩略图">`;
        item.addEventListener("click", () => loadPage(itemPage));
        thumbnails.appendChild(item);
      }
      observeReviewThumbnails();
    };
    const showHighlight = (text) => {
      if (!highlight) return;
      highlight.textContent = text || "已跳转到字段来源附近，请对照原文确认。";
      highlight.hidden = false;
      highlight.classList.add("is-active");
      clearTimeout(highlight._hideTimer);
      highlight._hideTimer = setTimeout(() => {
        highlight.classList.remove("is-active");
        highlight.hidden = true;
      }, 4200);
    };
    async function loadPage(targetPage, snippet = "") {
      if (!baseSrc || !image) return;
      const safePage = Math.max(1, Math.min(pageCount || 1, Number(targetPage) || 1));
      canvas?.setAttribute("aria-busy", "true");
      try {
        const response = await fetch(previewUrl(safePage), { credentials: "same-origin", cache: "no-store" });
        if (!response.ok) throw new Error("preview unavailable");
        const total = parseInt(response.headers.get("X-Preview-Page-Count") || "1", 10);
        const current = parseInt(response.headers.get("X-Preview-Page") || String(safePage), 10);
        pageCount = Number.isFinite(total) && total > 0 ? total : 1;
        page = Number.isFinite(current) && current > 0 ? current : safePage;
        const blob = await response.blob();
        clearObjectUrl();
        objectUrl = URL.createObjectURL(blob);
        image.hidden = false;
        image.src = objectUrl;
        renderReviewThumbnails();
        updateControls();
        applyTransform();
        if (snippet) showHighlight(snippet);
      } catch (_error) {
        image.hidden = true;
        if (canvas) canvas.dataset.error = "此文件暂不支持在线预览，请下载查看。";
      } finally {
        canvas?.setAttribute("aria-busy", "false");
      }
    }
    prev?.addEventListener("click", () => loadPage(page - 1));
    next?.addEventListener("click", () => loadPage(page + 1));
    const jumpReviewPreviewPage = () => {
      const raw = String(jumpInput?.value || "").trim();
      const target = Number(raw);
      if (!raw || !Number.isInteger(target) || target < 1) {
        setReviewMessage("请输入有效页码", "error");
        return;
      }
      if (target > pageCount) {
        setReviewMessage("页码超出范围", "error");
        return;
      }
      setReviewMessage("");
      loadPage(target);
      if (jumpInput) jumpInput.value = "";
    };
    jumpButton?.addEventListener("click", jumpReviewPreviewPage);
    jumpInput?.addEventListener("keydown", (event) => {
      if (event.key !== "Enter") return;
      event.preventDefault();
      jumpReviewPreviewPage();
    });
    thumbnailToggle?.addEventListener("click", () => {
      if (!previewRoot) return;
      const collapsed = previewRoot.classList.toggle("review-thumbnails-collapsed");
      thumbnailToggle.textContent = collapsed ? "显示缩略图" : "隐藏缩略图";
    });
    zoomSelect?.addEventListener("change", () => {
      zoom = Number(zoomSelect.value) || 1;
      applyTransform();
    });
    q("[data-review-preview-zoom-in]", previewRoot)?.addEventListener("click", () => {
      zoom = Math.min(2, Math.round((zoom + 0.25) * 100) / 100);
      applyTransform();
    });
    q("[data-review-preview-zoom-out]", previewRoot)?.addEventListener("click", () => {
      zoom = Math.max(0.5, Math.round((zoom - 0.25) * 100) / 100);
      applyTransform();
    });
    q("[data-review-preview-fit-width]", previewRoot)?.addEventListener("click", () => {
      zoom = 1;
      image?.classList.remove("fit-page");
      image?.classList.add("fit-width");
      applyTransform();
    });
    q("[data-review-preview-fit-page]", previewRoot)?.addEventListener("click", () => {
      zoom = 1;
      image?.classList.remove("fit-width");
      image?.classList.add("fit-page");
      applyTransform();
    });
    q("[data-review-preview-rotate]", previewRoot)?.addEventListener("click", () => {
      rotation = (rotation + 90) % 360;
      applyTransform();
    });
    q("[data-review-preview-open-window]", previewRoot)?.addEventListener("click", () => {
      if (baseSrc) window.open(previewUrl(page), "_blank", "noopener");
    });
    q("[data-review-preview-fullscreen]", previewRoot)?.addEventListener("click", async () => {
      if (document.fullscreenElement) {
        await document.exitFullscreen();
      } else if (root.requestFullscreen) {
        await root.requestFullscreen();
      }
    });
    search?.addEventListener("change", () => {
      const query = search.value.trim().toLowerCase();
      if (!query) return;
      const source = qa("[data-source-snippet]", root).find((item) => (item.dataset.sourceSnippet || "").toLowerCase().includes(query));
      if (source) {
        loadPage(parseInt(source.dataset.sourcePage || "1", 10), source.dataset.sourceSnippet || "");
        source.scrollIntoView({ block: "center", behavior: "smooth" });
      } else {
        showHighlight("未在已提取原文片段中找到该搜索词。");
      }
    });
    root.addEventListener("toggle", (event) => {
      const details = event.target.closest?.(".extraction-source");
      if (!details || !details.open) return;
      const field = details.closest("[data-source-page]");
      if (!field) return;
      loadPage(parseInt(field.dataset.sourcePage || "1", 10), field.dataset.sourceSnippet || "");
    }, true);
    const splitter = q("[data-review-splitter]", root);
    splitter?.addEventListener("pointerdown", (event) => {
      event.preventDefault();
      splitter.setPointerCapture?.(event.pointerId);
      const move = (moveEvent) => {
        const rect = root.getBoundingClientRect();
        const pct = Math.max(42, Math.min(78, ((moveEvent.clientX - rect.left) / rect.width) * 100));
        root.style.setProperty("--review-preview-fr", `${pct}fr`);
        root.style.setProperty("--review-fields-fr", `${100 - pct}fr`);
        localStorage.setItem("aaie-upload-review-preview-split", String(Math.round(pct)));
      };
      const up = () => {
        window.removeEventListener("pointermove", move);
        window.removeEventListener("pointerup", up);
      };
      window.addEventListener("pointermove", move);
      window.addEventListener("pointerup", up);
    });
    const savedSplit = parseInt(localStorage.getItem("aaie-upload-review-preview-split") || "", 10);
    if (Number.isFinite(savedSplit)) {
      const pct = Math.max(42, Math.min(78, savedSplit));
      root.style.setProperty("--review-preview-fr", `${pct}fr`);
      root.style.setProperty("--review-fields-fr", `${100 - pct}fr`);
    }
    document.addEventListener("fullscreenchange", () => {
      root.classList.toggle("is-fullscreen", document.fullscreenElement === root);
    });
    loadPage(1);
    window.addEventListener("beforeunload", () => {
      clearObjectUrl();
      clearThumbnailUrls();
    });
  };

  qa("[data-upload-review-workbench]").forEach(initUploadReviewWorkbench);

  qa("[data-extraction-copy]").forEach((button) => {
    button.addEventListener("click", async () => {
      const card = button.closest(".extraction-field");
      const text = card?.querySelector("blockquote")?.textContent?.trim() || "";
      if (!text) return;
      try {
        await navigator.clipboard.writeText(text);
        button.textContent = "已复制";
        setTimeout(() => {
          button.textContent = "复制原文";
        }, 1200);
      } catch (_) {
        window.prompt("复制原文片段", text);
      }
    });
  });

  const updateExtractionReview = (select) => {
    const card = select.closest(".review-card") || document;
    const chosen = select.value;
    const optionalTypes = (select.dataset.optionalDocumentTypes || "").split(/\s+/).filter(Boolean);
    const isOptional = optionalTypes.includes(chosen);
    const help = q("[data-document-type-help]", card);
    if (help) {
      help.textContent = isOptional
        ? "这是可选文件：不是每个学生都有，不上传也不会影响 COE/OSHC 完整性；如果已上传，确认保存即可。"
        : "这是为了保存和命名当前上传文件；不是说所有文件类型都必须上传。";
    }
    const rows = qa("[data-extraction-field-row]", card);
    let visibleCount = 0;
    rows.forEach((row) => {
      const allowed = (row.dataset.extractionFor || "").split(/\s+/).filter(Boolean);
      const show = !allowed.length || (chosen && allowed.includes(chosen));
      row.hidden = !show;
      if (show) visibleCount += 1;
    });
    const hint = q("[data-extraction-empty-hint]", card);
    if (hint) {
      if (!chosen) {
        hint.textContent = "请选择 DocumentType，系统只显示需要审核的字段。";
        hint.hidden = false;
      } else if (!visibleCount) {
        hint.textContent = isOptional
          ? "Letter of Confirmation 是可选文件；如果上传，系统会读取 Enrolment Details 字段，未识别到的内容请人工补充。"
          : "这个文件类型不需要自动提取字段，确认文件类型后即可保存。";
        hint.hidden = false;
      } else {
        hint.hidden = true;
      }
    }
  };

  qa("[data-document-type-select]").forEach((select) => {
    updateExtractionReview(select);
    select.addEventListener("change", () => updateExtractionReview(select));
  });

  const saveOfferForm = async (form) => {
    if (typeof form.reportValidity === "function" && !form.reportValidity()) return false;
    const row = form.closest(".student-file-record") || form.closest("tr") || document;
    const status = row.querySelector("[data-save-status]");
    const button = document.querySelector(`[form="${CSS.escape(form.id)}"]`) || form.querySelector('button[type="submit"], button:not([type])');
    const originalText = button?.textContent || "保存";
    if (status) status.textContent = "保存中...";
    if (button) {
      button.disabled = true;
      button.textContent = "保存中";
    }
    try {
      const response = await fetch(form.action, {
        method: "POST",
        body: new FormData(form),
        credentials: "same-origin",
        headers: {
          "Accept": "application/json",
          "X-Requested-With": "fetch",
        },
      });
      const data = await response.json().catch(() => ({}));
      if (!response.ok || data.ok === false) {
        throw new Error(data.error || data.message || "保存失败");
      }
      if (status) status.textContent = "已保存";
      row.classList.add("row-saved");
      setTimeout(() => {
        if (status?.textContent === "已保存") status.textContent = "";
        row.classList.remove("row-saved");
      }, 1800);
      return true;
    } catch (error) {
      if (status) status.textContent = error.message || "保存失败";
      row.classList.add("row-save-error");
      return false;
    } finally {
      if (button) {
        button.disabled = false;
        button.textContent = originalText;
      }
    }
  };

  const initOfferPanel = (root = document) => {
    qa("[data-offer-school-search]", root).forEach((input) => {
      if (input.dataset.offerSchoolSearchBound === "true") return;
      const form = input.closest("form");
      const select = form ? q('select[name="school_id"]', form) : null;
      if (!select) return;
      const status = q("[data-offer-school-search-status]", form);
      const choices = Array.from(select.options);
      const filter = () => {
        const query = input.value.trim().toLowerCase();
        const selected = select.value;
        const matches = new Set(choices.filter((option) => option.value
          && option.textContent.toLowerCase().includes(query)));
        select.replaceChildren(...choices.filter((option) => !option.value
          || option.value === selected || matches.has(option)));
        select.value = selected;
        if (status) status.textContent = !query
          ? "输入名称筛选；选择学校后保存，搜索不会自动更改当前匹配。"
          : matches.size ? `找到 ${matches.size} 所学校；当前匹配保留，选择学校后保存。`
            : "未找到匹配学校；当前学校选择未更改。";
      };
      input.dataset.offerSchoolSearchBound = "true";
      input.addEventListener("input", filter);
      input.addEventListener("keydown", (event) => {
        if (event.key === "Enter") event.preventDefault();
      });
      select.addEventListener("change", filter);
    });

    qa("form[data-ajax-offer]", root).forEach((form) => {
      if (form.dataset.ajaxOfferBound === "true") return;
      form.dataset.ajaxOfferBound = "true";
      form.addEventListener("submit", async (event) => {
        event.preventDefault();
        await saveOfferForm(form);
      });
    });

    qa("[data-offer-save-all]", root).forEach((button) => {
      if (button.dataset.offerSaveAllBound === "true") return;
      button.dataset.offerSaveAllBound = "true";
      button.addEventListener("click", async () => {
      const panel = button.closest(".offer-panel") || document;
      const forms = qa("form[data-ajax-offer]", panel);
      const originalText = button.textContent;
      button.disabled = true;
      button.textContent = "保存中...";
      let ok = 0;
      for (const form of forms) {
        if (await saveOfferForm(form)) ok += 1;
      }
      button.textContent = `已保存 ${ok}/${forms.length}`;
      setTimeout(() => {
        button.textContent = originalText;
        button.disabled = false;
      }, 1600);
    });
    });
  };
  initOfferPanel(document);

  const submitOfferAcceptance = async (form, warningAcknowledged = false) => {
    const panel = form.closest('[data-panel="offer"]');
    const live = q("[data-offer-acceptance-live]", panel || document);
    const button = q('button[type="submit"]', form);
    const originalText = button?.textContent || "设为已接受";
    if (button) {
      button.disabled = true;
      button.textContent = "处理中...";
    }
    if (live) live.textContent = "正在更新已接受 Offer...";
    try {
      const payload = new FormData(form);
      payload.set("coe_warning_acknowledged", warningAcknowledged ? "1" : "0");
      const response = await fetch(form.action, {
        method: "POST",
        body: payload,
        credentials: "same-origin",
        headers: { "Accept": "application/json", "X-Requested-With": "fetch" },
      });
      const data = await response.json().catch(() => ({}));
      if (response.status === 409 && data.requires_coe_warning && !warningAcknowledged) {
        const previous = data.previous_school_name || "当前学校";
        const next = data.new_school_name || "新学校";
        const confirmed = window.confirm(
          `已有 COE。你正在把已接受 Offer 从“${previous}”更改为“${next}”。这可能影响现有 COE。确认仍要继续吗？`
        );
        if (!confirmed) {
          if (live) live.textContent = "已取消更换，当前接受的 Offer 未改变。";
          return false;
        }
        return submitOfferAcceptance(form, true);
      }
      if (!response.ok || data.ok === false) throw new Error(data.message || data.error || "更新失败");
      if (live) live.textContent = data.message || "已更新最终接受的 Offer。";
      showCrmToast({ kind: "success", title: "Offer 已更新", message: data.message || "已更新最终接受的 Offer。" });
      if (panel?.dataset.lazyTabUrl) await refreshLazyTabPanel(panel);
      return true;
    } catch (error) {
      const message = error?.message || "更新已接受 Offer 失败";
      if (live) live.textContent = message;
      showCrmToast({ kind: "error", title: "Offer 更新失败", message });
      return false;
    } finally {
      if (button && document.body.contains(button)) {
        button.disabled = false;
        button.textContent = originalText;
      }
    }
  };

  document.addEventListener("submit", async (event) => {
    const form = event.target;
    if (!(form instanceof HTMLFormElement) || !form.matches("form[data-offer-accept-form]")) return;
    event.preventDefault();
    event.stopPropagation();
    if (form.dataset.offerAcceptSubmitting === "true") return;
    form.dataset.offerAcceptSubmitting = "true";
    try {
      await submitOfferAcceptance(form, false);
    } finally {
      delete form.dataset.offerAcceptSubmitting;
    }
  }, true);

  const saveStudentDetailAjaxForm = async (form) => {
    if (typeof form.reportValidity === "function" && !form.reportValidity()) return false;
    const panel = form.closest("[data-panel]");
    const status = q("[data-save-status]", form) || q("[data-save-status]", form.closest(".student-file-record") || form);
    const submitter = document.activeElement?.closest?.("button");
    const button = submitter?.form === form
      ? submitter
      : form.querySelector('button[type="submit"], button:not([type])');
    const originalText = button?.textContent || "保存";
    if (status) status.textContent = "保存中...";
    if (button) {
      button.disabled = true;
      button.textContent = "保存中";
    }
    try {
      const response = await fetch(form.action, {
        method: "POST",
        body: new FormData(form),
        credentials: "same-origin",
        headers: {
          "Accept": "application/json",
          "X-Requested-With": "fetch",
        },
      });
      const data = await response.json().catch(() => ({}));
      if (!response.ok || data.ok === false) {
        throw new Error(data.error || data.message || "保存失败");
      }
      if (status) status.textContent = "已保存";
      if (data.message) showCrmToast({ kind: "success", title: "保存成功", message: data.message });
      form.closest("dialog")?.close();
      const shouldRefreshPanel = form.dataset.refreshPanel !== "false";
      if (shouldRefreshPanel && panel?.dataset.lazyTabUrl) {
        const refreshed = await refreshLazyTabPanel(panel);
        if (!refreshed && status) status.textContent = "已保存，请刷新查看最新内容";
      }
      return true;
    } catch (error) {
      const message = error?.message || "保存失败";
      if (status) status.textContent = message;
      showCrmToast({ kind: "error", title: "保存失败", message });
      return false;
    } finally {
      if (button) {
        button.disabled = false;
        button.textContent = originalText;
      }
    }
  };

  const initStudentDetailAjaxForms = (root = document) => {
    qa("form[data-ajax-student-tab-form]", root).forEach((form) => {
      if (form.dataset.ajaxStudentTabBound === "true") return;
      form.dataset.ajaxStudentTabBound = "true";
    });
  };
  document.addEventListener("submit", async (event) => {
    const form = event.target;
    if (!(form instanceof HTMLFormElement) || !form.matches("form[data-ajax-student-tab-form]")) return;
    event.preventDefault();
    event.stopPropagation();
    if (form.dataset.ajaxStudentSubmitting === "true") return;
    form.dataset.ajaxStudentSubmitting = "true";
    try {
      await saveStudentDetailAjaxForm(form);
    } finally {
      delete form.dataset.ajaxStudentSubmitting;
    }
  }, true);
  initStudentDetailAjaxForms(document);

  qa("[data-school-radius-leaflet], [data-school-living-cost-leaflet]").forEach((shell) => {
    const container = q("[data-school-radius-leaflet-map]", shell);
    const dataEl = q("#school-radius-leaflet-data", shell);
    let tileLoadFailed = false;
    const showMapFailure = () => {
      if (tileLoadFailed) return;
      tileLoadFailed = true;
      shell.classList.remove("is-ready");
      const notice = document.createElement("p");
      notice.className = "notice warn";
      notice.setAttribute("role", "status");
      notice.textContent = "地图底图暂时无法加载。其他学校信息仍可使用；可查看静态示意图。";
      shell.appendChild(notice);
    };
    if (!container || !dataEl) return;
    if (!window.L) {
      showMapFailure();
      return;
    }
    let data = {};
    try {
      data = JSON.parse(dataEl.textContent || "{}");
    } catch (_) {
      return;
    }
    if (!data.center || data.center.latitude == null || data.center.longitude == null) return;
    const center = [Number(data.center.latitude), Number(data.center.longitude)];
    const isMobileRadiusSearch = document.body.classList.contains("mobile-business-page")
      && Boolean(shell.closest(".school-radius-search-page"));
    const mapOptions = { scrollWheelZoom: false };
    if (isMobileRadiusSearch) {
      Object.assign(mapOptions, {
        dragging: false,
        touchZoom: false,
        doubleClickZoom: false,
        boxZoom: false,
        keyboard: false,
      });
    }
    const map = L.map(container, mapOptions).setView(center, 12);
    L.tileLayer("https://tile.openstreetmap.org/{z}/{x}/{y}.png", {
      maxZoom: 19,
      referrerPolicy: 'origin',
      attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors',
    }).on("tileerror", showMapFailure).addTo(map);
    const centerMarker = L.circleMarker(center, {
      radius: 8,
      color: "#ffffff",
      weight: 3,
      fillColor: "#d97706",
      fillOpacity: 0.95,
    }).addTo(map);
    centerMarker.bindPopup(escapeHtml(data.center.label || "Search centre"));
    const bounds = L.latLngBounds([center]);
    (data.circle_radii_km || []).forEach((radius) => {
      const radiusKm = Number(radius);
      const circle = L.circle(center, {
        radius: radiusKm * 1000,
        color: "#2563eb",
        weight: radiusKm === Number(data.radius_km) ? 2 : 1,
        opacity: radiusKm === Number(data.radius_km) ? 0.55 : 0.28,
        fillOpacity: radiusKm === Number(data.radius_km) ? 0.08 : 0.025,
      }).addTo(map);
      bounds.extend(circle.getBounds());
    });
    (data.areas || []).forEach((area) => {
      if (area.latitude == null || area.longitude == null) return;
      const point = [Number(area.latitude), Number(area.longitude)];
      L.circleMarker(point, {
        radius: 5,
        color: "#ffffff",
        weight: 1.5,
        fillColor: "#16a34a",
        fillOpacity: 0.86,
      }).addTo(map).bindPopup(
        `<strong>${escapeHtml(area.name || "Area")}</strong><br>${escapeHtml(area.distance_text || "")}`
      );
      bounds.extend(point);
    });
    if (data.cbd && data.cbd.latitude != null && data.cbd.longitude != null) {
      const cbdPoint = [Number(data.cbd.latitude), Number(data.cbd.longitude)];
      L.circleMarker(cbdPoint, {
        radius: 7,
        color: "#ffffff",
        weight: 2,
        fillColor: "#111827",
        fillOpacity: 0.9,
      }).addTo(map).bindPopup(`<strong>${escapeHtml(data.cbd.label || "CBD")}</strong><br>${escapeHtml(data.cbd.distance_text || "")}`);
      L.polyline([center, cbdPoint], { color: "#7c3aed", weight: 2, dashArray: "8 7", opacity: 0.72 }).addTo(map);
      bounds.extend(cbdPoint);
      const cbdControl = L.control({ position: "topleft" });
      cbdControl.onAdd = () => {
        const div = L.DomUtil.create("div", "school-radius-cbd-badge");
        div.textContent = data.cbd.distance_text || `To ${data.cbd.label || "CBD"}`;
        return div;
      };
      cbdControl.addTo(map);
    }
    let lineTarget = null;
    (data.schools || []).forEach((school, index) => {
      if (school.latitude == null || school.longitude == null) return;
      const point = [Number(school.latitude), Number(school.longitude)];
      const marker = L.circleMarker(point, {
        radius: school.selected ? 8 : 6,
        color: school.selected ? "#d97706" : "#ffffff",
        weight: school.selected ? 3 : 1.5,
        fillColor: school.sector === "private" ? "#2563eb" : "#16a34a",
        fillOpacity: 0.9,
      }).addTo(map);
      marker.bindPopup(
        `<strong>${escapeHtml(school.name)}</strong><br>${escapeHtml(school.distance_text)} ${escapeHtml(school.direction)}<br>${escapeHtml(school.coordinate_source)}`
      );
      bounds.extend(point);
      if (school.selected || (!lineTarget && index === 0)) lineTarget = point;
    });
    if (lineTarget) L.polyline([center, lineTarget], { color: "#d97706", weight: 2, dashArray: "7 5" }).addTo(map);
    if (bounds.isValid()) map.fitBounds(bounds.pad(0.18));
    setTimeout(() => map.invalidateSize(), 80);
    if (!tileLoadFailed) shell.classList.add("is-ready");
  });

  qa("[data-living-cost-toggle]").forEach((toggle) => {
    const panel = toggle.closest(".school-living-cost-panel") || document;
    const setView = (view) => {
      qa("[data-living-cost-view]", toggle).forEach((button) => {
        button.classList.toggle("active", button.dataset.livingCostView === view);
      });
      qa("[data-living-cost-metric]", panel).forEach((item) => {
        item.hidden = item.dataset.livingCostMetric !== view;
      });
    };
    qa("[data-living-cost-view]", toggle).forEach((button) => {
      button.addEventListener("click", () => setView(button.dataset.livingCostView || "rent"));
    });
    setView("rent");
  });

  const initSchoolPlanStudentAutoLoad = () => {
    qa("[data-school-plan-student-form]").forEach((form) => {
      if (form.dataset.studentAutoLoadWired === "true") return;
      const select = q("[data-school-plan-student-select]", form);
      if (!select) return;
      form.dataset.studentAutoLoadWired = "true";
      let lastSubmittedValue = form.dataset.schoolPlanSelectedStudentId || select.value || "";
      const syncSelectedStudent = () => {
        qa("[data-school-plan-selected-student-id]").forEach((input) => {
          input.value = select.value || "";
        });
      };
      const submitSelectedStudent = () => {
        syncSelectedStudent();
        const nextValue = select.value || "";
        if (nextValue === lastSubmittedValue) return;
        lastSubmittedValue = nextValue;
        if (typeof form.requestSubmit === "function") {
          form.requestSubmit();
        } else {
          form.submit();
        }
      };
      select.addEventListener("change", submitSelectedStudent);
      syncSelectedStudent();
    });
  };
  initSchoolPlanStudentAutoLoad();

  const findSchoolRecord = (schools, value, exactOnly = false) => {
    const query = String(value || "").trim().toLowerCase();
    if (!query) return null;
    const exact = schools.find((school) => (
      String(school.label || "").toLowerCase() === query
      || String(school.name || "").toLowerCase() === query
    ));
    if (exact || exactOnly) return exact || null;
    return schools.find((school) => (
      String(school.label || "").toLowerCase().includes(query)
      || String(school.name || "").toLowerCase().includes(query)
      || String(school.suburb || "").toLowerCase().includes(query)
      || String(school.state || "").toLowerCase().includes(query)
      || String(school.postcode || "").toLowerCase().includes(query)
    )) || null;
  };

  initTransferSchoolPickers = (root = document) => {
    qa("[data-transfer-school-picker]", root).forEach((picker) => {
      if (picker.dataset.initialized === "true") return;
      const input = q("[data-transfer-school-search]", picker);
      const schoolId = q("[data-transfer-school-id]", picker);
      const schoolName = q("[data-transfer-school-name]", picker);
      const source = q("[data-transfer-school-options]", picker);
      const form = picker.closest("form");
      const city = form ? q("[data-transfer-city]", form) : null;
      const sector = form ? q("[data-transfer-school-sector]", form) : null;
      if (!input || !schoolId || !schoolName || !source) return;
      let schools = [];
      try {
        schools = JSON.parse(source.textContent || "[]");
      } catch (_) {
        schools = [];
      }
      const sync = () => {
        const school = findSchoolRecord(schools, input.value, true);
        schoolId.value = school ? school.id : "";
        schoolName.value = school ? school.name : "";
        if (school && city && school.city_key) city.value = school.city_key;
        if (school && sector && school.sector_key) sector.value = school.sector_key;
      };
      picker.dataset.initialized = "true";
      input.addEventListener("input", sync);
      input.addEventListener("change", sync);
    });
  };
  initTransferSchoolPickers();

  const formatDateInputValue = (value) => {
    const digits = String(value || "").replace(/\D/g, "").slice(0, 8);
    if (digits.length <= 2) return digits;
    if (digits.length <= 4) return `${digits.slice(0, 2)}/${digits.slice(2)}`;
    return `${digits.slice(0, 2)}/${digits.slice(2, 4)}/${digits.slice(4)}`;
  };
  const isValidDisplayDate = (value) => {
    const match = String(value || "").match(/^(\d{2})\/(\d{2})\/(\d{4})$/);
    if (!match) return false;
    const day = Number(match[1]);
    const month = Number(match[2]);
    const year = Number(match[3]);
    const parsed = new Date(year, month - 1, day);
    return parsed.getFullYear() === year && parsed.getMonth() === month - 1 && parsed.getDate() === day;
  };
  const syncDateInputValidity = (input) => {
    const value = String(input.value || "").trim();
    input.setCustomValidity(value && !isValidDisplayDate(value) ? "请按 dd/mm/yyyy 格式填写有效日期" : "");
  };
  const initDateInputMasks = (root) => {
    qa("[data-date-input]", root).forEach((input) => {
      const sync = () => {
        input.value = formatDateInputValue(input.value);
        syncDateInputValidity(input);
      };
      input.addEventListener("input", sync);
      input.addEventListener("blur", sync);
      syncDateInputValidity(input);
    });
  };
  initDateInputMasks(document);

  const valueOf = (name) => {
    const checked = q(`[name="${name}"]:checked`);
    if (checked) return checked.value;
    return q(`[name="${name}"]`)?.value || "";
  };
  const checkedValues = (name) => qa(`[name="${name}"]:checked`).map((item) => item.value);
  const show = (names, visible) => {
    names.forEach((name) => {
      const field = q(`[data-field="${name}"]`);
      if (!field) return;
      field.hidden = !visible;
      qa("input, select, textarea", field).forEach((control) => {
        if (!visible && control.type !== "hidden") control.required = false;
        if (visible && field.dataset.required === "1") {
          if (field.dataset.kind === "checkbox") return;
          if (field.dataset.kind === "radio") {
            control.required = control === qa("input", field)[0];
          } else {
            control.required = true;
          }
        }
      });
    });
  };
  const applyConditions = () => {
    const needsParent = valueOf("requires_parent_accompaniment") === "是";
    const wantsHomestay = checkedValues("accommodation_preference").includes("寄宿家庭");
    show(["sibling_1_name", "sibling_1_gender", "sibling_1_dob", "sibling_1_school_company", "sibling_1_year_level",
      "sibling_2_name", "sibling_2_gender", "sibling_2_dob", "sibling_2_school_company", "sibling_2_year_level"], true);
    show(["accompanying_person_relationship"], needsParent);
    show(["accommodation_preference", "accommodation_notes"], !needsParent);
    show(["accept_homestay_with_young_children", "homestay_style_preference"], !needsParent && wantsHomestay);
    show(["smoke_allergy", "food_allergy", "food_allergy_notes", "pet_allergy", "pet_allergy_notes", "medicine_allergy", "medicine_allergy_notes"], !needsParent);
    show(["food_allergy_notes"], !needsParent && valueOf("food_allergy") === "是");
    show(["pet_allergy_notes"], !needsParent && valueOf("pet_allergy") === "是");
    show(["medicine_allergy_notes"], !needsParent && valueOf("medicine_allergy") === "是");
    show(["medical_history_notes"], valueOf("has_medical_or_psychological_history") === "有");
    show(["medication_notes"], valueOf("requires_long_term_medication") === "有");
    show(["student_visa_refusal_notes"], valueOf("student_australia_visa_refusal") === "有");
    show(["parents_australia_visa_refusal_person"], valueOf("parents_australia_visa_refusal") === "有");
    show(["father_migration_notes"], valueOf("father_migration_application_status") === "是");
    show(["mother_migration_notes"], valueOf("mother_migration_application_status") === "是");
    show(["aeas_idat_score"], valueOf("has_aeas_or_idat") === "是");
  };
  if (q(".parent-form")) {
    const parentForm = q(".parent-form");
    qa("input,select", parentForm).forEach((control) => control.addEventListener("change", applyConditions));
    applyConditions();
  }

  const initPublicParentFormSubmit = () => {
    const form = q("[data-public-parent-form]");
    if (!form) return;
    const status = q("[data-parent-form-status]", form);
    const submitButton = q("[data-parent-form-submit]", form);
    const originalButtonText = submitButton?.textContent || "确认并提交";

    const setStatus = (message, kind = "error") => {
      if (!status) return;
      status.textContent = message || "";
      status.classList.remove("success", "error", "warn");
      status.classList.add(kind);
      status.hidden = !message;
    };

    const clearClientErrors = () => {
      qa(".invalid-field", form).forEach((field) => field.classList.remove("invalid-field"));
      qa("[data-client-error]", form).forEach((error) => error.remove());
      setStatus("");
    };

    const fieldControls = (field) => qa("input, select, textarea", field).filter((control) => !control.disabled);

    const hasFieldValue = (field) => {
      const controls = fieldControls(field);
      const kind = field?.dataset?.kind || "";
      if (kind === "checkbox" || controls.some((control) => control.type === "checkbox")) {
        return controls.some((control) => control.type === "checkbox" && control.checked);
      }
      if (kind === "radio" || controls.some((control) => control.type === "radio")) {
        return controls.some((control) => control.type === "radio" && control.checked);
      }
      return controls.some((control) => String(control.value || "").trim());
    };

    const showFieldError = (field, message) => {
      if (!field) return;
      field.hidden = false;
      field.classList.add("invalid-field");
      const error = document.createElement("div");
      error.className = "field-error";
      error.dataset.clientError = "true";
      error.textContent = message;
      field.appendChild(error);
    };

    const validateForm = () => {
      clearClientErrors();
      applyConditions();
      const invalidFields = [];

      qa("[data-field]", form).forEach((field) => {
        if (field.hidden || field.dataset.required !== "1") return;
        if (!hasFieldValue(field)) {
          invalidFields.push(field);
          showFieldError(field, field.dataset.field === "declaration" ? "请先确认信息真实完整" : "请填写此必填项");
        }
      });

      if (valueOf("requires_parent_accompaniment") === "是" && !valueOf("accompanying_person_relationship")) {
        const field = q('[data-field="accompanying_person_relationship"]', form);
        invalidFields.push(field);
        showFieldError(field, "请填写此必填项");
      }
      if (valueOf("parents_australia_visa_refusal") === "有" && !valueOf("parents_australia_visa_refusal_person")) {
        const field = q('[data-field="parents_australia_visa_refusal_person"]', form);
        invalidFields.push(field);
        showFieldError(field, "请填写此必填项");
      }

      qa("[data-date-input]", form).forEach((input) => {
        syncDateInputValidity(input);
        if (input.value && !isValidDisplayDate(input.value)) {
          const field = input.closest("[data-field]");
          invalidFields.push(field);
          showFieldError(field, "请按 dd/mm/yyyy 格式填写有效日期");
        }
      });

      const firstInvalid = invalidFields.find(Boolean);
      if (firstInvalid) {
        const declarationMissing = invalidFields.some((field) => field?.dataset?.field === "declaration");
        setStatus(declarationMissing ? "请先确认信息真实完整。" : "还有必填信息未完成，请检查红色提示项目。", "error");
        firstInvalid.scrollIntoView({ behavior: "smooth", block: "center" });
        const focusTarget = q("input, select, textarea", firstInvalid);
        setTimeout(() => focusTarget?.focus?.({ preventScroll: true }), 260);
        return false;
      }
      return true;
    };

    const setSubmitting = (submitting) => {
      form.dataset.submitting = submitting ? "true" : "false";
      form.setAttribute("aria-busy", submitting ? "true" : "false");
      if (submitButton) {
        submitButton.disabled = submitting;
        submitButton.textContent = submitting ? "正在提交，请稍候…" : originalButtonText;
      }
    };

    form.addEventListener("submit", async (event) => {
      event.preventDefault();
      event.stopPropagation();
      if (form.dataset.submitting === "true") return;
      if (!validateForm()) return;
      setSubmitting(true);
      setStatus("正在提交，请稍候…", "warn");
      try {
        if (window.aaieParentFormDraft?.saveNow) {
          await window.aaieParentFormDraft.saveNow();
        }
        if (typeof window.fetch !== "function") {
          form.submit();
          return;
        }
        const submissionBody = new FormData(form);
        if (window.aaieParentFormDraft?.getRevision) {
          submissionBody.set("draft_revision", String(window.aaieParentFormDraft.getRevision()));
        }
        const response = await fetch(form.action, {
          method: "POST",
          body: submissionBody,
          credentials: "same-origin",
          headers: { "Accept": "application/json", "X-Requested-With": "fetch" },
        });
        const data = await response.json().catch(() => ({}));
        if (!response.ok || data.ok === false) {
          if (Array.isArray(data.missing_fields) && data.missing_fields.length) {
            clearClientErrors();
            data.missing_fields.forEach((name) => showFieldError(q(`[data-field="${cssEscape(name)}"]`, form), name === "declaration" ? "请先确认信息真实完整" : "请填写此必填项"));
            const firstInvalid = q(".invalid-field", form);
            setStatus(data.message || "还有必填信息未完成，请检查红色提示项目。", "error");
            firstInvalid?.scrollIntoView({ behavior: "smooth", block: "center" });
            return;
          }
          throw new Error(data.message || "提交失败，请检查网络后重试。");
        }
        form.innerHTML = `<div class="public-result"><div class="success-mark">✓</div><h1>提交成功</h1><p>提交成功，我们已收到学生信息表。</p>${data.student_id ? `<p>Student ID：<strong>${escapeHtml(data.student_id)}</strong></p>` : ""}<p>如需修改信息，请联系 AAIE 工作人员重新发送链接。</p></div>`;
        window.scrollTo({ top: 0, behavior: "smooth" });
      } catch (error) {
        const message = /[\u4e00-\u9fa5]/.test(error?.message || "") ? error.message : "提交失败，请检查网络后重试。";
        setStatus(message || "系统正在处理，请稍后再试或联系顾问。", "error");
      } finally {
        if (document.body.contains(form) && q("[data-parent-form-submit]", form)) setSubmitting(false);
      }
    });
  };
  const initAssistantWidgetIsolated = () => {
    try {
      const root = q("[data-assistant-widget]");
      if (!root || root.dataset.initialized === "true") return;
      root.dataset.initialized = "true";
      const storageKey = "aaie_assistant_widget_state";
      const toggle = q("[data-assistant-toggle]", root);
      const close = q("[data-assistant-close]", root);
      const panel = q("[data-assistant-panel]", root);
      const messages = q("[data-assistant-messages]", root);
      const status = q("[data-assistant-status]", root);
      const input = q("[data-assistant-input]", root);
      const send = q("[data-assistant-send]", root);
      if (!toggle || !panel || !messages || !input || !send) return;
      const maxStoredMessages = 10;
      const assistantQuickPrompts = [
        { label: "查学生基础信息", question: "查询学生 20269991 基础信息" },
        { label: "查学生 Offer 状态", question: "查询学生 20269991 Offer 状态" },
        { label: "查学生 COE / OSHC 状态", question: "查询学生 20269991 COE / OSHC 状态" },
        { label: "查学生缺少哪些关键文件", question: "查询学生 20269991 缺少哪些关键文件" },
        { label: "查学生最近跟进提醒", question: "查询学生 20269991 最近跟进提醒" },
        { label: "查 Data Update Health 状态", question: "查询 Data Update Health 状态" },
      ];
      let busy = false;

      const readState = () => {
        try {
          return JSON.parse(localStorage.getItem(storageKey) || "{}") || {};
        } catch (_) {
          return {};
        }
      };
      const writeState = () => {
        try {
          const storedMessages = qa("[data-assistant-message]", messages).slice(-maxStoredMessages).map((item) => ({
            role: item.dataset.assistantMessage || "assistant",
            text: item.textContent || "",
          }));
          localStorage.setItem(storageKey, JSON.stringify({
            open: !panel.hidden,
            messages: storedMessages,
          }));
        } catch (_) {}
      };
      const scrollMessages = () => {
        window.setTimeout(() => {
          messages.scrollTop = messages.scrollHeight;
        }, 0);
      };
      const setStatusText = (message, kind = "idle") => {
        if (!status) return;
        status.textContent = message || "只读模式";
        status.classList.remove("assistant-widget-isolated__status--busy", "assistant-widget-isolated__status--error");
        if (kind === "busy") status.classList.add("assistant-widget-isolated__status--busy");
        if (kind === "error") status.classList.add("assistant-widget-isolated__status--error");
      };
      const setOpen = (open) => {
        panel.hidden = !open;
        toggle.setAttribute("aria-expanded", open ? "true" : "false");
        writeState();
        if (open) {
          scrollMessages();
          window.setTimeout(() => input.focus({ preventScroll: true }), 40);
        }
      };
      const initQuickPrompts = () => {
        if (q("[data-assistant-quick-prompts]", panel)) return;
        const quickPrompts = document.createElement("div");
        quickPrompts.className = "assistant-widget-isolated__quick-prompts";
        quickPrompts.dataset.assistantQuickPrompts = "true";
        assistantQuickPrompts.forEach((prompt) => {
          const button = document.createElement("button");
          button.type = "button";
          button.className = "assistant-widget-isolated__quick-prompt";
          button.dataset.assistantQuickPrompt = prompt.label;
          button.textContent = prompt.label;
          button.addEventListener("click", () => {
            input.value = prompt.question;
            setOpen(true);
            input.focus({ preventScroll: true });
          });
          quickPrompts.appendChild(button);
        });
        panel.insertBefore(quickPrompts, messages);
      };
      const addMessage = (role, text) => {
        const item = document.createElement("div");
        item.className = `assistant-widget-isolated__message assistant-widget-isolated__message--${role === "user" ? "user" : role === "error" ? "error" : "assistant"}`;
        if (role === "error") item.classList.add("assistant-widget-isolated__error");
        item.dataset.assistantMessage = role;
        item.textContent = text || "";
        messages.appendChild(item);
        qa("[data-assistant-message]", messages).slice(0, -maxStoredMessages).forEach((oldItem) => oldItem.remove());
        scrollMessages();
        writeState();
      };
      const setBusy = (nextBusy) => {
        busy = Boolean(nextBusy);
        send.disabled = busy;
        root.setAttribute("aria-busy", busy ? "true" : "false");
        setStatusText(busy ? "正在查询..." : "只读模式", busy ? "busy" : "idle");
      };
      const askAssistant = async () => {
        if (busy) {
          input.focus({ preventScroll: true });
          return;
        }
        const question = String(input.value || "").trim();
        if (!question) {
          input.focus();
          return;
        }
        input.value = "";
        setOpen(true);
        addMessage("user", question);
        setBusy(true);
        let finalStatus = ["只读模式", "idle"];
        try {
          const response = await fetch(`/api/assistant/query?q=${encodeURIComponent(question)}`, {
            method: "GET",
            credentials: "same-origin",
            cache: "no-store",
            headers: { "Accept": "application/json", "X-Requested-With": "fetch" },
          });
          const data = await response.json().catch(() => ({}));
          const answer = data.intent === "rejected_write"
            ? "只读模式，不能修改系统数据。"
            : data.answer || (response.ok ? "查询完成，但没有返回摘要。" : "小助手暂时不可用，请稍后再试。");
          if (!response.ok || data.ok === false) finalStatus = ["只读模式，不能修改系统数据", "error"];
          addMessage(data.ok === false ? "error" : "assistant", answer);
        } catch (_) {
          finalStatus = ["小助手暂时不可用，页面其他功能不受影响。", "error"];
          addMessage("error", "小助手暂时不可用，页面其他功能不受影响。");
        } finally {
          setBusy(false);
          setStatusText(finalStatus[0], finalStatus[1]);
          input.focus({ preventScroll: true });
        }
      };

      const saved = readState();
      initQuickPrompts();
      if (Array.isArray(saved.messages) && saved.messages.length) {
        messages.innerHTML = "";
        saved.messages.slice(-maxStoredMessages).forEach((item) => addMessage(item.role || "assistant", item.text || ""));
      }
      setOpen(Boolean(saved.open));
      toggle.addEventListener("click", () => setOpen(panel.hidden));
      close?.addEventListener("click", () => setOpen(false));
      send.addEventListener("click", askAssistant);
      input.addEventListener("keydown", (event) => {
        if (event.key === "Enter" && !event.shiftKey) {
          event.preventDefault();
          askAssistant();
        }
      });
      window.addEventListener("pagehide", writeState);
    } catch (_) {}
  };
  const scheduleAssistantWidgetInit = () => {
    initAssistantWidgetIsolated();
    if (document.readyState === "loading") {
      document.addEventListener("DOMContentLoaded", initAssistantWidgetIsolated, { once: true });
    }
  };
  scheduleAssistantWidgetInit();

  const initContractStudentSelector = () => {
    qa("[data-contract-student-search]").forEach((root) => {
      if (root.dataset.contractStudentSelectorBound === "true") return;
      const input = q('input[name="q"]', root);
      const results = q("[data-contract-student-results]", root);
      if (!input || !results) return;
      root.dataset.contractStudentSelectorBound = "true";
      const options = qa("[data-contract-student-option]", results);
      let activeIndex = -1;

      const setExpanded = (expanded) => {
        const open = Boolean(expanded) && options.length > 0;
        input.setAttribute("aria-expanded", open ? "true" : "false");
        results.hidden = !open;
        if (!open) {
          activeIndex = -1;
          input.removeAttribute("aria-activedescendant");
          options.forEach((option) => option.setAttribute("aria-selected", "false"));
        }
      };

      const setActiveOption = (index) => {
        if (!options.length) return;
        activeIndex = Math.max(0, Math.min(index, options.length - 1));
        options.forEach((option, optionIndex) => {
          option.setAttribute("aria-selected", optionIndex === activeIndex ? "true" : "false");
        });
        const activeOption = options[activeIndex];
        if (activeOption.id) input.setAttribute("aria-activedescendant", activeOption.id);
        activeOption.scrollIntoView?.({ block: "nearest" });
      };

      setExpanded(input.getAttribute("aria-expanded") === "true");
      input.addEventListener("focus", () => setExpanded(true));
      input.addEventListener("keydown", (event) => {
        if (!options.length) return;
        if (event.key === "ArrowDown") {
          event.preventDefault();
          setExpanded(true);
          setActiveOption(activeIndex < 0 ? 0 : activeIndex + 1);
        } else if (event.key === "ArrowUp") {
          event.preventDefault();
          setExpanded(true);
          setActiveOption(activeIndex < 0 ? options.length - 1 : activeIndex - 1);
        } else if (event.key === "Enter" && activeIndex >= 0) {
          event.preventDefault();
          options[activeIndex].click();
        } else if (event.key === "Escape") {
          event.preventDefault();
          setExpanded(false);
        }
      });
    });

    qa("[data-contract-student-confirmation] form").forEach((form) => {
      if (form.dataset.contractStudentSubmitBound === "true") return;
      const submit = q("[data-contract-student-submit]", form);
      if (!submit) return;
      form.dataset.contractStudentSubmitBound = "true";
      form.addEventListener("submit", (event) => {
        if (form.dataset.contractStudentSubmitting === "true") {
          event.preventDefault();
          return;
        }
        form.dataset.contractStudentSubmitting = "true";
        form.setAttribute("aria-busy", "true");
        submit.disabled = true;
        submit.textContent = "正在进入合同流程…";
      });
    });
  };
  initContractStudentSelector();

  initPublicParentFormSubmit();
})();

/* Accurate Transcript CRM Review */
(function (root) {
  "use strict";

  const escapeHtml = (value) => String(value ?? "").replace(/[&<>"']/g, (char) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  })[char]);

  function splitCarrier(text, start, end) {
    const value = String(text || "");
    return {
      prefix: value.slice(0, Number(start)),
      suspect: value.slice(Number(start), Number(end)),
      suffix: value.slice(Number(end)),
    };
  }

  function resolutionFields({ csrf, queueHash, revision, action, selectedText, candidateReferences }) {
    return {
      csrf: String(csrf || ""),
      source_queue_hash: String(queueHash || ""),
      expected_revision: String(Number(revision || 0)),
      action: String(action || ""),
      selected_text: String(selectedText || ""),
      candidate_references: JSON.stringify(candidateReferences || []),
    };
  }

  function partitionReviewItems(items) {
    const pending = [];
    const completed = [];
    (items || []).forEach((item) => {
      if (["CONFIRMED", "EDITED"].includes(String(item.status || ""))) completed.push(item);
      else pending.push(item);
    });
    return { pending, completed };
  }

  function nextPendingReviewId(items, afterReviewId = "") {
    const values = items || [];
    const pending = (item) => !["CONFIRMED", "EDITED"].includes(String(item.status || ""));
    const afterIndex = values.findIndex((item) => String(item.review_id) === String(afterReviewId));
    const ordered = afterIndex < 0
      ? values
      : [...values.slice(afterIndex + 1), ...values.slice(0, afterIndex + 1)];
    return String((ordered.find(pending) || {}).review_id || "");
  }

  function machineReferenceMarkup(evidence) {
    const values = evidence || {};
    const unique = (group) => [...new Set(Object.values(group || {}).map((value) => String(value || "").trim()).filter(Boolean))];
    const ocr = unique(values.ocr);
    const asr = unique(values.asr);
    const rows = [];
    if (asr.length) rows.push(`<p><strong>语音识别候选：</strong>${asr.map(escapeHtml).join(" / ")}</p>`);
    if (ocr.length) rows.push(`<p><strong>字幕识别候选：</strong>${ocr.map(escapeHtml).join(" / ")}</p>`);
    if (new Set([...ocr, ...asr]).size > 1) rows.push("<p><strong>机器存在分歧</strong></p>");
    return rows.join("") || "<p>暂无额外机器参考。</p>";
  }

  function reviewCardMarkup(item, mediaUrl) {
    const parts = splitCarrier(item.carrier_text, item.start_offset, item.end_offset);
    const disabled = item.status !== "PENDING" ? " disabled" : "";
    const candidates = (item.alternatives || []).map((alternative) => (
      `<button type="button" class="primary" data-review-candidate="${escapeHtml(alternative.text)}"${disabled}>${escapeHtml(alternative.text)}</button>`
    )).join("");
    return `<article class="content-accurate-review-card" data-review-id="${escapeHtml(item.review_id)}" tabindex="-1">
      <div class="content-accurate-review-meta"><strong>需要确认</strong><span>${escapeHtml(item.start_ms / 1000)}s – ${escapeHtml(item.end_ms / 1000)}s</span></div>
      <video controls preload="metadata" playsinline src="${escapeHtml(mediaUrl)}"></video>
      <div class="content-accurate-review-context">
        <p>${escapeHtml(item.context_before || "")}</p>
        <p>${escapeHtml(parts.prefix)}<mark>${escapeHtml(parts.suspect)}</mark>${escapeHtml(parts.suffix)}</p>
        <p>${escapeHtml(item.context_after || "")}</p>
      </div>
      <h2>当前文字</h2>
      <p class="content-accurate-current-text">${escapeHtml(parts.suspect)}</p>
      <button type="button" class="secondary" data-review-keep${disabled}>保留原文</button>
      <h2>选择候选</h2>
      <div class="content-accurate-review-candidates">${candidates}</div>
      <button type="button" class="secondary" data-review-custom-toggle${disabled}>手工修改</button>
      <div class="content-accurate-review-custom" data-review-custom hidden>
        <label>实际说法<input data-review-custom-text value=""></label>
        <button type="button" class="primary" data-review-custom-save>确认并下一条</button>
      </div>
      <button type="button" class="secondary" data-review-ambiguous${disabled}>暂时听不清</button>
      <section class="content-accurate-machine-reference"><h2>机器参考</h2>${machineReferenceMarkup(item.evidence)}</section>
      <details><summary>技术详情</summary><pre>${escapeHtml(JSON.stringify(item.evidence || {}, null, 2))}</pre></details>
      <p class="content-accurate-review-saved">${item.status === "PENDING" ? "" : `已保存：${escapeHtml(item.status)}`}</p>
    </article>`;
  }

  function highlightedTranscript(text, uncertainties, show = true) {
    const value = String(text || "");
    if (!show) return escapeHtml(value);
    let cursor = 0;
    const output = [];
    [...(uncertainties || [])]
      .sort((left, right) => Number(left.start_offset) - Number(right.start_offset))
      .forEach((item) => {
        const start = Number(item.start_offset);
        const end = Number(item.end_offset);
        if (start < cursor || end <= start || value.slice(start, end) !== String(item.current_text || "")) return;
        output.push(escapeHtml(value.slice(cursor, start)));
        output.push(`<mark data-uncertainty-id="${escapeHtml(item.review_id || "")}">${escapeHtml(value.slice(start, end))}</mark>`);
        cursor = end;
      });
    output.push(escapeHtml(value.slice(cursor)));
    return output.join("");
  }

  function applyTranscriptCandidate(text, uncertainties, reviewId, selectedText) {
    const value = String(text || "");
    const items = [...(uncertainties || [])];
    const target = items.find((item) => String(item.review_id) === String(reviewId));
    if (!target) return { text: value, uncertainties: items };
    const start = Number(target.start_offset);
    const end = Number(target.end_offset);
    const before = value.slice(start, end);
    if (before !== String(target.current_text || "")) return { text: value, uncertainties: items };
    const replacement = String(selectedText || "");
    const delta = replacement.length - (end - start);
    return {
      text: value.slice(0, start) + replacement + value.slice(end),
      uncertainties: items.filter((item) => item !== target).map((item) => {
        if (Number(item.start_offset) < end) return item;
        return { ...item, start_offset: Number(item.start_offset) + delta, end_offset: Number(item.end_offset) + delta };
      }),
    };
  }

  function transcriptSaveFields(csrf, text, uncertainties) {
    return {
      csrf: String(csrf || ""), final_transcript: String(text || ""),
      uncertainty_metadata: JSON.stringify(uncertainties || []),
    };
  }

  async function copyCurrentTranscript(text, clipboardApi, fallbackCopy) {
    const value = String(text || "");
    if (!value.trim()) return false;
    try {
      if (clipboardApi && typeof clipboardApi.writeText === "function") {
        await clipboardApi.writeText(value);
        return true;
      }
    } catch (_error) {
      // Fall through to the deterministic selection-based copy path.
    }
    return Boolean(await fallbackCopy(value));
  }

  async function readSafeJsonResponse(response, { route = "", onDiagnostic = null } = {}) {
    const contentType = String(response?.headers?.get?.("content-type") || "");
    const diagnostic = {
      status: Number(response?.status || 0),
      route: String(route || ""),
      redirected: Boolean(response?.redirected),
      finalUrl: String(response?.url || "").split("?", 1)[0],
      contentType,
    };
    const report = typeof onDiagnostic === "function"
      ? onDiagnostic
      : (value) => root.console?.warn?.("CRM JSON response diagnostic", value);
    if (!contentType.toLowerCase().includes("application/json")) {
      report(diagnostic);
      throw new Error("操作失败，请刷新页面后重试。");
    }
    let payload;
    try {
      payload = await response.json();
    } catch (_error) {
      report({ ...diagnostic, reason: "invalid_json" });
      throw new Error("操作失败，请刷新页面后重试。");
    }
    if (!response.ok || payload?.ok === false) {
      report(diagnostic);
      throw new Error(payload?.error || payload?.message || "操作失败，请刷新页面后重试。");
    }
    return payload;
  }

  root.AccurateTranscriptCRMUI = {
    splitCarrier, resolutionFields, reviewCardMarkup,
    partitionReviewItems, nextPendingReviewId, machineReferenceMarkup,
    highlightedTranscript, applyTranscriptCandidate, transcriptSaveFields,
    copyCurrentTranscript, readSafeJsonResponse, initAccurateTranscriptEditors,
  };
  if (typeof document === "undefined") return;

  document.querySelectorAll("[data-accurate-review]").forEach((page) => {
    const itemsRoot = page.querySelector("[data-review-items]");
    const progress = page.querySelector("[data-review-progress]");
    const status = page.querySelector("[data-review-status]");
    const completedSection = page.querySelector("[data-review-completed]");
    const completedRoot = page.querySelector("[data-review-completed-items]");
    const completedSummary = page.querySelector("[data-review-completed-summary]");
    let queue = null;

    async function load(afterReviewId = "") {
      const response = await fetch(page.dataset.reviewUrl, {
        credentials: "same-origin", cache: "no-store",
        headers: { "Accept": "application/json", "X-Requested-With": "fetch" },
      });
      const payload = await response.json();
      if (!response.ok) throw new Error(payload.user_message || payload.error || "无法读取审核进度");
      queue = payload;
      render(afterReviewId);
    }

    async function submit(item, action, selectedText, references) {
      status.textContent = "正在安全保存…";
      const fields = resolutionFields({
        csrf: page.dataset.csrf, queueHash: queue.queue_hash,
        revision: queue.revision, action, selectedText,
        candidateReferences: references,
      });
      const response = await fetch(`${page.dataset.submitBase}/${encodeURIComponent(item.review_id)}`, {
        method: "POST", credentials: "same-origin",
        headers: { "Content-Type": "application/x-www-form-urlencoded;charset=UTF-8", "X-Requested-With": "fetch" },
        body: new URLSearchParams(fields).toString(),
      });
      const payload = await response.json();
      if (!response.ok) throw new Error(payload.user_message || payload.error || "保存失败");
      status.textContent = payload.status === "HUMAN_REVIEW_COMPLETE" ? "确认完成" : "已保存";
      await load(item.review_id);
    }

    function render(afterReviewId = "") {
      const groups = partitionReviewItems(queue.items || []);
      progress.textContent = `待确认 ${groups.pending.length} 处 · 已完成 ${groups.completed.length}`;
      itemsRoot.replaceChildren();
      completedRoot?.replaceChildren();
      if (completedSection) completedSection.hidden = groups.completed.length === 0;
      if (completedSummary) completedSummary.textContent = `已完成 ${groups.completed.length} 处`;

      const renderItem = (item, target) => {
        const wrapper = document.createElement("div");
        const media = `${page.dataset.mediaBase}/${encodeURIComponent(item.clip_id || "")}`;
        wrapper.innerHTML = reviewCardMarkup(item, media);
        const card = wrapper.firstElementChild;
        card.querySelector("[data-review-keep]")?.addEventListener("click", () => submit(
          item, "CONFIRMED", item.suspect_span,
          [`carrier:${item.suspect_span}`],
        ).catch((error) => { status.textContent = error.message; }));
        card.querySelectorAll("[data-review-candidate]").forEach((button) => {
          button.addEventListener("click", () => submit(
            item, "CONFIRMED", button.dataset.reviewCandidate,
            [`alternative:${button.dataset.reviewCandidate}`],
          ).catch((error) => { status.textContent = error.message; }));
        });
        const custom = card.querySelector("[data-review-custom]");
        card.querySelector("[data-review-custom-toggle]")?.addEventListener("click", () => {
          custom.hidden = false;
          custom.querySelector("input")?.focus();
        });
        card.querySelector("[data-review-custom-save]")?.addEventListener("click", () => {
          const value = card.querySelector("[data-review-custom-text]").value.trim();
          if (!value) { status.textContent = "请填写实际说法"; return; }
          submit(item, "EDITED", value, []).catch((error) => { status.textContent = error.message; });
        });
        card.querySelector("[data-review-ambiguous]")?.addEventListener("click", () => {
          submit(item, "AMBIGUOUS", "", []).catch((error) => { status.textContent = error.message; });
        });
        target.append(card);
      };
      groups.pending.forEach((item) => renderItem(item, itemsRoot));
      groups.completed.forEach((item) => renderItem(item, completedRoot));
      const focusId = nextPendingReviewId(queue.items || [], afterReviewId);
      [...itemsRoot.querySelectorAll("[data-review-id]")]
        .find((card) => card.dataset.reviewId === focusId)
        ?.focus({ preventScroll: false });
    }

    load().catch((error) => { status.textContent = error.message; });
  });

  function initAccurateTranscriptEditors(scope = document) {
    const editors = [];
    if (scope?.matches?.("[data-accurate-transcript-editor]")) editors.push(scope);
    scope?.querySelectorAll?.("[data-accurate-transcript-editor]").forEach((editor) => editors.push(editor));
    editors.forEach((editor) => {
    if (editor.dataset.accurateTranscriptInitialized === "true") return;
    editor.dataset.accurateTranscriptInitialized = "true";
    const input = editor.querySelector("[data-transcript-input]");
    const preview = editor.querySelector("[data-transcript-highlights]");
    const list = editor.querySelector("[data-uncertainty-list]");
    const status = editor.querySelector("[data-transcript-status]");
    const source = editor.querySelector("[data-transcript-uncertainties]");
    let uncertainties = [];
    let show = false;
    try { uncertainties = JSON.parse(source?.textContent || "[]"); } catch (_error) { uncertainties = []; }

    function render() {
      preview.innerHTML = highlightedTranscript(input.value, uncertainties, show);
      preview.hidden = !show;
      list.replaceChildren();
      uncertainties.forEach((item) => {
        const card = document.createElement("article");
        card.className = "content-accurate-uncertainty-card";
        const candidates = (item.candidate_texts || []).filter((value) => value !== item.current_text);
        card.innerHTML = `<p><strong>当前文字：</strong>${escapeHtml(item.current_text || "")}</p>
          <div class="content-accurate-uncertainty-candidates">${candidates.map((candidate) => (
            `<button type="button" class="secondary compact" data-uncertainty-candidate="${escapeHtml(candidate)}">${escapeHtml(candidate)}</button>`
          )).join("")}</div>`;
        card.querySelectorAll("[data-uncertainty-candidate]").forEach((button) => {
          button.addEventListener("click", () => {
            const result = applyTranscriptCandidate(input.value, uncertainties, item.review_id, button.dataset.uncertaintyCandidate);
            input.value = result.text;
            uncertainties = result.uncertainties;
            status.textContent = "已应用局部修改，请保存文案。";
            render();
          });
        });
        list.append(card);
      });
      list.hidden = !show || uncertainties.length === 0;
      const toggle = editor.querySelector("[data-uncertainty-toggle]");
      if (toggle) toggle.textContent = show ? "隐藏疑点" : "显示疑点";
    }

    input.addEventListener("input", () => {
      if (uncertainties.length) {
        uncertainties = [];
        status.textContent = "已按你的手工编辑更新全文；原疑点标记已视为人工处理。";
      }
      render();
    });
    editor.querySelector("[data-uncertainty-toggle]")?.addEventListener("click", () => {
      show = !show;
      render();
    });
    function fallbackCopy(value) {
      const helper = document.createElement("textarea");
      helper.value = value;
      helper.setAttribute("readonly", "");
      helper.style.position = "fixed";
      helper.style.opacity = "0";
      document.body.append(helper);
      helper.select();
      let copied = false;
      try { copied = document.execCommand("copy"); } catch (_error) { copied = false; }
      helper.remove();
      input.focus({ preventScroll: true });
      return copied;
    }
    editor.querySelector("[data-transcript-copy]")?.addEventListener("click", async () => {
      try {
        const copied = await copyCurrentTranscript(input.value, navigator.clipboard, fallbackCopy);
        const message = copied
          ? "已复制当前文案"
          : (input.value.trim() ? "复制失败，请手动复制" : "当前没有可复制的文案");
        status.textContent = message;
        root.setTimeout?.(() => {
          if (status.textContent === message) status.textContent = "";
        }, 2400);
      } catch (_error) { status.textContent = "复制失败，请手动复制"; }
    });
    editor.querySelector("[data-transcript-save]")?.addEventListener("click", async () => {
      status.textContent = "正在保存…";
      try {
        const response = await fetch(editor.dataset.saveUrl, {
          method: "POST", credentials: "same-origin",
          headers: {
            "Accept": "application/json",
            "Content-Type": "application/x-www-form-urlencoded;charset=UTF-8",
            "X-Requested-With": "fetch",
          },
          body: new URLSearchParams(transcriptSaveFields(editor.dataset.csrf, input.value, uncertainties)).toString(),
        });
        const payload = await readSafeJsonResponse(response, { route: editor.dataset.saveUrl });
        uncertainties = payload.uncertainty_metadata || [];
        status.textContent = "文案已保存。";
        render();
      } catch (error) { status.textContent = error.message; }
    });
    render();
    });
  }
  initAccurateTranscriptEditors(document);

  function createContractTemplateSaveCoordinator({
    delayMs = 700,
    setTimer = (callback, delay) => window.setTimeout(callback, delay),
    clearTimer = (timer) => window.clearTimeout(timer),
    onStateChange = () => {},
  } = {}) {
    const states = new Map();
    let saveChain = Promise.resolve();

    function stateFor(key, allowDisposed = false) {
      const normalized = String(key || "");
      if (!normalized) throw new Error("保存键不能为空");
      if (!states.has(normalized)) {
        states.set(normalized, {
          dirty: false,
          disposed: false,
          disposing: false,
          error: null,
          generation: 0,
          save: null,
          timer: null,
        });
      }
      const state = states.get(normalized);
      if (state.disposed && !allowDisposed) throw new Error("保存键已停用");
      return [normalized, state];
    }

    function dirtyKeys() {
      return [...states.entries()]
        .filter(([, state]) => state.dirty && !state.disposed)
        .map(([key]) => key);
    }

    function errorEntries() {
      return [...states.entries()]
        .filter(([, state]) => state.dirty && state.error && !state.disposed)
        .map(([key, state]) => ({ key, error: state.error }));
    }

    function snapshot({ type = "snapshot", key = null, error = null, cleared = false, message = "" } = {}) {
      const currentDirtyKeys = dirtyKeys();
      const currentErrors = errorEntries();
      return {
        type,
        key,
        error,
        cleared,
        message,
        dirtyKeys: currentDirtyKeys,
        failedKeys: currentErrors.map((entry) => entry.key),
        errors: currentErrors,
        currentError: currentErrors[0] || null,
        hasDirty: currentDirtyKeys.length > 0,
        hasError: currentErrors.length > 0,
      };
    }

    function notify(type, key, error = null, cleared = false, message = "") {
      const current = snapshot({ type, key, error, cleared, message });
      onStateChange(current);
      return current;
    }

    function report(type, { key = null, error = null, message = "" } = {}) {
      return notify(type, key, error, false, message);
    }

    function enqueue(key, generation) {
      const execute = async () => {
        const state = states.get(key);
        if (!state || state.generation !== generation || typeof state.save !== "function") return;
        const save = state.save;
        notify("saving", key);
        try {
          await save();
          const current = states.get(key);
          if (!current || current.generation !== generation || current.disposed) return;
          current.dirty = false;
          current.error = null;
          notify("saved", key, null, true);
        } catch (error) {
          const current = states.get(key);
          if (current && current.generation === generation && !current.disposed) {
            current.dirty = true;
            current.error = error;
            notify("error", key, error);
          }
        }
      };
      saveChain = saveChain.then(execute, execute);
      return saveChain;
    }

    function serialize(mutation) {
      if (typeof mutation !== "function") throw new Error("串行任务必须是函数");
      const execute = async () => mutation();
      const operation = saveChain.then(execute, execute);
      saveChain = operation.catch(() => {});
      return operation;
    }

    function cancel(key, { dispose = false } = {}) {
      const [normalized, state] = stateFor(key, true);
      if (state.timer !== null) clearTimer(state.timer);
      state.timer = null;
      state.generation += 1;
      state.dirty = false;
      state.error = null;
      state.disposed = Boolean(dispose || state.disposed);
      notify(state.disposed ? "disposing" : "cancelled", normalized);
      return state;
    }

    function dispose(key, mutation = null) {
      const [normalized, state] = stateFor(key, true);
      if (state.disposed) throw new Error("保存键已停用");
      const previous = {
        dirty: state.dirty,
        error: state.error,
        save: state.save,
      };
      cancel(normalized, { dispose: true });
      state.disposing = typeof mutation === "function";
      if (typeof mutation !== "function") {
        notify("disposed", normalized);
        return saveChain;
      }
      return serialize(async () => {
        try {
          const result = await mutation();
          state.disposing = false;
          notify("disposed", normalized);
          return result;
        } catch (error) {
          state.disposed = false;
          state.disposing = false;
          state.dirty = previous.dirty;
          state.error = previous.error;
          state.save = previous.save;
          notify("error", normalized, error);
          throw error;
        }
      });
    }

    function schedule(key, save) {
      if (typeof save !== "function") throw new Error("保存任务必须是函数");
      const [normalized, state] = stateFor(key);
      state.generation += 1;
      state.save = save;
      state.dirty = true;
      state.error = null;
      if (state.timer !== null) clearTimer(state.timer);
      const generation = state.generation;
      let timer = null;
      timer = setTimer(() => {
        const current = states.get(normalized);
        if (current && current.timer === timer) current.timer = null;
        enqueue(normalized, generation);
      }, delayMs);
      state.timer = timer;
      notify("dirty", normalized);
      return timer;
    }

    function retry(key) {
      const [normalized, state] = stateFor(key);
      if (!state.dirty || typeof state.save !== "function") return saveChain;
      if (state.timer !== null) {
        clearTimer(state.timer);
        state.timer = null;
      }
      state.generation += 1;
      return enqueue(normalized, state.generation);
    }

    async function flushAll() {
      while (true) {
        states.forEach((state, key) => {
          if (!state.dirty || state.disposed || state.timer === null) return;
          clearTimer(state.timer);
          state.timer = null;
          enqueue(key, state.generation);
        });
        const observedChain = saveChain;
        await observedChain;
        const hasPendingTimer = [...states.values()].some(
          (state) => state.dirty && !state.disposed && state.timer !== null,
        );
        if (!hasPendingTimer && observedChain === saveChain) return snapshot({ type: "flushed" });
      }
    }

    async function prepareForAction() {
      notify("preparing-action", null, null, false, "正在保存全部修改…");
      await flushAll();
      const current = snapshot({ type: "action-ready" });
      if (current.hasDirty || current.failedKeys.length) {
        const error = current.currentError?.error || new Error("仍有未保存修改，正式操作已阻止");
        notify("action-blocked", current.currentError?.key || null, error);
        throw error;
      }
      return notify("action-ready", null, null, false, "全部修改已保存");
    }

    return {
      schedule,
      retry,
      cancel,
      dispose,
      serialize,
      report,
      snapshot,
      hasDirty: () => dirtyKeys().length > 0,
      isDirty: (key) => {
        const state = states.get(String(key || ""));
        return Boolean(state?.dirty && !state.disposed);
      },
      isDisposed: (key) => Boolean(states.get(String(key || ""))?.disposed),
      errorFor: (key) => states.get(String(key || ""))?.error || null,
      failedKeys: () => errorEntries().map((entry) => entry.key),
      dirtyKeys,
      flushAll,
      prepareForAction,
      whenIdle: () => saveChain,
    };
  }

  function renderContractTemplateSaveStatus(event = {}) {
    const needsEditRetry = Boolean(event.hasDirty || event.failedKeys?.length);
    const actionableError = (message) => {
      if (!needsEditRetry) return message;
      const separator = /[。！？；]$/.test(String(message || "")) ? "" : "；";
      return `${message}${separator}仍有未保存修改，可继续编辑重试`;
    };
    const saveErrorMessage = (error) => {
      const message = String(error?.message || error || "自动保存失败").trim();
      return message === "Failed to fetch" ? "保存失败：无法连接服务器" : message;
    };
    const aggregateError = event.currentError?.error;
    if (aggregateError) return actionableError(saveErrorMessage(aggregateError));
    if (event.type === "error") {
      return actionableError(saveErrorMessage(event.error));
    }
    if (event.message) return event.message;
    if (event.type === "dirty") return event.key === "settings" ? "有未保存配置" : "有未保存修改";
    if (event.type === "saving") return "正在自动保存…";
    if (event.type === "saved") {
      if (event.hasDirty) return "仍有未保存修改";
      return event.key === "settings" ? "配置已自动保存" : "已自动保存";
    }
    if (event.type === "disposing") return "正在删除区块…";
    if (event.type === "disposed") return "区块已删除";
    if (event.type === "cancelled") return event.hasDirty ? "仍有未保存修改" : "修改已取消";
    return event.hasDirty ? "仍有未保存修改" : "";
  }

  function renderContractTemplateActionStatus(
    name, { error = null, payload = null, draftSaved = true } = {},
  ) {
    if (error) {
      const message = String(error?.message || error || "合同模板操作失败").trim();
      if (name === "apply") {
        return draftSaved
          ? `草稿已保存，但暂不能应用：${message}`
          : `应用已阻止：${message}`;
      }
      return message;
    }
    if (name === "apply") return "模板已应用，新建合同将使用此版本。";
    if (name === "simulate") return `模拟通过：${payload?.artifact_dir || ""}`;
    if (payload?.passed === false) {
      return (payload.issues || []).map((item) => item.message).join("；");
    }
    return "操作完成";
  }

  function setContractTemplateCardDeletePending(card, pending) {
    const stateKey = "templateDeleteWasDisabled";
    card.querySelectorAll("input,textarea,select,button").forEach((control) => {
      if (pending) {
        if (!(stateKey in control.dataset)) control.dataset[stateKey] = control.disabled ? "true" : "false";
        control.disabled = true;
      } else if (stateKey in control.dataset) {
        control.disabled = control.dataset[stateKey] === "true";
        delete control.dataset[stateKey];
      }
    });
    if (pending) {
      if (!("templateDeleteWasDraggable" in card.dataset)) {
        card.dataset.templateDeleteWasDraggable = card.draggable ? "true" : "false";
      }
      card.draggable = false;
    } else if ("templateDeleteWasDraggable" in card.dataset) {
      card.draggable = card.dataset.templateDeleteWasDraggable === "true";
      delete card.dataset.templateDeleteWasDraggable;
    }
  }

  const contractTemplateDeleteOperations = new WeakMap();

  function deleteContractTemplateBlock({ card, saveCoordinator, ensureLock, action }) {
    const existing = contractTemplateDeleteOperations.get(card);
    if (existing) return existing;

    let resolveOperation;
    let rejectOperation;
    const operation = new Promise((resolve, reject) => {
      resolveOperation = resolve;
      rejectOperation = reject;
    });
    contractTemplateDeleteOperations.set(card, operation);
    try {
      setContractTemplateCardDeletePending(card, true);
    } catch (error) {
      contractTemplateDeleteOperations.delete(card);
      rejectOperation(error);
      return operation;
    }

    const owner = (async () => {
      try {
        await saveCoordinator.dispose(`block:${card.dataset.blockKey}`, async () => {
          await ensureLock();
          await action("delete-block", { block_key: card.dataset.blockKey });
        });
        card.remove();
        return true;
      } catch (_error) {
        setContractTemplateCardDeletePending(card, false);
        return false;
      }
    })();
    owner.then((result) => {
      if (contractTemplateDeleteOperations.get(card) === operation) {
        contractTemplateDeleteOperations.delete(card);
      }
      resolveOperation(result);
    }, (error) => {
      if (contractTemplateDeleteOperations.get(card) === operation) {
        contractTemplateDeleteOperations.delete(card);
      }
      rejectOperation(error);
    });
    return operation;
  }

  const contractTemplateFormalActionFreezeStates = new WeakMap();

  function setContractTemplateEditorFormalActionPending(editor, pending) {
    if (pending) {
      if (contractTemplateFormalActionFreezeStates.has(editor)) return;
      const controls = [...editor.querySelectorAll("input,textarea,select,button,[contenteditable]")]
        .map((control) => ({
          control,
          hasDisabled: "disabled" in control,
          disabled: Boolean(control.disabled),
          hasContentEditable: Boolean(control.hasAttribute?.("contenteditable")),
          contentEditable: control.getAttribute?.("contenteditable"),
        }));
      const cards = [...editor.querySelectorAll(".contract-template-block")]
        .map((card) => ({ card, draggable: Boolean(card.draggable) }));
      const state = {
        controls,
        cards,
        hadBusy: Boolean(editor.hasAttribute?.("aria-busy")),
        busy: editor.getAttribute?.("aria-busy"),
      };
      contractTemplateFormalActionFreezeStates.set(editor, state);
      controls.forEach(({ control, hasDisabled, hasContentEditable }) => {
        if (hasDisabled) control.disabled = true;
        if (hasContentEditable) control.setAttribute("contenteditable", "false");
      });
      cards.forEach(({ card }) => {
        card.draggable = false;
        card.classList?.remove("is-dragging");
      });
      editor.setAttribute?.("aria-busy", "true");
      return;
    }

    const state = contractTemplateFormalActionFreezeStates.get(editor);
    if (!state) return;
    state.controls.forEach(({
      control, hasDisabled, disabled, hasContentEditable, contentEditable,
    }) => {
      if (hasDisabled) control.disabled = disabled;
      if (hasContentEditable) control.setAttribute("contenteditable", contentEditable ?? "");
    });
    state.cards.forEach(({ card, draggable }) => { card.draggable = draggable; });
    if (state.hadBusy) editor.setAttribute?.("aria-busy", state.busy ?? "");
    else editor.removeAttribute?.("aria-busy");
    contractTemplateFormalActionFreezeStates.delete(editor);
  }

  function createContractTemplateFormalActionController({ setPending, execute }) {
    if (typeof setPending !== "function" || typeof execute !== "function") {
      throw new Error("正式操作控制器配置无效");
    }
    let inFlight = null;
    let inFlightName = "";
    let retainedAfterSuccess = false;
    let mutationOwned = false;
    let mutationInFlight = null;
    let mutationKey = "";

    function formalActionActive() {
      return retainedAfterSuccess || Boolean(inFlight);
    }

    function isActive() {
      return formalActionActive() || mutationOwned;
    }

    function allowsLocalMutation() {
      return !isActive();
    }

    function runMutation(mutation, { key = "", keepFrozenOnSuccess = false } = {}) {
      if (formalActionActive()) return false;
      if (typeof mutation !== "function") throw new Error("编辑操作必须是函数");
      const normalizedKey = String(key || "");
      if (mutationOwned) {
        if (
          mutationInFlight
          && normalizedKey
          && normalizedKey === mutationKey
        ) return mutationInFlight;
        return false;
      }
      const ownsFreeze = Boolean(normalizedKey);
      if (ownsFreeze) {
        setPending(true);
        mutationOwned = true;
        mutationKey = normalizedKey;
      }
      let result;
      try {
        result = mutation();
      } catch (error) {
        if (ownsFreeze) {
          setPending(false);
          mutationOwned = false;
          mutationKey = "";
        }
        throw error;
      }
      if (!result || typeof result.then !== "function") {
        if (ownsFreeze) {
          if (keepFrozenOnSuccess) retainedAfterSuccess = true;
          else setPending(false);
          mutationOwned = false;
          mutationKey = "";
        }
        return result;
      }
      const operation = Promise.resolve(result).then((value) => {
        if (keepFrozenOnSuccess) retainedAfterSuccess = true;
        else setPending(false);
        return value;
      }, (error) => {
        setPending(false);
        throw error;
      });
      mutationInFlight = operation;
      const clear = () => {
        if (mutationInFlight === operation) {
          mutationOwned = false;
          mutationInFlight = null;
          mutationKey = "";
        }
      };
      operation.then(clear, clear);
      return operation;
    }

    function run(options = {}) {
      const name = String(options.name || "");
      if (!name) return Promise.reject(new Error("正式操作名称不能为空"));
      if (retainedAfterSuccess) {
        return Promise.reject(new Error("正式操作正在执行，请等待页面刷新"));
      }
      if (inFlight) {
        if (name === inFlightName) return inFlight;
        return Promise.reject(new Error("另一正式操作正在执行"));
      }
      if (mutationOwned) {
        return Promise.reject(new Error("编辑操作正在执行，正式操作已阻止"));
      }

      setPending(true);
      inFlightName = name;
      const operation = Promise.resolve()
        .then(() => execute(options))
        .then((result) => {
          if (options.keepFrozenOnSuccess) retainedAfterSuccess = true;
          else setPending(false);
          return result;
        }, (error) => {
          setPending(false);
          throw error;
        });
      inFlight = operation;
      const clear = () => {
        if (inFlight === operation) {
          inFlight = null;
          inFlightName = "";
        }
      };
      operation.then(clear, clear);
      return operation;
    }

    return { run, runMutation, allowsLocalMutation, isActive };
  }

  async function runContractTemplateFormalAction({
    name, saveCoordinator, ensureLock, action, reportAction = () => {},
    buildFields = () => ({}),
  }) {
    await ensureLock();
    const prepared = await saveCoordinator.prepareForAction();
    if (prepared.hasDirty || prepared.failedKeys.length) {
      throw new Error("仍有未保存修改，正式操作已阻止");
    }
    reportAction("正在处理…");
    return saveCoordinator.serialize(async () => action(name, await buildFields()));
  }

  function isContractTemplateEditLockConflict(error) {
    return Number(error?.status || 0) === 409
      && String(error?.code || "") === "edit_lock_conflict";
  }

  async function runContractTemplateActionWithLockRecovery({
    name, fields = {}, action, renewLock,
  }) {
    try {
      return await action(name, fields);
    } catch (error) {
      if (name === "lock" || !isContractTemplateEditLockConflict(error)) throw error;
      await renewLock();
      return action(name, fields);
    }
  }

  function initContractTemplateEditorV1(scope = document) {
    const editor = scope.querySelector?.('[data-contract-template-editor="v1"][data-version-id]');
    if (!editor || editor.dataset.initialized === "true") return;
    editor.dataset.initialized = "true";
    const templateType = editor.dataset.templateType;
    const editable = editor.dataset.editable === "true";
    const csrf = editor.querySelector("[data-template-csrf]")?.value || "";
    const status = editor.querySelector("[data-template-save-message]");
    const actionStatus = editor.querySelector("[data-template-action-message]");
    let revision = Number(editor.dataset.revision || 0);
    let lockToken = editor.dataset.lockToken || "";
    let lockRequest = null;
    let dragged = null;

    async function requestAction(name, fields = {}) {
      const response = await fetch(`/templates/v1/${encodeURIComponent(templateType)}/actions/${name}`, {
        method: "POST",
        credentials: "same-origin",
        headers: {
          "Accept": "application/json",
          "Content-Type": "application/x-www-form-urlencoded;charset=UTF-8",
          "X-Requested-With": "fetch",
        },
        body: new URLSearchParams({ csrf, version_id: editor.dataset.versionId, revision: String(revision), lock_token: lockToken, ...fields }).toString(),
      });
      const payload = await readSafeJsonResponse(response, { route: response.url });
      if (!response.ok || payload.ok === false) {
        const error = new Error(payload.error || "合同模板操作失败");
        error.status = Number(response.status || 0);
        error.code = String(payload.error_code || "");
        throw error;
      }
      if (payload.revision) revision = Number(payload.revision);
      if (payload.token) lockToken = payload.token;
      editor.dataset.revision = String(revision);
      editor.dataset.lockToken = lockToken;
      return payload;
    }

    async function ensureLock(forceRenewal = false) {
      if (lockToken && !forceRenewal) return lockToken;
      if (lockRequest) return lockRequest;
      if (!lockToken) {
        saveCoordinator.report("operation", { message: "正在取得编辑权…" });
      }
      lockRequest = requestAction("lock").then((payload) => {
        if (!lockToken) lockToken = String(payload.token || "");
        return String(payload.token || lockToken);
      });
      try {
        const token = await lockRequest;
        if (!forceRenewal) {
          saveCoordinator.report("operation", { message: "可以开始编辑" });
        }
        return token;
      } finally {
        lockRequest = null;
      }
    }

    async function action(name, fields = {}) {
      return runContractTemplateActionWithLockRecovery({
        name,
        fields,
        action: requestAction,
        renewLock: () => ensureLock(true),
      });
    }

    const saveCoordinator = createContractTemplateSaveCoordinator({
      onStateChange: (event) => {
        status.textContent = renderContractTemplateSaveStatus(event);
      },
    });
    const reportAction = (message) => {
      if (actionStatus) actionStatus.textContent = String(message || "");
    };
    const formalActionController = createContractTemplateFormalActionController({
      setPending: (pending) => {
        if (pending) dragged = null;
        setContractTemplateEditorFormalActionPending(editor, pending);
      },
      execute: (options) => runContractTemplateFormalAction({
        ...options, saveCoordinator, ensureLock, action, reportAction,
      }),
    });

    async function saveBlock(card) {
      await ensureLock();
      const payload = await action("autosave-block", {
        block_key: card.dataset.blockKey,
        title: card.querySelector('[name="title"]')?.value || "",
        body: card.querySelector('[name="body"]')?.value || "",
        enabled: card.querySelector('[name="enabled"]')?.checked ? "1" : "0",
      });
      revision = Number(payload.revision || revision);
    }

    function scheduleBlockSave(card) {
      saveCoordinator.schedule(`block:${card.dataset.blockKey}`, () => saveBlock(card));
    }

    editor.querySelectorAll(".contract-template-block").forEach((card) => {
      card.querySelectorAll("input,textarea").forEach((input) => input.addEventListener("input", () => {
        if (!formalActionController.allowsLocalMutation()) return;
        scheduleBlockSave(card);
      }));
      card.addEventListener("dragstart", () => {
        if (!formalActionController.allowsLocalMutation()) return;
        dragged = card;
        card.classList.add("is-dragging");
      });
      card.addEventListener("dragend", () => { card.classList.remove("is-dragging"); dragged = null; });
      card.addEventListener("dragover", (event) => {
        if (!formalActionController.allowsLocalMutation()) return;
        event.preventDefault();
        if (dragged && dragged !== card) card.parentElement.insertBefore(dragged, card);
      });
      card.querySelector("[data-template-delete-block]")?.addEventListener("click", async () => {
        await formalActionController.runMutation(async () => {
          if (!window.confirm("确认删除这个条款区块？")) return;
          await deleteContractTemplateBlock({ card, saveCoordinator, ensureLock, action });
        }, { key: `delete:${card.dataset.blockKey}` });
      });
    });

    editor.querySelector(".contract-template-block-list")?.addEventListener("drop", async (event) => {
      await formalActionController.runMutation(async () => {
        event.preventDefault();
        try {
          await ensureLock();
          const ordered = [...editor.querySelectorAll(".contract-template-block")].map((card) => card.dataset.blockKey);
          await action("reorder", { ordered_keys_json: JSON.stringify(ordered) });
          saveCoordinator.report("operation", { message: "条款顺序已保存" });
        } catch (error) { saveCoordinator.report("error", { error }); }
      }, { key: "reorder" });
    });

    editor.querySelector("[data-template-add-block]")?.addEventListener("click", async () => {
      if (!formalActionController.allowsLocalMutation()) return;
      const title = window.prompt("新区块标题");
      if (!title) return;
      const body = window.prompt("新区块正文");
      if (!body) return;
      try {
        await formalActionController.runMutation(async () => {
          await ensureLock();
          const parent = editor.querySelector("[data-template-add-block-slot]")?.value || "";
          const key = `${parent ? `${parent}::` : ""}custom_${Date.now()}`;
          await action("create-block", { block_key: key, title, body });
          window.location.reload();
        }, { key: "create-block", keepFrozenOnSuccess: true });
      } catch (error) { saveCoordinator.report("error", { error }); }
    });

    editor.querySelectorAll("[data-template-commercial],[data-template-service]").forEach((input) => {
      input.addEventListener("input", () => {
        if (!formalActionController.allowsLocalMutation()) return;
        saveCoordinator.schedule("settings", async () => {
          await ensureLock();
          const commercial = {};
          editor.querySelectorAll("[data-template-commercial]").forEach((field) => { commercial[field.dataset.templateCommercial] = field.value; });
          const services = [...editor.querySelectorAll("[data-template-service]")].map((field) => ({
            service_id: field.value, enabled: true, default_selected: field.checked,
          }));
          await action("autosave-settings", { commercial_json: JSON.stringify(commercial), services_json: JSON.stringify(services) });
        });
      });
    });

    editor.querySelectorAll("[data-template-action]").forEach((button) => button.addEventListener("click", async () => {
      const name = button.dataset.templateAction;
      try {
        const payload = await formalActionController.run({
          name,
          keepFrozenOnSuccess: name === "publish" || name === "apply",
          buildFields: () => {
            if (name !== "simulate") return {};
            const selected = [...editor.querySelectorAll("[data-template-service]:checked")].map((field) => field.value);
            const signer = editor.querySelector("[data-template-signer]")?.value || "mother";
            return {
              student_id: editor.querySelector("[data-template-student-id]")?.value || "",
              student_snapshot_json: JSON.stringify({
                student_name: "系统模拟学生", [`${signer}_passport_first_name`]: signer === "father" ? "Test Father" : "Test Mother",
                [`${signer}_passport_last_name`]: "Signer", [`${signer}_mobile`]: "0400000000",
                [`${signer}_email`]: "simulation@example.test",
              }),
              signer_source: signer,
              signer_overrides_json: JSON.stringify({
                phone: editor.querySelector("[data-template-signer-phone]")?.value || "",
                email: editor.querySelector("[data-template-signer-email]")?.value || "",
              }),
              selected_services_json: JSON.stringify(selected),
            };
          },
        });
        reportAction(renderContractTemplateActionStatus(name, { payload }));
        if (name === "apply") window.setTimeout(() => window.location.reload(), 1500);
        if (name === "publish") window.location.reload();
      } catch (error) {
        reportAction(renderContractTemplateActionStatus(name, {
          error,
          draftSaved: !saveCoordinator.hasDirty(),
        }));
      }
    }));

    editor.querySelectorAll("[data-template-republish-version]").forEach((button) => button.addEventListener("click", async () => {
      if (!formalActionController.allowsLocalMutation()) return;
      if (!window.confirm("将此历史版本复制为新的草稿版本？原历史版本不会被覆盖。")) return;
      try {
        await formalActionController.runMutation(async () => {
          const payload = await action("republish", { version_id: button.dataset.templateRepublishVersion });
          window.location.href = `/templates/v1/${encodeURIComponent(templateType)}?version_id=${encodeURIComponent(payload.id)}`;
        }, {
          key: `republish:${button.dataset.templateRepublishVersion}`,
          keepFrozenOnSuccess: true,
        });
      } catch (error) { saveCoordinator.report("error", { error }); }
    }));

    window.addEventListener("beforeunload", (event) => {
      if (!saveCoordinator.hasDirty()) return;
      event.preventDefault();
      event.returnValue = "";
    });
    if (editable && !lockToken) ensureLock().catch((error) => { saveCoordinator.report("error", { error }); });
    if (editable) {
      const renewEditorLock = () => ensureLock(true).catch((error) => {
        saveCoordinator.report("error", { error });
      });
      window.setInterval(renewEditorLock, 5 * 60 * 1000);
      window.addEventListener("focus", renewEditorLock);
      document.addEventListener("visibilitychange", () => {
        if (document.visibilityState === "visible") renewEditorLock();
      });
    }
  }
  initContractTemplateEditorV1(document);
}(window));
