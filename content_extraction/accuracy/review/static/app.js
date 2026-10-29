(function (root) {
  "use strict";

  const stageCopy = {
    FIRST_PASS: {
      heading: "第一遍：独立听写",
      instruction: "先播放原始片段，再填写第一遍听写。机器证据默认折叠，仅用于必要的时间定位。",
    },
    SECOND_PASS: {
      heading: "第二遍：独立重新听写",
      instruction: "第一遍文字和全部机器输出已由服务端隐藏。请重新播放原始片段，从空白输入框独立听写。",
    },
    RECONCILIATION: {
      heading: "两遍不一致：人工核对",
      instruction: "请同时参考两遍听写并再次播放原片。Provider 只可辅助判断，不能自动成为最终答案。",
    },
    COMPLETE: {
      heading: "本条审核已完成",
      instruction: "可查看结果或切换到下一条未完成样本。",
    },
  };

  function buildViewModel(sample) {
    const stage = sample.review_stage || "COMPLETE";
    const copy = stageCopy[stage] || stageCopy.COMPLETE;
    return {
      stage,
      heading: copy.heading,
      instruction: copy.instruction,
      showEvidence: stage !== "SECOND_PASS",
      showPasses: stage === "RECONCILIATION",
      firstPassText: sample.first_pass_text || "",
      secondPassText: sample.second_pass_text || "",
      initialText: "",
    };
  }

  function reviewPayload(stage, action, text) {
    return { stage, action, text };
  }

  function focusEditor(editor) {
    editor.focus();
  }

  root.GroundTruthReviewUI = { buildViewModel, reviewPayload, focusEditor };
  if (typeof document === "undefined") return;

  const byId = (id) => document.getElementById(id);
  const elements = {
    progress: byId("progress"), readiness: byId("readiness"), sampleId: byId("sample-id"),
    sampleTime: byId("sample-time"), stageBadge: byId("stage-badge"),
    heading: byId("stage-heading"), instruction: byId("stage-instruction"),
    player: byId("clip-player"), passes: byId("passes-panel"),
    firstPass: byId("first-pass-text"), secondPass: byId("second-pass-text"),
    evidence: byId("evidence-panel"), clean: byId("clean-text"),
    providers: byId("provider-texts"), alternatives: byId("alternatives"),
    conflicts: byId("conflicts"), critical: byId("critical-tokens"),
    editor: byId("human-text"), message: byId("message"),
    confirm: byId("confirm-button"), edit: byId("edit-button"),
    ambiguous: byId("ambiguous-button"), skip: byId("skip-button"),
    previous: byId("previous-button"), next: byId("next-button"),
  };
  const token = document.querySelector('meta[name="review-token"]').content;
  let payload = null;
  let index = 0;

  function formatTime(ms) {
    const seconds = Math.max(0, Number(ms || 0)) / 1000;
    const minutes = Math.floor(seconds / 60);
    return `${minutes}:${(seconds % 60).toFixed(1).padStart(4, "0")}`;
  }

  function replaceTextList(target, values) {
    target.replaceChildren();
    const list = Array.isArray(values) ? values : [];
    if (!list.length) {
      const item = document.createElement("li");
      item.textContent = "无";
      target.append(item);
      return;
    }
    list.forEach((value) => {
      const item = document.createElement("li");
      item.textContent = typeof value === "string" ? value : JSON.stringify(value);
      target.append(item);
    });
  }

  function replaceProviders(values) {
    elements.providers.replaceChildren();
    Object.entries(values || {}).forEach(([provider, text]) => {
      if (!text) return;
      const row = document.createElement("div");
      const term = document.createElement("dt");
      const description = document.createElement("dd");
      term.textContent = provider;
      description.textContent = text;
      row.append(term, description);
      elements.providers.append(row);
    });
    if (!elements.providers.childElementCount) {
      const row = document.createElement("div");
      const description = document.createElement("dd");
      description.textContent = "此样本没有可用的 Provider 证据";
      row.append(description);
      elements.providers.append(row);
    }
  }

  function currentSample() {
    return payload && payload.samples[index];
  }

  function render() {
    const sample = currentSample();
    if (!sample) return;
    const view = buildViewModel(sample);
    const completed = payload.samples.filter((item) => item.review_stage === "COMPLETE").length;
    elements.progress.textContent = `${index + 1} / ${payload.sample_count} · 已完成 ${completed}`;
    elements.readiness.textContent = payload.readiness.status === "READY"
      ? "已达到 Model Suitability Benchmark 最低数据门槛"
      : `尚未达到门槛：Human Verified ${payload.readiness.human_verified_count}，无需纠正 ${payload.readiness.no_correction_needed_count}，视频 ${payload.readiness.source_video_count}`;
    elements.sampleId.textContent = sample.sample_id;
    elements.sampleTime.textContent = `${formatTime(sample.source.start_ms)} – ${formatTime(sample.source.end_ms)}`;
    elements.stageBadge.textContent = view.stage;
    elements.heading.textContent = view.heading;
    elements.instruction.textContent = view.instruction;
    elements.player.src = sample.clip_url || "";
    elements.passes.hidden = !view.showPasses;
    elements.firstPass.textContent = view.firstPassText;
    elements.secondPass.textContent = view.secondPassText;
    elements.evidence.hidden = !view.showEvidence;
    elements.evidence.open = false;
    elements.clean.textContent = sample.current_clean_raw_transcript || sample.before_text || "无";
    replaceProviders(sample.provider_texts);
    replaceTextList(elements.alternatives, sample.alternatives);
    replaceTextList(elements.conflicts, sample.conflicts);
    replaceTextList(elements.critical, sample.critical_tokens);
    elements.editor.value = view.initialText;
    elements.editor.disabled = view.stage === "COMPLETE";
    elements.confirm.disabled = view.stage === "COMPLETE";
    elements.edit.disabled = view.stage === "COMPLETE";
    elements.ambiguous.disabled = view.stage === "COMPLETE";
    elements.skip.disabled = view.stage === "COMPLETE";
    elements.previous.disabled = index === 0;
    elements.next.disabled = index >= payload.samples.length - 1;
    elements.message.textContent = "";
  }

  async function loadReviewSet(preferredSampleId) {
    const response = await fetch("/api/review-set", { cache: "no-store" });
    if (!response.ok) throw new Error("无法载入审核数据");
    payload = await response.json();
    const preferred = payload.samples.findIndex((item) => item.sample_id === preferredSampleId);
    if (preferred >= 0) index = preferred;
    else {
      const firstIncomplete = payload.samples.findIndex((item) => item.review_stage !== "COMPLETE");
      index = firstIncomplete >= 0 ? firstIncomplete : 0;
    }
    render();
  }

  async function submit(action) {
    const sample = currentSample();
    if (!sample || sample.review_stage === "COMPLETE") return;
    elements.message.textContent = "正在保存…";
    [elements.confirm, elements.ambiguous, elements.skip].forEach((button) => { button.disabled = true; });
    try {
      const response = await fetch(`/api/review/${encodeURIComponent(sample.sample_id)}`, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "X-Review-Token": token,
        },
        body: JSON.stringify(reviewPayload(sample.review_stage, action, elements.editor.value)),
      });
      const result = await response.json();
      if (!response.ok) throw new Error(result.error || "保存失败");
      await loadReviewSet(sample.sample_id);
    } catch (error) {
      elements.message.textContent = error.message;
      render();
      elements.message.textContent = error.message;
    }
  }

  elements.confirm.addEventListener("click", () => submit("CONFIRM"));
  elements.edit.addEventListener("click", () => focusEditor(elements.editor, currentSample()));
  elements.ambiguous.addEventListener("click", () => submit("AMBIGUOUS"));
  elements.skip.addEventListener("click", () => submit("SKIP"));
  elements.previous.addEventListener("click", () => { index -= 1; render(); });
  elements.next.addEventListener("click", () => { index += 1; render(); });

  loadReviewSet().catch((error) => {
    elements.heading.textContent = "审核工具无法启动";
    elements.message.textContent = error.message;
  });
}(window));
