import assert from "node:assert/strict";
import { existsSync, readFileSync } from "node:fs";
import test from "node:test";
import vm from "node:vm";
import { fileURLToPath } from "node:url";
import { dirname, resolve } from "node:path";

const here = dirname(fileURLToPath(import.meta.url));
const assetPath = resolve(here, "../../assets/school-plan-report-workspace.js");
const loadSource = () => {
  assert.ok(existsSync(assetPath), "School Plan Report workspace JavaScript module is missing");
  return readFileSync(assetPath, "utf8");
};

const dataKey = (name) => name
  .slice(5)
  .replace(/-([a-z])/g, (_, letter) => letter.toUpperCase());

class FakeElement {
  constructor(tagName, document) {
    this.tagName = tagName.toUpperCase();
    this.ownerDocument = document;
    this.parentNode = null;
    this.children = [];
    this.dataset = {};
    this.attributes = new Map();
    this.listeners = new Map();
    this.className = "";
    this._textContent = "";
    this.value = "";
    this.disabled = false;
    this.selected = false;
    this.readOnly = false;
  }

  set textContent(value) {
    this._textContent = String(value ?? "");
    this.children = [];
  }

  get textContent() {
    return this.children.length
      ? this.children.map((child) => child.textContent).join("")
      : this._textContent;
  }

  setAttribute(name, value) {
    const text = String(value);
    this.attributes.set(name, text);
    if (name === "class") this.className = text;
    if (name.startsWith("data-")) this.dataset[dataKey(name)] = text;
  }

  getAttribute(name) {
    if (name === "class") return this.className || null;
    if (name.startsWith("data-")) {
      const value = this.dataset[dataKey(name)];
      return value === undefined ? null : value;
    }
    return this.attributes.get(name) ?? null;
  }

  append(...children) {
    children.flat().forEach((child) => this.appendChild(child));
  }

  appendChild(child) {
    if (typeof child === "string") {
      const text = new FakeElement("#text", this.ownerDocument);
      text.textContent = child;
      child = text;
    }
    child.parentNode = this;
    this.children.push(child);
    return child;
  }

  replaceChildren(...children) {
    this.children.forEach((child) => { child.parentNode = null; });
    this.children = [];
    this._textContent = "";
    this.append(...children);
  }

  remove() {
    if (!this.parentNode) return;
    this.parentNode.children = this.parentNode.children.filter((child) => child !== this);
    this.parentNode = null;
  }

  addEventListener(type, handler) {
    const handlers = this.listeners.get(type) || [];
    handlers.push(handler);
    this.listeners.set(type, handlers);
  }

  emit(type, target = this) {
    const event = {
      type,
      target,
      preventDefault() {},
    };
    (this.listeners.get(type) || []).forEach((handler) => handler(event));
  }

  focus() {
    this.ownerDocument.activeElement = this;
  }

  select() {
    this.wasSelected = true;
  }

  matches(selector) {
    const parts = selector.match(/(?:\[[^\]]+\]|\.[\w-]+|#[\w-]+|^[\w-]+)/g) || [];
    return parts.every((part) => {
      if (part.startsWith(".")) return this.className.split(/\s+/).includes(part.slice(1));
      if (part.startsWith("#")) return this.getAttribute("id") === part.slice(1);
      if (part.startsWith("[")) {
        const match = part.match(/^\[([^=\]]+)(?:=["']?([^"'\]]+)["']?)?\]$/);
        if (!match) return false;
        const actual = this.getAttribute(match[1]);
        return match[2] === undefined ? actual !== null : actual === match[2];
      }
      return this.tagName.toLowerCase() === part.toLowerCase();
    });
  }

  querySelectorAll(selector) {
    const matches = [];
    const visit = (node) => {
      node.children.forEach((child) => {
        if (child.matches(selector)) matches.push(child);
        visit(child);
      });
    };
    visit(this);
    return matches;
  }

  querySelector(selector) {
    return this.querySelectorAll(selector)[0] || null;
  }

  closest(selector) {
    let node = this;
    while (node) {
      if (node.matches(selector)) return node;
      node = node.parentNode;
    }
    return null;
  }
}

class FakeDocument {
  constructor() {
    this.activeElement = null;
    this.root = null;
  }

  createElement(tagName) {
    return new FakeElement(tagName, this);
  }

  querySelector(selector) {
    if (this.root?.matches(selector)) return this.root;
    return this.root?.querySelector(selector) || null;
  }
}

const report = (overrides = {}) => ({
  report_id: 21,
  version: 2,
  sector: "public",
  city_name: "Melbourne <unsafe>",
  is_current: true,
  status: "active",
  parent_status: "opened",
  share_url: "https://example.test/parent?<unsafe>",
  can_copy_link: true,
  viewed_count: 3,
  last_viewed_display: "07/08/2026 10:00",
  confirmed_display: "",
  school_count: 4,
  file_name: "plan.pdf",
  has_pdf: true,
  detail_url: "/school-plan-reports/21",
  download_url: "/school-plan-reports/21/download",
  created_by_name: "Advisor",
  created_display: "07/08/2026 09:00",
  ...overrides,
});

const workspace = (overrides = {}) => {
  const current = report();
  return {
    search: "",
    status_filter: "needs_followup",
    filters: [
      { key: "needs_followup", label: "需要跟进", count: 1 },
      { key: "all", label: "全部", count: 2 },
    ],
    groups: [{
      group_key: "S001::A001",
      student_id: "S001",
      application_id: "A001",
      student_name: "Jia <unsafe>",
      summary_report_id: 21,
      summary_activity_display: "07/08/2026 10:00",
    }],
    page: 1,
    page_count: 2,
    total_groups: 21,
    selected_report_id: 21,
    selected: {
      group_key: "S001::A001",
      student_id: "S001",
      application_id: "A001",
      student_name: "Jia <unsafe>",
      create_url: "/school-plan-reports/create?student_id=S001",
      current_plans: [current, report({ report_id: 22, sector: "private", city_name: "Sydney" })],
      records: [current, report({ report_id: 20, version: 1, is_current: false })],
      selected_report: current,
    },
    ...overrides,
  };
};

const settle = async () => {
  await new Promise((resolveTask) => setImmediate(resolveTask));
  await Promise.resolve();
};

function fixture(source, initialWorkspace = workspace()) {
  const document = new FakeDocument();
  const root = document.createElement("section");
  root.setAttribute("data-school-plan-report-workspace", "");
  root.dataset.endpoint = "/school-plan-reports/workspace-data";
  document.root = root;

  const initialSource = document.createElement("script");
  initialSource.setAttribute("data-school-plan-report-workspace-source", "");
  initialSource.textContent = JSON.stringify(initialWorkspace);
  const body = document.createElement("div");
  body.className = "school-plan-workspace__body";
  const master = document.createElement("aside");
  master.className = "school-plan-workspace__master";
  const detail = document.createElement("section");
  detail.setAttribute("data-school-plan-detail-workspace", "");
  body.append(master, detail);
  const announcer = document.createElement("div");
  announcer.setAttribute("data-workspace-announcer", "");
  root.append(initialSource, body, announcer);

  const timers = [];
  const fetchCalls = [];
  const responseQueue = [];
  const historyCalls = [];
  const copied = [];
  let copyError = null;
  const setTimer = (handler, milliseconds) => {
    timers.push({ handler, milliseconds, cleared: false });
    return timers.length - 1;
  };
  const clearTimer = (id) => {
    if (timers[id]) timers[id].cleared = true;
  };
  const fetch = (url, options) => {
    fetchCalls.push({ url, options });
    const next = responseQueue.shift();
    if (next instanceof Error) return Promise.reject(next);
    const payload = next || { ok: true, workspace: workspace() };
    return Promise.resolve({
      ok: payload.httpOk !== false,
      status: payload.status || 200,
      json: async () => payload,
    });
  };
  const window = {
    setTimeout: setTimer,
    clearTimeout: clearTimer,
  };
  const context = {
    document,
    fetch,
    history: { replaceState: (...args) => historyCalls.push(args) },
    location: { pathname: "/school-plan-reports" },
    navigator: {
      clipboard: {
        writeText: async (value) => {
          if (copyError) throw copyError;
          copied.push(value);
        },
      },
    },
    window,
    URLSearchParams,
    AbortController,
    setTimeout: setTimer,
    clearTimeout: clearTimer,
    console,
  };
  window.document = document;
  vm.runInNewContext(source, context, { filename: assetPath });

  return {
    document,
    root,
    timers,
    fetchCalls,
    responseQueue,
    historyCalls,
    copied,
    setCopyError(error) { copyError = error; },
    runTimer(milliseconds) {
      const timer = timers.find((item) => item.milliseconds === milliseconds && !item.cleared);
      assert.ok(timer, `Missing active ${milliseconds}ms timer`);
      timer.cleared = true;
      timer.handler();
    },
  };
}

test("workspace module keeps its security and ownership contracts", () => {
  const source = loadSource();
  assert.match(source, /\[data-school-plan-report-workspace\]/);
  assert.match(source, /AbortController/);
  assert.match(source, /history\.replaceState/);
  assert.match(source, /navigator\.clipboard\.writeText/);
  assert.match(source, /data-workspace-retry/);
  assert.match(source, /aria-selected/);
  assert.doesNotMatch(source, /innerHTML\s*=\s*[^`'\"]/);
});

test("workspace renders safe master-detail controls and pagination from initial JSON", () => {
  const source = loadSource();
  const env = fixture(source);

  assert.equal(env.root.querySelector("[data-workspace-search]").value, "");
  assert.equal(env.root.querySelector("[data-workspace-group]").textContent.includes("Jia <unsafe>"), true);
  assert.equal(env.root.querySelector("[data-workspace-group]").getAttribute("aria-selected"), "true");
  assert.equal(env.root.querySelectorAll("[data-workspace-page]").length, 2);
  assert.equal(env.root.querySelector("[data-workspace-plan]").value, "21");
  assert.equal(env.root.querySelector("[data-workspace-copy]").dataset.workspaceCopy, "https://example.test/parent?<unsafe>");
});

test("workspace loads search, filter, pagination, group, plan and retry changes authoritatively", async () => {
  const source = loadSource();
  const env = fixture(source);

  const search = env.root.querySelector("[data-workspace-search]");
  search.value = "Jia & Yu";
  env.root.emit("input", search);
  env.responseQueue.push({ ok: true, workspace: workspace({ search: "Jia & Yu" }) });
  env.runTimer(250);
  await settle();
  assert.match(env.fetchCalls.at(-1).url, /q=Jia%20%26%20Yu|q=Jia\+%26\+Yu/);
  assert.match(env.fetchCalls.at(-1).url, /page=1/);
  assert.equal(env.document.activeElement?.matches("[data-workspace-search]"), true);

  const filter = env.root.querySelector('[data-workspace-filter="all"]');
  env.responseQueue.push({ ok: true, workspace: workspace({ status_filter: "all" }) });
  env.root.emit("click", filter);
  await settle();
  assert.match(env.fetchCalls.at(-1).url, /status=all/);
  assert.match(env.fetchCalls.at(-1).url, /page=1/);

  const next = env.root.querySelector('[data-workspace-page="2"]');
  env.responseQueue.push({ ok: true, workspace: workspace({ page: 2 }) });
  env.root.emit("click", next);
  await settle();
  assert.match(env.fetchCalls.at(-1).url, /page=2/);

  const group = env.root.querySelector("[data-workspace-group]");
  env.responseQueue.push({ ok: true, workspace: workspace() });
  env.root.emit("click", group);
  await settle();
  assert.match(env.fetchCalls.at(-1).url, /report=21/);
  assert.equal(env.root.dataset.mobileView, "detail");
  assert.equal(env.document.activeElement?.getAttribute("aria-selected"), "true");

  const plan = env.root.querySelector("[data-workspace-plan]");
  plan.value = "22";
  env.responseQueue.push({ ok: true, workspace: workspace({ selected_report_id: 22 }) });
  env.root.emit("change", plan);
  await settle();
  assert.match(env.fetchCalls.at(-1).url, /report=22/);
  assert.equal(env.historyCalls.length >= 5, true);
  assert.equal(env.fetchCalls.at(-2).options.signal.aborted, true);

  env.responseQueue.push(new Error("offline"));
  env.root.emit("click", env.root.querySelector('[data-workspace-filter="all"]'));
  await settle();
  assert.ok(env.root.querySelector("[data-workspace-group]"), "read failure must preserve the master list");
  const retry = env.root.querySelector("[data-workspace-retry]");
  assert.ok(retry, "read failure must expose a local retry control");
  env.responseQueue.push({ ok: true, workspace: workspace() });
  env.root.emit("click", retry);
  await settle();
  assert.equal(env.root.querySelector("[data-workspace-retry]"), null);
});

test("workspace restores focus to the initiating filter after a successful rebuild", async () => {
  const source = loadSource();
  const env = fixture(source);
  const filter = env.root.querySelector('[data-workspace-filter="all"]');

  env.responseQueue.push({ ok: true, workspace: workspace({ status_filter: "all" }) });
  env.root.emit("click", filter);
  await settle();

  assert.equal(env.document.activeElement?.dataset.workspaceFilter, "all");
});

test("workspace restores pagination focus to an enabled stable pager after rebuild", async () => {
  const source = loadSource();
  const env = fixture(source);
  const next = env.root.querySelector('[data-workspace-page="2"]');

  env.responseQueue.push({ ok: true, workspace: workspace({ page: 2 }) });
  env.root.emit("click", next);
  await settle();

  assert.equal(env.document.activeElement?.dataset.workspacePageDirection, "previous");
});

test("workspace copy feedback, safe fallback and mobile back behavior are local", async () => {
  const source = loadSource();
  const initial = workspace();
  const selectedGroup = {
    ...initial.groups[0],
    group_key: "S002::A002",
    student_id: "S002",
    application_id: "A002",
    student_name: "Selected Student",
    summary_report_id: 99,
  };
  const env = fixture(source, {
    ...initial,
    groups: [initial.groups[0], selectedGroup],
    selected: { ...initial.selected, group_key: selectedGroup.group_key },
  });

  const copy = env.root.querySelector("[data-workspace-copy]");
  env.root.emit("click", copy);
  await settle();
  assert.deepEqual(env.copied, ["https://example.test/parent?<unsafe>"]);
  assert.equal(copy.textContent, "已复制");
  assert.match(env.root.querySelector("[data-workspace-announcer]").textContent, /家长链接已复制/);
  env.runTimer(1600);
  assert.equal(copy.textContent, "复制家长链接");

  env.setCopyError(new Error("clipboard denied"));
  const secondCopy = env.root.querySelector("[data-workspace-copy]");
  env.root.emit("click", secondCopy);
  await settle();
  const fallback = env.root.querySelector("[data-workspace-copy-fallback]");
  const input = fallback?.querySelector("input");
  assert.ok(input);
  assert.equal(input.value, "https://example.test/parent?<unsafe>");
  assert.equal(input.readOnly, true);
  assert.equal(input.wasSelected, true);

  env.root.dataset.mobileView = "detail";
  env.root.emit("click", env.root.querySelector("[data-workspace-back]"));
  assert.equal(env.root.dataset.mobileView, "list");
  assert.equal(
    env.document.activeElement?.dataset.reportId,
    "99",
    "mobile back must focus the selected master option instead of the first option",
  );
});
