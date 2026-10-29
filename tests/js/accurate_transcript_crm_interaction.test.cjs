const assert = require("node:assert/strict");
const { readFileSync } = require("node:fs");
const test = require("node:test");
const vm = require("node:vm");
const { resolve } = require("node:path");

const root = resolve(__dirname, "../..");
const source = readFileSync(resolve(root, "assets/app.js"), "utf8");
const css = readFileSync(resolve(root, "assets/ui-premium-v2.css"), "utf8");
const window = {};
const marker = "/* Accurate Transcript CRM Review */";
const helperSource = source.includes(marker) ? source.slice(source.indexOf(marker)) : "";
vm.runInNewContext(helperSource, { window });
const ui = window.AccurateTranscriptCRMUI;

test("suspect highlighting preserves exact prefix and suffix", () => {
  const result = ui.splitCarrier("学生签证是500类别", 5, 8);
  assert.deepEqual(JSON.parse(JSON.stringify(result)), {
    prefix: "学生签证是", suspect: "500", suffix: "类别",
  });
});

test("candidate and ambiguous actions create CRM form fields", () => {
  const candidate = ui.resolutionFields({
    csrf: "csrf", queueHash: "sha256:q", revision: 2,
    action: "CONFIRMED", selectedText: "590",
    candidateReferences: ["alternative:590"],
  });
  assert.deepEqual(JSON.parse(JSON.stringify(candidate)), {
    csrf: "csrf", source_queue_hash: "sha256:q", expected_revision: "2",
    action: "CONFIRMED", selected_text: "590",
    candidate_references: '["alternative:590"]',
  });
  assert.equal(ui.resolutionFields({
    csrf: "c", queueHash: "q", revision: 0,
    action: "AMBIGUOUS", selectedText: "", candidateReferences: [],
  }).action, "AMBIGUOUS");
});

test("review card uses business language and CRM media proxy", () => {
  const html = ui.reviewCardMarkup({
    review_id: "r1", carrier_text: "学生签证是500类别", start_offset: 5,
    end_offset: 8, suspect_span: "500", context_before: "前文",
    context_after: "后文", alternatives: [{ text: "590" }],
    status: "PENDING", risk_level: "P0", evidence: { asr: { one: "500" } },
  }, "/content/extractions/18/accurate-review/media/r1.m4v");
  assert.match(html, /当前文字/);
  assert.match(html, /保留原文/);
  assert.match(html, /选择候选/);
  assert.match(html, /手工修改/);
  assert.match(html, /机器参考/);
  assert.match(html, /技术详情/);
  assert.match(html, /accurate-review\/media/);
  const primary = html.split("技术详情")[0];
  for (const term of ["Paddle", "Apple Vision", "FunASR", "Whisper", "Fusion", "Qwen"]) {
    assert.equal(primary.includes(term), false);
  }
});

test("pending-only partition keeps ambiguous unresolved and restores completed history", () => {
  const groups = ui.partitionReviewItems([
    { review_id: "p", status: "PENDING" },
    { review_id: "a", status: "AMBIGUOUS" },
    { review_id: "c", status: "CONFIRMED" },
    { review_id: "e", status: "EDITED" },
  ]);
  assert.deepEqual(JSON.parse(JSON.stringify(groups.pending.map((item) => item.review_id))), ["p", "a"]);
  assert.deepEqual(JSON.parse(JSON.stringify(groups.completed.map((item) => item.review_id))), ["c", "e"]);
});

test("confirmation advances to the next unresolved item in queue order", () => {
  const items = [
    { review_id: "first", status: "CONFIRMED" },
    { review_id: "second", status: "PENDING" },
    { review_id: "third", status: "AMBIGUOUS" },
  ];
  assert.equal(ui.nextPendingReviewId(items, "first"), "second");
  assert.equal(ui.nextPendingReviewId(items, "second"), "third");
});

test("machine reference summary is business-facing and never dumps provider internals", () => {
  const summary = ui.machineReferenceMarkup({
    ocr: { paddle: "字幕甲", apple_vision: "字幕乙" },
    asr: { funasr: "语音甲", whispercpp: "语音乙" },
  });
  assert.match(summary, /字幕识别候选/);
  assert.match(summary, /语音识别候选/);
  assert.match(summary, /机器存在分歧/);
  for (const term of ["paddle", "apple_vision", "funasr", "whispercpp"]) {
    assert.equal(summary.includes(term), false);
  }
});

test("mobile styles guarantee touch targets and prevent horizontal overflow", () => {
  const reviewCss = css.slice(css.indexOf(".content-accurate-review-page"));
  assert.match(reviewCss, /min-height:\s*(44|4[5-9]|[5-9]\d)px/);
  assert.match(reviewCss, /overflow-x:\s*(clip|hidden)/);
  assert.match(reviewCss, /@media\s*\(max-width:\s*430px\)/);
  assert.match(reviewCss, /content-accurate-editor-actions[\s\S]*min-height:\s*44px/);
});

test("transcript highlight escapes text and marks only uncertainty spans", () => {
  const html = ui.highlightedTranscript("孩子考杂了要出国", [{
    review_id: "r1", start_offset: 2, end_offset: 5, current_text: "考杂了",
  }], true);
  assert.match(html, /孩子<mark[^>]*>考杂了<\/mark>要出国/);
  assert.equal(ui.highlightedTranscript("<危险>", [], true), "&lt;危险&gt;");
});

test("candidate patch is local and shifts later uncertainty offsets", () => {
  const result = ui.applyTranscriptCandidate("孩子考杂了要出国，公签也可以", [
    { review_id: "r1", start_offset: 2, end_offset: 5, current_text: "考杂了" },
    { review_id: "r2", start_offset: 10, end_offset: 12, current_text: "公签" },
  ], "r1", "考砸了");
  assert.equal(result.text, "孩子考砸了要出国，公签也可以");
  assert.deepEqual(JSON.parse(JSON.stringify(result.uncertainties)), [
    { review_id: "r2", start_offset: 10, end_offset: 12, current_text: "公签" },
  ]);
});

test("editor save payload contains current text and remaining uncertainties", () => {
  const payload = ui.transcriptSaveFields("csrf", "当前全文", [{ review_id: "r2" }]);
  assert.deepEqual(JSON.parse(JSON.stringify(payload)), {
    csrf: "csrf", final_transcript: "当前全文",
    uncertainty_metadata: '[{"review_id":"r2"}]',
  });
});

function responseStub({ status = 200, contentType = "application/json; charset=utf-8", payload = {}, redirected = false, url = "/content/extractions/30/accurate-transcript" } = {}) {
  return {
    ok: status >= 200 && status < 300,
    status,
    redirected,
    url,
    headers: { get: (name) => name.toLowerCase() === "content-type" ? contentType : null },
    json: async () => payload,
  };
}

test("safe JSON response accepts the save API 200 contract", async () => {
  const payload = await ui.readSafeJsonResponse(responseStub({ payload: { ok: true, final_transcript: "当前全文" } }), {
    route: "/content/extractions/30/accurate-transcript",
  });
  assert.deepEqual(JSON.parse(JSON.stringify(payload)), { ok: true, final_transcript: "当前全文" });
});

for (const [label, response] of [
  ["login redirect HTML", responseStub({ status: 200, contentType: "text/html; charset=utf-8", redirected: true, url: "/login" })],
  ["404 HTML", responseStub({ status: 404, contentType: "text/html; charset=utf-8" })],
  ["500 HTML", responseStub({ status: 500, contentType: "text/html; charset=utf-8" })],
]) {
  test(`safe JSON response rejects ${label} without parsing or exposing HTML`, async () => {
    const diagnostics = [];
    await assert.rejects(
      ui.readSafeJsonResponse(response, {
        route: "/content/extractions/30/accurate-transcript",
        onDiagnostic: (diagnostic) => diagnostics.push(diagnostic),
      }),
      (error) => error.message === "操作失败，请刷新页面后重试。" && !error.message.includes("<!DOCTYPE"),
    );
    assert.equal(diagnostics.length, 1);
    assert.equal(diagnostics[0].status, response.status);
    assert.equal(diagnostics[0].route, "/content/extractions/30/accurate-transcript");
    assert.equal(diagnostics[0].contentType, "text/html; charset=utf-8");
  });
}

test("copy helper copies the exact current editor value", async () => {
  const calls = [];
  const copied = await ui.copyCurrentTranscript("编辑后的\n完整文案", {
    writeText: async (value) => calls.push(value),
  }, () => false);
  assert.equal(copied, true);
  assert.deepEqual(calls, ["编辑后的\n完整文案"]);
});

test("copy helper falls back deterministically when Clipboard API fails", async () => {
  const fallbackValues = [];
  const copied = await ui.copyCurrentTranscript("当前文案", {
    writeText: async () => { throw new Error("permission denied"); },
  }, (value) => { fallbackValues.push(value); return true; });
  assert.equal(copied, true);
  assert.deepEqual(fallbackValues, ["当前文案"]);
});

test("copy helper rejects an empty editor without claiming success", async () => {
  let attempted = false;
  const copied = await ui.copyCurrentTranscript("  \n", {
    writeText: async () => { attempted = true; },
  }, () => { attempted = true; return true; });
  assert.equal(copied, false);
  assert.equal(attempted, false);
});

const dataKey = (name) => name.slice(5).replace(/-([a-z])/g, (_, letter) => letter.toUpperCase());

class FakeElement {
  constructor(tagName, ownerDocument) {
    this.tagName = tagName.toUpperCase();
    this.ownerDocument = ownerDocument;
    this.parentNode = null;
    this.children = [];
    this.dataset = {};
    this.attributes = new Map();
    this.listeners = new Map();
    this.className = "";
    this.style = {};
    this.textContent = "";
    this.innerHTML = "";
    this.value = "";
    this.hidden = false;
  }

  setAttribute(name, value) {
    const text = String(value);
    this.attributes.set(name, text);
    if (name.startsWith("data-")) this.dataset[dataKey(name)] = text;
  }

  getAttribute(name) {
    if (name.startsWith("data-")) return this.dataset[dataKey(name)] ?? null;
    return this.attributes.get(name) ?? null;
  }

  append(...children) {
    children.forEach((child) => {
      child.parentNode = this;
      this.children.push(child);
    });
  }

  replaceChildren(...children) {
    this.children = [];
    this.append(...children);
  }

  remove() {
    if (this.parentNode) this.parentNode.children = this.parentNode.children.filter((child) => child !== this);
  }

  addEventListener(type, handler) {
    const handlers = this.listeners.get(type) || [];
    handlers.push(handler);
    this.listeners.set(type, handlers);
  }

  async emit(type) {
    await Promise.all((this.listeners.get(type) || []).map((handler) => handler({ type, target: this })));
  }

  focus() {}
  select() {}

  matches(selector) {
    const match = selector.match(/^\[([^\]]+)\]$/);
    return Boolean(match && this.getAttribute(match[1]) !== null);
  }

  querySelectorAll(selector) {
    const found = [];
    const visit = (node) => node.children.forEach((child) => {
      if (child.matches(selector)) found.push(child);
      visit(child);
    });
    visit(this);
    return found;
  }

  querySelector(selector) {
    return this.querySelectorAll(selector)[0] || null;
  }
}

class FakeDocument {
  constructor() {
    this.root = new FakeElement("main", this);
    this.body = this.root;
    this.copyResult = false;
  }

  createElement(tagName) { return new FakeElement(tagName, this); }
  querySelectorAll(selector) {
    const found = this.root.querySelectorAll(selector);
    if (this.root.matches(selector)) found.unshift(this.root);
    return found;
  }
  querySelector(selector) { return this.querySelectorAll(selector)[0] || null; }
  execCommand(command) { return command === "copy" && this.copyResult; }
}

function buildEditor(document, { text = "当前全文", uncertainties = [] } = {}) {
  const editor = document.createElement("section");
  editor.setAttribute("data-accurate-transcript-editor", "");
  editor.dataset.saveUrl = "/content/extractions/30/accurate-transcript";
  editor.dataset.csrf = "csrf";
  const nodes = {};
  for (const [name, tag] of [
    ["input", "textarea"], ["preview", "div"], ["list", "div"], ["status", "div"],
    ["source", "script"], ["toggle", "button"], ["copy", "button"], ["save", "button"],
  ]) nodes[name] = document.createElement(tag);
  nodes.input.setAttribute("data-transcript-input", "");
  nodes.input.value = text;
  nodes.preview.setAttribute("data-transcript-highlights", "");
  nodes.list.setAttribute("data-uncertainty-list", "");
  nodes.status.setAttribute("data-transcript-status", "");
  nodes.source.setAttribute("data-transcript-uncertainties", "");
  nodes.source.textContent = JSON.stringify(uncertainties);
  nodes.toggle.setAttribute("data-uncertainty-toggle", "");
  nodes.copy.setAttribute("data-transcript-copy", "");
  nodes.save.setAttribute("data-transcript-save", "");
  editor.append(...Object.values(nodes));
  return { editor, ...nodes };
}

function loadInteractiveUi(document, clipboard = null, fetchImpl = async () => { throw new Error("unexpected fetch"); }) {
  const timers = [];
  const interactiveWindow = { setTimeout: (callback, delay) => { timers.push({ callback, delay }); return timers.length; } };
  vm.runInNewContext(helperSource, {
    window: interactiveWindow, document, navigator: { clipboard },
    fetch: fetchImpl, URLSearchParams,
  });
  return { ui: interactiveWindow.AccurateTranscriptCRMUI, timers };
}

test("poll replacement editor is rebound and repeat initialization does not duplicate handlers", async () => {
  const document = new FakeDocument();
  const first = buildEditor(document, { uncertainties: [{ review_id: "r1", start_offset: 0, end_offset: 2, current_text: "当前" }] });
  const canonical = document.createElement("script");
  canonical.setAttribute("data-canonical-transcript", "");
  canonical.textContent = "当前全文";
  const rawProvider = document.createElement("pre");
  rawProvider.setAttribute("data-raw-provider", "");
  rawProvider.textContent = "RAW OCR / RAW ASR";
  document.root.append(first.editor);
  document.root.append(canonical, rawProvider);
  const env = loadInteractiveUi(document);
  assert.equal(first.toggle.listeners.get("click").length, 1);

  const replacement = buildEditor(document, { uncertainties: [{ review_id: "r1", start_offset: 0, end_offset: 2, current_text: "当前" }] });
  document.root.replaceChildren(replacement.editor);
  env.ui.initAccurateTranscriptEditors(replacement.editor);
  env.ui.initAccurateTranscriptEditors(replacement.editor);
  assert.equal(replacement.toggle.listeners.get("click").length, 1);

  await replacement.toggle.emit("click");
  assert.equal(replacement.preview.hidden, false);
  assert.match(replacement.preview.innerHTML, /<mark/);
  assert.equal(replacement.input.value, "当前全文");
  assert.equal(canonical.textContent, "当前全文");
  assert.equal(rawProvider.textContent, "RAW OCR / RAW ASR");
  await replacement.toggle.emit("click");
  assert.equal(replacement.preview.hidden, true);
  assert.equal(replacement.preview.hidden ? 0 : (replacement.preview.innerHTML.match(/<mark/g) || []).length, 0);
  await replacement.toggle.emit("click");
  assert.equal(replacement.preview.hidden ? 0 : (replacement.preview.innerHTML.match(/<mark/g) || []).length, 1);
  assert.equal(replacement.input.value, "当前全文");
  assert.equal(canonical.textContent, "当前全文");
  assert.equal(rawProvider.textContent, "RAW OCR / RAW ASR");
});

test("copy interaction copies the unedited editor value through Clipboard API", async () => {
  const document = new FakeDocument();
  const editor = buildEditor(document, { text: "未编辑完整全文" });
  document.root.append(editor.editor);
  const copied = [];
  loadInteractiveUi(document, { writeText: async (value) => copied.push(value) });
  await editor.copy.emit("click");
  assert.deepEqual(copied, ["未编辑完整全文"]);
  assert.equal(editor.status.textContent, "已复制当前文案");
});

test("copy interaction uses edited value and emits approved transient success feedback", async () => {
  const document = new FakeDocument();
  const editor = buildEditor(document, { text: "初始全文" });
  document.root.append(editor.editor);
  const copied = [];
  const env = loadInteractiveUi(document, { writeText: async (value) => copied.push(value) });
  editor.input.value = "编辑后的完整全文";

  await editor.copy.emit("click");
  assert.deepEqual(copied, ["编辑后的完整全文"]);
  assert.equal(editor.status.textContent, "已复制当前文案");
  assert.equal(env.timers.length, 1);
  assert.ok(env.timers[0].delay >= 1000 && env.timers[0].delay <= 5000);
  env.timers[0].callback();
  assert.equal(editor.status.textContent, "");
});

test("copy interaction reports exact empty and total-failure messages", async () => {
  const document = new FakeDocument();
  const editor = buildEditor(document, { text: "  \n" });
  document.root.append(editor.editor);
  loadInteractiveUi(document, { writeText: async () => { throw new Error("denied"); } });

  await editor.copy.emit("click");
  assert.equal(editor.status.textContent, "当前没有可复制的文案");
  editor.input.value = "无法复制的全文";
  document.copyResult = false;
  await editor.copy.emit("click");
  assert.equal(editor.status.textContent, "复制失败，请手动复制");
});

test("copy interaction uses and cleans the textarea fallback when Clipboard API is unavailable", async () => {
  const document = new FakeDocument();
  document.copyResult = true;
  const editor = buildEditor(document, { text: "Fallback 全文" });
  document.root.append(editor.editor);
  const childCount = document.root.children.length;
  loadInteractiveUi(document, null);
  await editor.copy.emit("click");
  assert.equal(editor.status.textContent, "已复制当前文案");
  assert.equal(document.root.children.length, childCount);
});

test("save HTML failure preserves editor and raw provider values and shows only safe copy", async () => {
  const document = new FakeDocument();
  const editor = buildEditor(document, { text: "编辑后的完整全文", uncertainties: [{ review_id: "r1" }] });
  const rawProvider = document.createElement("pre");
  rawProvider.setAttribute("data-raw-provider", "");
  rawProvider.textContent = "RAW OCR / RAW ASR PRESERVED";
  document.root.append(editor.editor, rawProvider);
  loadInteractiveUi(document, null, async () => responseStub({
    status: 200,
    contentType: "text/html; charset=utf-8",
    redirected: true,
    url: "/login",
  }));

  await editor.save.emit("click");

  assert.equal(editor.input.value, "编辑后的完整全文");
  assert.equal(rawProvider.textContent, "RAW OCR / RAW ASR PRESERVED");
  assert.equal(editor.status.textContent, "操作失败，请刷新页面后重试。");
  assert.equal(editor.status.textContent.includes("<!DOCTYPE"), false);
});
