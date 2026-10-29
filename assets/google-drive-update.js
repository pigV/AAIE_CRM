(function (globalScope) {
  "use strict";

  const eligibleSelection = (items) => (items || [])
    .filter((item) => item.eligible && item.checked)
    .map((item) => item.id);

  const selectAllEligible = (items) => (items || []).map((item) => ({
    ...item,
    checked: Boolean(item.eligible),
  }));

  const actionButtonLabel = (action, locked) => {
    const labels = {
      scan: ["立即检查 Drive", "正在扫描 Drive，请稍候…"],
      confirm: ["确认更新 CRM", "正在更新 CRM，请稍候…"],
      start: ["启动自动更新", "正在检查服务状态…"],
      pause: ["暂停自动更新", "正在暂停未来任务…"],
    };
    const pair = labels[action] || ["提交", "处理中…"];
    return pair[locked ? 1 : 0];
  };

  const createScanPoller = (dependencies) => {
    const terminalStates = new Set(["completed", "failed", "interrupted"]);
    const schedule = dependencies.schedule || ((callback, delay) => globalScope.setTimeout(callback, delay));
    const delay = Number(dependencies.delay || 1000);
    let active = false;
    let scheduled = false;
    let polling = false;
    let currentJobId = "";
    let statusUrl = "";

    const scheduleNext = () => {
      if (!active || scheduled || polling) return;
      scheduled = true;
      schedule(async () => {
        scheduled = false;
        await poll();
      }, delay);
    };

    const poll = async () => {
      if (!active || polling) return;
      polling = true;
      try {
        const job = await dependencies.request(statusUrl);
        if (job.job_id && job.job_id !== currentJobId) throw new Error("Scan job identity changed");
        dependencies.onProgress(job);
        if (terminalStates.has(job.state)) {
          active = false;
          dependencies.onComplete(job);
        }
      } catch (error) {
        dependencies.onError(error);
      } finally {
        polling = false;
        scheduleNext();
      }
    };

    return {
      start(job) {
        const nextJobId = String(job.job_id || "");
        const nextStatusUrl = String(job.status_url || job.statusUrl || "");
        if (!nextJobId || !nextStatusUrl) return false;
        if (active && currentJobId === nextJobId) return true;
        currentJobId = nextJobId;
        statusUrl = nextStatusUrl;
        active = !terminalStates.has(job.state);
        dependencies.onProgress(job);
        if (active) scheduleNext();
        else dependencies.onComplete(job);
        return true;
      },
      stop() { active = false; },
      isActive() { return active; },
      jobId() { return currentJobId; },
    };
  };

  const createDriveUpdateController = (dependencies) => {
    const inFlight = new Set();
    let request = dependencies.request;

    return {
      setRequest(nextRequest) {
        request = nextRequest;
      },
      isInFlight(action) {
        return inFlight.has(action);
      },
      async submit(action, payload) {
        if (inFlight.has(action)) return { ignored: true };
        if (action === "confirm" && !(payload.selectedIds || []).length) {
          return { validationError: true };
        }
        if (action === "start" && !dependencies.confirmStart()) {
          return { cancelled: true };
        }
        inFlight.add(action);
        dependencies.setLocked(action, true);
        try {
          const result = await request(action, payload);
          dependencies.render(result);
          dependencies.focusResult({ preventScroll: true });
          return result;
        } finally {
          inFlight.delete(action);
          dependencies.setLocked(action, false);
        }
      },
    };
  };

  const initializeDriveUpdatePage = (documentRef) => {
    const root = documentRef.querySelector(".google-drive-update-page");
    if (!root) return null;
    const forms = {
      scan: root.querySelector("[data-drive-scan]"),
      confirm: root.querySelector("[data-drive-confirm]"),
      start: root.querySelector("[data-drive-start]"),
      pause: root.querySelector("[data-drive-pause]"),
    };

    const setLocked = (action, locked) => {
      const form = forms[action];
      if (!form) return;
      const submitButton = form.querySelector('button[type="submit"]');
      if (submitButton) submitButton.textContent = actionButtonLabel(action, locked);
      form.querySelectorAll("button, input").forEach((control) => {
        if (control.name !== "csrf" && control.name !== "confirmation_nonce") {
          control.disabled = Boolean(locked) || (control.dataset.driveSelection !== undefined && !control.dataset.eligible);
        }
      });
      form.dataset.requestInFlight = locked ? "true" : "false";
    };

    const request = async (action) => {
      const form = forms[action];
      const response = await fetch(form.action, {
        method: form.method || "POST",
        body: new FormData(form),
        headers: { Accept: "application/json" },
        credentials: "same-origin",
      });
      const result = await response.json();
      if (!response.ok) throw new Error(result.error || "Google Drive request failed");
      return result;
    };

    const render = (result) => {
      if (result.job_id && globalScope.history && globalScope.location) {
        const nextUrl = new URL(globalScope.location.href);
        nextUrl.searchParams.set("tab", "sync");
        nextUrl.searchParams.set("scan_job_id", result.job_id);
        nextUrl.searchParams.delete("scan_id");
        globalScope.history.replaceState({}, "", nextUrl);
      }
      if (result.scan_id && globalScope.history && globalScope.location) {
        const nextUrl = new URL(globalScope.location.href);
        nextUrl.searchParams.set("tab", "sync");
        nextUrl.searchParams.set("scan_id", result.scan_id);
        nextUrl.searchParams.delete("scan_job_id");
        globalScope.history.replaceState({}, "", nextUrl);
      }
      if (result.workspace_html) {
        root.outerHTML = result.workspace_html;
        initializeDriveUpdatePage(documentRef);
        return;
      }
      const container = root.querySelector("[data-drive-result-container]");
      if (container && result.html) container.innerHTML = result.html;
    };

    const focusResult = (options) => {
      const heading = documentRef.querySelector("[data-drive-result-heading]");
      if (heading) heading.focus(options);
    };

    const controller = createDriveUpdateController({
      request,
      confirmStart: () => globalScope.confirm("确认启动每天 23:00 的 Google Drive 自动更新？"),
      setLocked,
      render,
      focusResult,
    });

    Object.entries(forms).forEach(([action, form]) => {
      if (!form) return;
      form.addEventListener("submit", async (event) => {
        event.preventDefault();
        const selectedIds = action === "confirm"
          ? Array.from(form.querySelectorAll("[data-drive-selection]:checked:not(:disabled)")).map((input) => input.value)
          : [];
        try {
          const result = await controller.submit(action, { selectedIds });
          if (result && result.validationError) {
            const heading = root.querySelector("[data-drive-result-heading]");
            if (heading) heading.textContent = "请至少选择一条可更新记录";
          }
        } catch (error) {
          const container = root.querySelector("[data-drive-result-container]");
          if (container) {
            container.innerHTML = '<section class="panel drive-report"><h3 data-drive-result-heading tabindex="-1">操作未完成</h3><p data-drive-error></p></section>';
            const message = container.querySelector("[data-drive-error]");
            if (message) message.textContent = String(error.message || error);
          }
          focusResult({ preventScroll: true });
        }
      });
    });

    const confirmationForm = forms.confirm;
    if (confirmationForm) {
      const confirmButton = confirmationForm.querySelector("[data-drive-confirm-button]");
      const selections = Array.from(confirmationForm.querySelectorAll("[data-drive-selection]"));
      selections.forEach((selection) => { selection.dataset.eligible = selection.disabled ? "" : "true"; });
      const updateConfirmState = () => {
        if (confirmButton) confirmButton.disabled = !selections.some((selection) => selection.checked && !selection.disabled);
      };
      selections.forEach((selection) => selection.addEventListener("change", updateConfirmState));
      const selectAll = confirmationForm.querySelector("[data-drive-select-all]");
      if (selectAll) selectAll.addEventListener("change", () => {
        selections.forEach((selection) => { selection.checked = selectAll.checked && !selection.disabled; });
        updateConfirmState();
      });
      updateConfirmState();
    }

    const logToggle = root.querySelector("[data-drive-log-toggle]");
    const logs = root.querySelector("[data-drive-logs]");
    if (logToggle && logs) logToggle.addEventListener("click", () => {
      logs.hidden = !logs.hidden;
      logToggle.textContent = logs.hidden ? "查看同步日志" : "收起同步日志";
    });

    const progressCard = root.querySelector("[data-drive-scan-progress]");
    if (progressCard) {
      const phaseLabels = {
        queued: "等待开始",
        connecting: "连接 Google Drive",
        listing: "读取文件目录",
        reading: "文件内容读取",
        deduplicating: "CRM 重复检查",
        previewing: "生成扫描预览",
        completed: "扫描完成",
        failed: "扫描失败",
        interrupted: "扫描已中断",
      };
      const updateProgress = (job) => {
        const percent = Math.max(0, Math.min(100, Number(job.percent || 0)));
        const phase = progressCard.querySelector("[data-drive-progress-phase]");
        const percentText = progressCard.querySelector("[data-drive-progress-percent]");
        const count = progressCard.querySelector("[data-drive-progress-count]");
        const bar = progressCard.querySelector("[data-drive-progress-bar]");
        const message = progressCard.querySelector("[data-drive-progress-message]");
        progressCard.dataset.scanState = String(job.state || "running");
        if (phase) phase.textContent = phaseLabels[job.phase] || String(job.phase || "扫描中");
        if (percentText) percentText.textContent = `${percent}%`;
        if (count) count.textContent = `${Number(job.processed || 0)} / ${Number(job.total || 0)}`;
        if (bar) {
          bar.setAttribute("aria-valuenow", String(percent));
          const fill = bar.querySelector("span");
          if (fill) fill.style.width = `${percent}%`;
        }
        if (message) message.textContent = String(job.message || "正在扫描…");
      };
      const poller = createScanPoller({
        request: async (statusUrl) => {
          const response = await fetch(statusUrl, { headers: { Accept: "application/json" }, credentials: "same-origin" });
          const result = await response.json();
          if (!response.ok) throw new Error(result.error || "无法读取扫描进度");
          return result;
        },
        onProgress: updateProgress,
        onComplete: (job) => {
          if (job.workspace_html) {
            render(job);
            return;
          }
          updateProgress(job);
          const scanButton = forms.scan && forms.scan.querySelector('button[type="submit"]');
          if (scanButton) scanButton.disabled = false;
        },
        onError: () => {
          const message = progressCard.querySelector("[data-drive-progress-message]");
          if (message) message.textContent = "网络暂时中断，正在重试当前扫描任务…";
        },
      });
      poller.start({
        job_id: progressCard.dataset.scanJobId,
        status_url: progressCard.dataset.statusUrl,
        state: progressCard.dataset.scanState,
        phase: progressCard.dataset.scanPhase,
        percent: progressCard.dataset.scanPercent,
        processed: progressCard.dataset.scanProcessed,
        total: progressCard.dataset.scanTotal,
        message: progressCard.querySelector("[data-drive-progress-message]")?.textContent || "",
      });
    }
    return controller;
  };

  const exported = { actionButtonLabel, createDriveUpdateController, createScanPoller, eligibleSelection, selectAllEligible, initializeDriveUpdatePage };
  if (typeof module !== "undefined" && module.exports) module.exports = exported;
  if (globalScope && globalScope.document) {
    globalScope.document.addEventListener("DOMContentLoaded", () => initializeDriveUpdatePage(globalScope.document));
  }
})(typeof window !== "undefined" ? window : globalThis);
