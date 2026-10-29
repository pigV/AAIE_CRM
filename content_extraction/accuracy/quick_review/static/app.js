(function (root) {
  "use strict";

  function splitCarrier(text, start, end) {
    return { prefix: text.slice(0, start), suspect: text.slice(start, end), suffix: text.slice(end) };
  }

  function resolutionPayload({ queueHash, revision, action, selectedText, candidateReferences }) {
    return {
      source_queue_hash: queueHash,
      expected_revision: revision,
      action,
      selected_text: selectedText,
      candidate_references: candidateReferences || [],
      reviewer_source: "quick_review_ui",
    };
  }

  root.QuickReviewUI = { splitCarrier, resolutionPayload };
  if (typeof document === "undefined") return;

  const id = (name) => document.getElementById(name);
  const token = document.querySelector('meta[name="review-token"]').content;
  const el = {
    progress: id("progress"), risk: id("risk"), time: id("time"), clip: id("clip"),
    before: id("before"), carrier: id("carrier"), after: id("after"), candidates: id("candidates"),
    customToggle: id("custom-toggle"), customPanel: id("custom-panel"), customText: id("custom-text"),
    customSave: id("custom-save"), ambiguous: id("ambiguous"), evidence: id("evidence"),
    message: id("message"), previous: id("previous"), next: id("next"),
  };
  let data = null;
  let index = 0;

  function current() { return data && data.items[index]; }
  function formatMs(value) { return `${(Number(value || 0) / 1000).toFixed(1)}s`; }
  function appendCarrier(item) {
    const parts = splitCarrier(item.carrier_text, item.start_offset, item.end_offset);
    el.carrier.replaceChildren();
    el.carrier.append(document.createTextNode(parts.prefix));
    const mark = document.createElement("mark");
    mark.textContent = parts.suspect;
    el.carrier.append(mark, document.createTextNode(parts.suffix));
  }
  function render() {
    const item = current();
    if (!item) { el.progress.textContent = "没有需要核对的内容"; return; }
    const done = data.items.filter((value) => value.status !== "PENDING").length;
    el.progress.textContent = `${index + 1} / ${data.items.length} · 已完成 ${done}`;
    el.risk.textContent = item.risk_level;
    el.time.textContent = `${formatMs(item.start_ms)} – ${formatMs(item.end_ms)}`;
    el.clip.src = item.clip_url;
    el.before.textContent = item.context_before || "";
    appendCarrier(item);
    el.after.textContent = item.context_after || "";
    el.candidates.replaceChildren();
    item.alternatives.forEach((alternative) => {
      const button = document.createElement("button");
      button.className = "candidate primary";
      button.textContent = alternative.text;
      button.disabled = item.status !== "PENDING";
      button.addEventListener("click", () => submit("CONFIRMED", alternative.text, [`alternative:${alternative.text}`]));
      el.candidates.append(button);
    });
    el.customToggle.disabled = item.status !== "PENDING";
    el.ambiguous.disabled = item.status !== "PENDING";
    el.evidence.textContent = JSON.stringify({
      reason: item.reason_for_review, critical: item.critical_token_type,
      ocr: item.ocr_evidence, asr: item.asr_evidence, qwen: item.qwen_proposal,
    }, null, 2);
    el.previous.disabled = index === 0;
    el.next.disabled = index === data.items.length - 1;
    el.message.textContent = item.status === "PENDING" ? "" : `已保存：${item.status}`;
  }
  async function load(preferred) {
    const response = await fetch("/api/queue", { cache: "no-store" });
    if (!response.ok) throw new Error("无法载入核对列表");
    data = await response.json();
    if (preferred) {
      const found = data.items.findIndex((item) => item.review_id === preferred);
      if (found >= 0) index = found;
    } else {
      const pending = data.items.findIndex((item) => item.status === "PENDING");
      index = pending >= 0 ? pending : 0;
    }
    render();
  }
  async function submit(action, selectedText, references) {
    const item = current();
    if (!item) return;
    el.message.textContent = "正在安全保存…";
    try {
      const response = await fetch(`/api/review/${encodeURIComponent(item.review_id)}`, {
        method: "POST",
        headers: { "Content-Type": "application/json", "X-Review-Token": token },
        body: JSON.stringify(resolutionPayload({
          queueHash: data.queue_hash, revision: data.revision, action,
          selectedText, candidateReferences: references,
        })),
      });
      const result = await response.json();
      if (!response.ok) throw new Error(result.error || "保存失败");
      await load(item.review_id);
    } catch (error) { el.message.textContent = error.message; }
  }
  el.customToggle.addEventListener("click", () => { el.customPanel.hidden = false; el.customText.focus(); });
  el.customSave.addEventListener("click", () => submit("EDITED", el.customText.value.trim(), []));
  el.ambiguous.addEventListener("click", () => submit("AMBIGUOUS", "", []));
  el.previous.addEventListener("click", () => { index -= 1; render(); });
  el.next.addEventListener("click", () => { index += 1; render(); });
  load().catch((error) => { el.message.textContent = error.message; });
}(window));
