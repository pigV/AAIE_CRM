(function (root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  if (root) root.AAIEParentForm = api;
})(typeof globalThis !== "undefined" ? globalThis : this, function () {
  "use strict";

  function createParentFormDraftController(options) {
    const delayMs = Number(options.delayMs || 2000);
    const setTimer = options.setTimer || setTimeout;
    const clearTimer = options.clearTimer || clearTimeout;
    const save = options.save;
    let timer = null;

    const cancelPending = () => {
      if (timer !== null) clearTimer(timer);
      timer = null;
    };

    const changed = () => {
      cancelPending();
      timer = setTimer(() => {
        timer = null;
        Promise.resolve(save("auto")).catch(() => {});
      }, delayMs);
    };

    const saveNow = () => {
      cancelPending();
      return save("manual");
    };

    return { changed, saveNow, cancelPending };
  }

  function bootParentFormDraft(documentRef, windowRef) {
    const form = documentRef.querySelector("[data-public-parent-form]");
    if (!form || form.dataset.parentDraftReady === "true") return null;
    const draftUrl = form.dataset.parentFormDraftUrl;
    if (!draftUrl) return null;
    form.dataset.parentDraftReady = "true";

    const syncStreamFields = () => {
      const stream = form.querySelector('[name="application_stream"]')?.value || "";
      const courseType = form.querySelector('[name="higher_education_course_type"]')?.value || "";
      form.querySelectorAll("[data-visible-for-stream]").forEach((field) => {
        const streamVisible = field.dataset.visibleForStream === stream;
        const courseVisible = !field.dataset.visibleForCourseType || field.dataset.visibleForCourseType === courseType;
        const visible = streamVisible && courseVisible;
        field.hidden = !visible;
        field.querySelectorAll("input,select,textarea").forEach((control) => {
          control.disabled = !visible;
          control.required = visible && field.dataset.required === "1";
        });
      });
    };
    form.querySelector('[name="application_stream"]')?.addEventListener("change", syncStreamFields);
    form.querySelector('[name="higher_education_course_type"]')?.addEventListener("change", syncStreamFields);
    syncStreamFields();

    const status = form.querySelector("[data-parent-form-save-status]");
    const saveButton = form.querySelector("[data-parent-form-save]");
    let revision = Number(form.dataset.parentFormRevision || 0);
    let changeVersion = 0;
    let savedVersion = 0;
    let activeSave = null;

    const setStatus = (message, state) => {
      if (!status) return;
      status.textContent = message;
      status.dataset.state = state || "idle";
    };

    const displayTime = (value) => {
      const parsed = value ? new Date(value) : new Date();
      return Number.isNaN(parsed.getTime())
        ? "刚刚"
        : parsed.toLocaleTimeString("zh-CN", { hour: "2-digit", minute: "2-digit" });
    };

    const collectDraft = (source) => {
      const body = new FormData(form);
      body.delete("declaration");
      body.set("revision", String(revision));
      body.set("save_source", source);
      return body;
    };

    const performSave = async (source, options) => {
      const requestedVersion = changeVersion;
      const body = collectDraft(source);
      if (!options?.silent) {
        setStatus(source === "manual" ? "正在保存草稿…" : "正在自动保存…", "saving");
      }
      if (saveButton) saveButton.disabled = true;
      try {
        const response = await windowRef.fetch(draftUrl, {
          method: "POST",
          body,
          credentials: "same-origin",
          keepalive: Boolean(options?.keepalive),
          headers: { "Accept": "application/json", "X-Requested-With": "fetch" },
        });
        const data = await response.json().catch(() => ({}));
        if (!response.ok || data.ok === false) {
          const error = new Error(data.message || "草稿保存失败，请点击保存草稿重试。");
          error.status = response.status;
          error.payload = data;
          throw error;
        }
        revision = Number(data.revision || revision);
        form.dataset.parentFormRevision = String(revision);
        if (requestedVersion > savedVersion) savedVersion = requestedVersion;
        const label = source === "manual" ? "已保存" : "已自动保存";
        setStatus(`${label} ${displayTime(data.saved_at)}`, "saved");
        return data;
      } catch (error) {
        if (!options?.silent) {
          if (error?.status === 409) {
            setStatus("发现较新的草稿，请重新载入后继续填写", "conflict");
          } else {
            setStatus(source === "manual" ? "保存失败，请重试" : "自动保存失败，请点击保存草稿", "error");
          }
        }
        throw error;
      } finally {
        if (saveButton) saveButton.disabled = false;
      }
    };

    const saveDraft = (source, options) => {
      const run = async () => {
        if (activeSave) {
          try { await activeSave; } catch (_) {}
        }
        if (source === "auto" && savedVersion >= changeVersion) return null;
        activeSave = performSave(source, options);
        try {
          return await activeSave;
        } finally {
          activeSave = null;
        }
      };
      return run();
    };

    const controller = createParentFormDraftController({
      delayMs: 2000,
      save: (source) => saveDraft(source),
    });

    const markChanged = (event) => {
      if (event.target?.name === "declaration") return;
      changeVersion += 1;
      setStatus("有未保存的更改", "dirty");
      controller.changed();
    };
    form.addEventListener("input", markChanged);
    form.addEventListener("change", markChanged);
    saveButton?.addEventListener("click", () => {
      controller.saveNow().catch(() => {});
    });
    windowRef.addEventListener("pagehide", () => {
      if (savedVersion < changeVersion) {
        controller.cancelPending();
        saveDraft("auto", { keepalive: true, silent: true }).catch(() => {});
      }
    });

    const publicApi = {
      saveNow: () => controller.saveNow(),
      cancelPending: controller.cancelPending,
      getRevision: () => revision,
    };
    windowRef.aaieParentFormDraft = publicApi;
    return publicApi;
  }

  if (typeof document !== "undefined" && typeof window !== "undefined") {
    const boot = () => bootParentFormDraft(document, window);
    if (document.readyState === "loading") {
      document.addEventListener("DOMContentLoaded", boot, { once: true });
    } else {
      boot();
    }
  }

  return { createParentFormDraftController, bootParentFormDraft };
});
