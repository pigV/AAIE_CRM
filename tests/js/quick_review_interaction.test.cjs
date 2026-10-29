const assert = require("node:assert/strict");
const { readFileSync } = require("node:fs");
const test = require("node:test");
const vm = require("node:vm");
const { resolve } = require("node:path");

const root = resolve(__dirname, "../../content_extraction/accuracy/quick_review/static");
const source = readFileSync(resolve(root, "app.js"), "utf8");
const html = readFileSync(resolve(root, "index.html"), "utf8");
const window = {};
vm.runInNewContext(source, { window });
const ui = window.QuickReviewUI;

test("candidate click creates bounded one-click resolution payload", () => {
  const payload = ui.resolutionPayload({
    queueHash: "sha256:q", revision: 2, action: "CONFIRMED", selectedText: "590",
    candidateReferences: ["alternative:590"],
  });
  assert.deepEqual(JSON.parse(JSON.stringify(payload)), {
    source_queue_hash: "sha256:q", expected_revision: 2,
    action: "CONFIRMED", selected_text: "590",
    candidate_references: ["alternative:590"], reviewer_source: "quick_review_ui",
  });
});

test("suspect span is highlighted without rewriting context", () => {
  const parts = ui.splitCarrier("学生签证是500类别", 5, 8);
  assert.deepEqual(JSON.parse(JSON.stringify(parts)), {
    prefix: "学生签证是", suspect: "500", suffix: "类别",
  });
});

test("primary UI uses plain language and keeps technical evidence collapsed", () => {
  assert.match(html, /查看依据/);
  assert.match(html, /听不清/);
  assert.match(html, /都不对，自己输入/);
  const primary = html.split('<details id="evidence-panel"')[0];
  for (const term of ["OCR", "ASR", "Fusion", "Provider"]) {
    assert.equal(primary.includes(term), false);
  }
});
