const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const { test } = require("node:test");

const source = fs.readFileSync(
  path.resolve(__dirname, "../../assets/student-lifecycle-phase6.js"),
  "utf8"
);

function loadListeners() {
  const listeners = new Map();
  const document = {
    addEventListener(name, handler) {
      listeners.set(name, handler);
    },
  };
  vm.runInNewContext(source, { document });
  return listeners;
}

test("business handling change reveals only the relevant follow-up field", () => {
  const listeners = loadListeners();
  assert.equal(typeof listeners.get("change"), "function");

  const existing = { hidden: true };
  const reason = { hidden: true };
  const form = {
    querySelector(selector) {
      return selector === "[data-existing-choice]" ? existing : reason;
    },
  };
  const select = {
    value: "skip",
    matches(selector) {
      return selector === "[data-handling-select]";
    },
    closest(selector) {
      return selector === "[data-lifecycle-business-disposition]" ? form : null;
    },
  };

  listeners.get("change")({ target: select });
  assert.equal(existing.hidden, true);
  assert.equal(reason.hidden, false);

  select.value = "reuse";
  listeners.get("change")({ target: select });
  assert.equal(existing.hidden, false);
  assert.equal(reason.hidden, true);

  select.value = "new";
  listeners.get("change")({ target: select });
  assert.equal(existing.hidden, true);
  assert.equal(reason.hidden, true);
});
