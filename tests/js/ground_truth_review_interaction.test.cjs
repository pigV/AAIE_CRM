const assert = require("node:assert/strict");
const { readFileSync } = require("node:fs");
const test = require("node:test");
const vm = require("node:vm");
const { resolve } = require("node:path");

const source = readFileSync(resolve(
  __dirname,
  "../../content_extraction/accuracy/review/static/app.js",
), "utf8");
const window = {};
vm.runInNewContext(source, { window });
const ui = window.GroundTruthReviewUI;

test("second pass view has no machine evidence or first-pass transcript", () => {
  const view = ui.buildViewModel({
    sample_id: "sample-1",
    review_stage: "SECOND_PASS",
    verification_status: "PENDING_SECOND_PASS",
    source: { start_ms: 1000, end_ms: 2500 },
    clip_url: "/media/sample-1.m4v",
  });

  assert.equal(view.heading, "第二遍：独立重新听写");
  assert.equal(view.showEvidence, false);
  assert.equal(view.showPasses, false);
  assert.equal(view.initialText, "");
});

test("reconciliation reveals both independent passes only at that stage", () => {
  const view = ui.buildViewModel({
    sample_id: "sample-1",
    review_stage: "RECONCILIATION",
    first_pass_text: "第一遍",
    second_pass_text: "第二遍",
    provider_texts: { funasr: "机器稿" },
    source: { start_ms: 0, end_ms: 1000 },
  });

  assert.equal(view.showEvidence, true);
  assert.equal(view.showPasses, true);
  assert.equal(view.firstPassText, "第一遍");
  assert.equal(view.secondPassText, "第二遍");
  assert.equal(view.initialText, "");
});

test("review request contains only the three allowlisted fields", () => {
  assert.deepEqual(
    JSON.parse(JSON.stringify(ui.reviewPayload("FIRST_PASS", "CONFIRM", "人工听写"))),
    { stage: "FIRST_PASS", action: "CONFIRM", text: "人工听写" },
  );
});

test("edit action never copies provider text into the editor", () => {
  const editor = { value: "", focused: false, focus() { this.focused = true; } };
  ui.focusEditor(editor, { provider_texts: { funasr: "不得预填" } });
  assert.equal(editor.value, "");
  assert.equal(editor.focused, true);
});
