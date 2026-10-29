import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import vm from "node:vm";
import { fileURLToPath } from "node:url";
import { dirname, resolve } from "node:path";

const here = dirname(fileURLToPath(import.meta.url));
const appSource = readFileSync(resolve(here, "../../assets/app.js"), "utf8");
const initializer = appSource
  .split("  const initContractStudentSelector = () => {", 2)[1]
  .split("  initContractStudentSelector();", 1)[0];

class FakeElement {
  constructor() {
    this.attributes = new Map();
    this.dataset = {};
    this.listeners = new Map();
    this.queries = new Map();
    this.hidden = false;
    this.disabled = false;
    this.textContent = "";
    this.id = "";
    this.clicked = 0;
  }

  setAttribute(name, value) {
    this.attributes.set(name, String(value));
  }

  getAttribute(name) {
    return this.attributes.get(name) ?? null;
  }

  removeAttribute(name) {
    this.attributes.delete(name);
  }

  addEventListener(type, handler) {
    const handlers = this.listeners.get(type) || [];
    handlers.push(handler);
    this.listeners.set(type, handlers);
  }

  dispatch(type, properties = {}) {
    const event = {
      type,
      defaultPrevented: false,
      preventDefault() { this.defaultPrevented = true; },
      ...properties,
    };
    for (const handler of this.listeners.get(type) || []) handler(event);
    return event;
  }

  querySelector(selector) {
    return this.queries.get(selector)?.[0] || null;
  }

  querySelectorAll(selector) {
    return this.queries.get(selector) || [];
  }

  click() {
    this.clicked += 1;
  }

  scrollIntoView() {}
}

const fixture = () => {
  const document = new FakeElement();
  const search = new FakeElement();
  const input = new FakeElement();
  const results = new FakeElement();
  const first = new FakeElement();
  const second = new FakeElement();
  const confirmation = new FakeElement();
  const form = new FakeElement();
  const submit = new FakeElement();

  input.setAttribute("aria-expanded", "true");
  first.id = "student-option-1";
  second.id = "student-option-2";
  search.queries.set('input[name="q"]', [input]);
  search.queries.set("[data-contract-student-results]", [results]);
  results.queries.set("[data-contract-student-option]", [first, second]);
  confirmation.queries.set("form", [form]);
  form.queries.set("[data-contract-student-submit]", [submit]);
  document.queries.set("[data-contract-student-search]", [search]);
  document.queries.set("[data-contract-student-confirmation] form", [form]);

  const script = `(function () {
    const q = (selector, root = document) => root.querySelector(selector);
    const qa = (selector, root = document) => Array.from(root.querySelectorAll(selector));
    const initContractStudentSelector = () => {${initializer}
    initContractStudentSelector();
  })();`;
  vm.runInNewContext(script, { document });
  return { input, results, first, second, form, submit };
};

test("keyboard selection follows the active existing-student link", () => {
  const { input, results, first, second } = fixture();

  assert.equal(input.dispatch("keydown", { key: "ArrowDown" }).defaultPrevented, true);
  assert.equal(first.getAttribute("aria-selected"), "true");
  assert.equal(input.getAttribute("aria-activedescendant"), first.id);
  input.dispatch("keydown", { key: "ArrowDown" });
  assert.equal(second.getAttribute("aria-selected"), "true");
  assert.equal(input.getAttribute("aria-activedescendant"), second.id);
  assert.equal(input.dispatch("keydown", { key: "Enter" }).defaultPrevented, true);
  assert.equal(second.clicked, 1);

  assert.equal(input.dispatch("keydown", { key: "Escape" }).defaultPrevented, true);
  assert.equal(results.hidden, true);
  assert.equal(input.getAttribute("aria-activedescendant"), null);
});

test("a repeated confirmation submit is cancelled after the first lock", () => {
  const { form, submit } = fixture();

  assert.equal(form.dispatch("submit").defaultPrevented, false);
  assert.equal(form.dataset.contractStudentSubmitting, "true");
  assert.equal(form.getAttribute("aria-busy"), "true");
  assert.equal(submit.disabled, true);
  assert.equal(submit.textContent, "正在进入合同流程…");
  assert.equal(form.dispatch("submit").defaultPrevented, true);
});
