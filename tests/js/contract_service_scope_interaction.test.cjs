const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");

const source = fs.readFileSync("assets/app.js", "utf8");
assert.match(source, /function initContractTypeForms/);
assert.match(source, /data-contract-type-radio/);
assert.match(source, /data-contract-service-types/);
assert.match(source, /data-contract-signer-source/);
assert.match(source, /data-contract-refund-types/);
assert.match(source, /data-contract-parent-phone/);
assert.match(source, /data-contract-parent-email/);
assert.match(source, /field\.dataset\[key\]/);
const endMarker = '  document.addEventListener("click", (event) => {\n    const choice =';
const end = source.indexOf(endMarker);
assert.notEqual(end, -1, "service scope card click handler marker is absent");

const delegatedStart = source.lastIndexOf('  document.addEventListener("click", (event) => {', end - 1);
const directStart = source.lastIndexOf('  qa("[data-scope-select]")', end - 1);
const start = Math.max(delegatedStart, directStart);
assert.notEqual(start, -1, "service scope bulk control handler is absent");

class FakeDocument {
  constructor() {
    this.listeners = new Map();
  }

  addEventListener(type, handler) {
    const handlers = this.listeners.get(type) || [];
    handlers.push(handler);
    this.listeners.set(type, handlers);
  }

  querySelectorAll() {
    return [];
  }

  click(target) {
    for (const handler of this.listeners.get("click") || []) {
      handler({ target });
    }
  }
}

const document = new FakeDocument();
const qa = (selector, root = document) => Array.from(root.querySelectorAll(selector));
vm.runInNewContext(source.slice(start, end), { document, qa });

const inputs = [{ checked: true }, { checked: false }, { checked: true, disabled: true }];
const form = {
  querySelectorAll(selector) {
    return selector === 'input[name="service_scope"]' ? inputs : [];
  },
};
const button = (action) => ({
  dataset: { scopeSelect: action },
  closest(selector) {
    if (selector === "[data-scope-select]") return this;
    if (selector === "form") return form;
    return null;
  },
});

document.click(button("none"));
assert.deepEqual(inputs.map((input) => input.checked), [false, false, true]);

document.click(button("all"));
assert.deepEqual(inputs.map((input) => input.checked), [true, true, true]);

console.log("contract service scope lazy bulk controls: ok");
