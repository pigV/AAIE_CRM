const assert = require("node:assert/strict");
const fs = require("node:fs");
const test = require("node:test");
const vm = require("node:vm");

const source = fs.readFileSync("assets/app.js", "utf8");
const editorSource = fs.readFileSync("contract_template_editor.py", "utf8");
const styleSource = fs.readFileSync("assets/style.css", "utf8");
const startMarker = "function createContractTemplateSaveCoordinator";
const endMarker = "function initContractTemplateEditorV1";
const start = source.indexOf(startMarker);
const end = source.indexOf(endMarker);
assert.notEqual(start, -1, "contract template keyed save coordinator is absent");
assert.notEqual(end, -1, "contract template editor initializer is absent");
assert.ok(end > start, "save coordinator must be defined before editor wiring");
const context = { Promise, Set, Map };
vm.createContext(context);
vm.runInContext(
  `${source.slice(start, end)}\nthis.createContractTemplateSaveCoordinator = createContractTemplateSaveCoordinator;`,
  context,
);
const createCoordinator = context.createContractTemplateSaveCoordinator;

test("editor wiring delegates block and settings autosaves to keyed coordinator", () => {
  const initializer = source.slice(end);
  assert.match(
    initializer,
    /saveCoordinator\.schedule\(`block:\$\{card\.dataset\.blockKey\}`/,
  );
  assert.match(initializer, /saveCoordinator\.schedule\("settings"/);
  assert.match(initializer, /runContractTemplateFormalAction\(\{/);
  assert.match(initializer, /createContractTemplateFormalActionController\(\{/);
  assert.match(initializer, /formalActionController\.run\(\{/);
  assert.match(initializer, /formalActionController\.runMutation\(/);
  assert.match(initializer, /formalActionController\.allowsLocalMutation\(\)/);
  assert.match(initializer, /key: `delete:\$\{card\.dataset\.blockKey\}`/);
  assert.equal(
    initializer.match(/formalActionController\.runMutation\(/g)?.length || 0,
    4,
    "only the four keyed async network mutations use runMutation",
  );
  assert.match(initializer, /deleteContractTemplateBlock\(\{ card, saveCoordinator, ensureLock, action \}\)/);
  assert.match(initializer, /if \(!saveCoordinator\.hasDirty\(\)\) return/);
  assert.match(initializer, /renderContractTemplateSaveStatus\(event\)/);
  assert.equal(
    initializer.match(/status\.textContent\s*=/g)?.length || 0,
    1,
    "all editor status writes must flow through the aggregate coordinator renderer",
  );
  assert.doesNotMatch(initializer, /let saveTimer\s*=/);
});

test("apply is the single manager publication action with clear success feedback", () => {
  const initializer = source.slice(end);
  assert.match(initializer, /name === "apply"/);
  assert.match(source, /模板已应用，新建合同将使用此版本。/);
  assert.match(initializer, /if \(name === "apply"\) window\.setTimeout\(\(\) => window\.location\.reload\(\), 1500\)/);
});

test("apply publication failures never overwrite the autosave status", () => {
  const initializer = source.slice(end);
  assert.match(editorSource, /data-template-action-message/);
  assert.match(
    initializer,
    /const actionStatus = editor\.querySelector\("\[data-template-action-message\]"\)/,
  );
  assert.match(
    initializer,
    /draftSaved: !saveCoordinator\.hasDirty\(\)/,
  );
  assert.match(
    initializer,
    /reportAction\(renderContractTemplateActionStatus/,
  );
  const formalHandler = initializer.slice(
    initializer.indexOf('editor.querySelectorAll("[data-template-action]")'),
    initializer.indexOf('editor.querySelectorAll("[data-template-republish-version]")'),
  );
  assert.doesNotMatch(
    formalHandler,
    /saveCoordinator\.report\("error"/,
    "formal Apply/Publish errors must not be rendered through the autosave channel",
  );

  const { renderContractTemplateActionStatus } = loadEditorHelpers();
  assert.equal(
    renderContractTemplateActionStatus("apply", {
      error: new Error("模板发布门禁未通过。"),
    }),
    "草稿已保存，但暂不能应用：模板发布门禁未通过。",
  );
  assert.equal(
    renderContractTemplateActionStatus("apply", {
      error: new Error("仍有未保存修改，正式操作已阻止"),
      draftSaved: false,
    }),
    "应用已阻止：仍有未保存修改，正式操作已阻止",
  );
});

test("autosave status is explicitly labelled and fixed inside every viewport", () => {
  assert.match(
    editorSource,
    /contract-template-save-state-label[^>]*>自动保存：<\/strong>/,
  );
  assert.match(editorSource, /data-template-save-message>已保存<\/span>/);
  assert.match(
    source.slice(end),
    /const status = editor\.querySelector\("\[data-template-save-message\]"\)/,
  );
  const statusRule = styleSource.match(/\.contract-template-save-state\s*\{([^}]+)\}/)?.[1] || "";
  assert.match(statusRule, /position:\s*fixed/);
  assert.match(statusRule, /left:\s*50%/);
  assert.match(statusRule, /bottom:/);
  assert.match(statusRule, /z-index:/);
  assert.match(statusRule, /pointer-events:\s*none/);
});

test("network autosave failure is explicit and actionable", () => {
  const { renderContractTemplateSaveStatus } = loadEditorHelpers();
  assert.equal(
    renderContractTemplateSaveStatus({
      type: "error",
      error: new Error("Failed to fetch"),
      hasDirty: true,
      failedKeys: [],
    }),
    "保存失败：无法连接服务器；仍有未保存修改，可继续编辑重试",
  );
});

test("genuine different-user conflict is clear and keeps unsaved edits actionable", () => {
  const { renderContractTemplateSaveStatus } = loadEditorHelpers();
  assert.equal(
    renderContractTemplateSaveStatus({
      type: "error",
      error: new Error("此模板正在被另一位用户编辑，你当前修改尚未应用。"),
      hasDirty: true,
      failedKeys: ["block:profile_4"],
    }),
    "此模板正在被另一位用户编辑，你当前修改尚未应用。仍有未保存修改，可继续编辑重试",
  );
});

function loadFormalActionController() {
  const helperStart = source.indexOf("const contractTemplateFormalActionFreezeStates");
  assert.notEqual(helperStart, -1, "editor-wide formal action freeze controller is absent");
  const helperContext = { Promise, Set, Map, WeakMap };
  vm.createContext(helperContext);
  vm.runInContext(
    `${source.slice(helperStart, end)}\n` +
      "this.setContractTemplateEditorFormalActionPending = setContractTemplateEditorFormalActionPending;" +
      "this.createContractTemplateFormalActionController = createContractTemplateFormalActionController;",
    helperContext,
  );
  return helperContext;
}

function loadEditorHelpers() {
  const helperStart = source.indexOf("function renderContractTemplateSaveStatus");
  assert.notEqual(helperStart, -1, "aggregate editor status renderer is absent");
  const helperContext = { Promise, Set, Map };
  vm.createContext(helperContext);
  vm.runInContext(
    `${source.slice(helperStart, end)}\n` +
      "this.renderContractTemplateSaveStatus = renderContractTemplateSaveStatus;" +
      "this.renderContractTemplateActionStatus = renderContractTemplateActionStatus;" +
      "this.deleteContractTemplateBlock = deleteContractTemplateBlock;",
    helperContext,
  );
  return helperContext;
}

function loadFormalActionHelper() {
  const helperStart = source.indexOf("async function runContractTemplateFormalAction");
  assert.notEqual(helperStart, -1, "formal actions do not share a save-flush gate");
  const helperContext = { Promise, Set, Map };
  vm.createContext(helperContext);
  vm.runInContext(
    `${source.slice(helperStart, end)}\n` +
      "this.runContractTemplateFormalAction = runContractTemplateFormalAction;",
    helperContext,
  );
  return helperContext.runContractTemplateFormalAction;
}

function loadLockRecoveryHelper() {
  const helperStart = source.indexOf("function isContractTemplateEditLockConflict");
  assert.notEqual(helperStart, -1, "lock-specific autosave recovery helper is absent");
  const helperContext = { Promise, Set, Map };
  vm.createContext(helperContext);
  vm.runInContext(
    `${source.slice(helperStart, end)}\n` +
      "this.runContractTemplateActionWithLockRecovery = runContractTemplateActionWithLockRecovery;",
    helperContext,
  );
  return helperContext.runContractTemplateActionWithLockRecovery;
}

test("expired or stale own lock is renewed once and the same autosave is retried", async () => {
  const runWithRecovery = loadLockRecoveryHelper();
  const calls = [];
  let attempts = 0;
  const result = await runWithRecovery({
    name: "autosave-block",
    fields: { block_key: "profile_3", body: "UNSAVED BODY" },
    action: async (name, fields) => {
      attempts += 1;
      calls.push(`${name}:${fields.body}`);
      if (attempts === 1) {
        const error = new Error("编辑会话已失效");
        error.status = 409;
        error.code = "edit_lock_conflict";
        throw error;
      }
      return { revision: 8 };
    },
    renewLock: async () => { calls.push("renew-lock"); },
  });

  assert.deepEqual(result, { revision: 8 });
  assert.deepEqual(calls, [
    "autosave-block:UNSAVED BODY", "renew-lock", "autosave-block:UNSAVED BODY",
  ]);
});

test("stale revision never renews or retries and keeps the coordinator dirty", async () => {
  const runWithRecovery = loadLockRecoveryHelper();
  const timers = fakeTimers();
  let renewals = 0;
  let attempts = 0;
  const coordinator = createCoordinator({
    setTimer: timers.setTimer,
    clearTimer: timers.clearTimer,
  });
  coordinator.schedule("block:profile_3", () => runWithRecovery({
    name: "autosave-block",
    fields: { body: "LOCAL UNSAVED BODY" },
    action: async () => {
      attempts += 1;
      const error = new Error("模板内容已在另一标签页更新");
      error.status = 409;
      error.code = "stale_revision";
      throw error;
    },
    renewLock: async () => { renewals += 1; },
  }));
  timers.fireActive();
  await coordinator.whenIdle();

  assert.equal(attempts, 1);
  assert.equal(renewals, 0);
  assert.equal(coordinator.isDirty("block:profile_3"), true);
  assert.match(coordinator.errorFor("block:profile_3").message, /另一标签页/);
});

test("different-user conflict fails after one recovery attempt without losing dirty input", async () => {
  const runWithRecovery = loadLockRecoveryHelper();
  const timers = fakeTimers();
  let attempts = 0;
  let renewals = 0;
  const coordinator = createCoordinator({
    setTimer: timers.setTimer,
    clearTimer: timers.clearTimer,
  });
  coordinator.schedule("block:profile_3", () => runWithRecovery({
    name: "autosave-block",
    fields: { body: "PRESERVE ME" },
    action: async () => {
      attempts += 1;
      const error = new Error("编辑会话已失效");
      error.status = 409;
      error.code = "edit_lock_conflict";
      throw error;
    },
    renewLock: async () => {
      renewals += 1;
      const error = new Error("此模板正在被另一位用户编辑，你当前修改尚未应用。");
      error.status = 409;
      error.code = "edit_lock_conflict";
      throw error;
    },
  }));
  timers.fireActive();
  await coordinator.whenIdle();

  assert.equal(attempts, 1, "a failed lock renewal never replays the write");
  assert.equal(renewals, 1);
  assert.equal(coordinator.isDirty("block:profile_3"), true);
  assert.equal(
    coordinator.errorFor("block:profile_3").message,
    "此模板正在被另一位用户编辑，你当前修改尚未应用。",
  );
});

test("lock recovery retries at most once when the write still conflicts", async () => {
  const runWithRecovery = loadLockRecoveryHelper();
  let attempts = 0;
  let renewals = 0;
  const makeConflict = () => {
    const error = new Error("编辑会话已失效");
    error.status = 409;
    error.code = "edit_lock_conflict";
    return error;
  };

  await assert.rejects(
    runWithRecovery({
      name: "autosave-settings",
      action: async () => { attempts += 1; throw makeConflict(); },
      renewLock: async () => { renewals += 1; },
    }),
    /编辑会话已失效/,
  );
  assert.equal(attempts, 2);
  assert.equal(renewals, 1);
});

function fakeTimers() {
  let nextId = 1;
  const timers = new Map();
  return {
    setTimer(fn, delay) {
      const id = nextId++;
      timers.set(id, { fn, delay, cleared: false, fired: false });
      return id;
    },
    clearTimer(id) {
      if (timers.has(id)) timers.get(id).cleared = true;
    },
    fireActive() {
      for (const timer of timers.values()) {
        if (!timer.cleared && !timer.fired) {
          timer.fired = true;
          timer.fn();
        }
      }
    },
    active() {
      return [...timers.values()].filter((timer) => !timer.cleared && !timer.fired);
    },
    all() {
      return [...timers.values()];
    },
  };
}

function deferred() {
  let resolve;
  let reject;
  const promise = new Promise((res, rej) => { resolve = res; reject = rej; });
  return { promise, resolve, reject };
}

async function nextMicrotask() {
  await Promise.resolve();
  await Promise.resolve();
}

test("different block and settings keys debounce independently and save serially", async () => {
  const timers = fakeTimers();
  const calls = [];
  const first = deferred();
  const second = deferred();
  const settings = deferred();
  const secondStarted = deferred();
  const settingsStarted = deferred();
  const coordinator = createCoordinator({
    delayMs: 700,
    setTimer: timers.setTimer,
    clearTimer: timers.clearTimer,
  });

  coordinator.schedule("block:a", async () => { calls.push("stale-a"); });
  coordinator.schedule("block:a", async () => { calls.push("a"); await first.promise; });
  coordinator.schedule("block:b", async () => {
    calls.push("b");
    secondStarted.resolve();
    await second.promise;
  });
  coordinator.schedule("settings", async () => {
    calls.push("settings");
    settingsStarted.resolve();
    await settings.promise;
  });

  assert.equal(timers.all()[0].cleared, true, "only the replaced A timer is cleared");
  assert.equal(timers.active().length, 3, "A, B and settings own separate timers");
  assert.ok(timers.active().every((timer) => timer.delay === 700));
  assert.deepEqual([...coordinator.dirtyKeys()], ["block:a", "block:b", "settings"]);

  timers.fireActive();
  await nextMicrotask();
  assert.deepEqual(calls, ["a"], "only one network save starts at a time");

  first.resolve();
  await secondStarted.promise;
  assert.deepEqual(calls, ["a", "b"]);
  second.resolve();
  await settingsStarted.promise;
  assert.deepEqual(calls, ["a", "b", "settings"]);
  settings.resolve();
  await coordinator.whenIdle();

  assert.deepEqual(calls, ["a", "b", "settings"]);
  assert.deepEqual([...coordinator.dirtyKeys()], []);
  assert.equal(coordinator.hasDirty(), false);
});

test("failed key remains dirty and retryable after another key succeeds", async () => {
  const timers = fakeTimers();
  const calls = [];
  const events = [];
  let aAttempts = 0;
  const coordinator = createCoordinator({
    setTimer: timers.setTimer,
    clearTimer: timers.clearTimer,
    onStateChange: (event) => events.push(event),
  });
  coordinator.schedule("block:a", async () => {
    aAttempts += 1;
    calls.push(`a-${aAttempts}`);
    if (aAttempts === 1) throw new Error("A failed");
  });
  coordinator.schedule("block:b", async () => { calls.push("b"); });

  timers.fireActive();
  await coordinator.whenIdle();

  assert.deepEqual(calls, ["a-1", "b"]);
  assert.deepEqual([...coordinator.dirtyKeys()], ["block:a"]);
  assert.equal(coordinator.isDirty("block:a"), true);
  assert.equal(coordinator.isDirty("block:b"), false);
  assert.deepEqual([...coordinator.failedKeys()], ["block:a"]);
  assert.equal(coordinator.errorFor("block:a").message, "A failed");
  const afterBSuccess = events.at(-1);
  assert.equal(afterBSuccess.type, "saved");
  assert.equal(afterBSuccess.hasError, true);
  assert.equal(afterBSuccess.currentError.key, "block:a");
  assert.equal(afterBSuccess.currentError.error.message, "A failed");

  coordinator.retry("block:a");
  await coordinator.whenIdle();
  assert.deepEqual(calls, ["a-1", "b", "a-2"]);
  assert.deepEqual([...coordinator.dirtyKeys()], []);
});

test("disposing a dirty key cancels its timer and leaves no unload dirtiness", async () => {
  const timers = fakeTimers();
  const calls = [];
  const coordinator = createCoordinator({
    setTimer: timers.setTimer,
    clearTimer: timers.clearTimer,
  });
  coordinator.schedule("block:a", async () => { calls.push("stale-save"); });

  const deletion = coordinator.dispose("block:a", async () => {
    calls.push("delete");
  });

  assert.equal(timers.all()[0].cleared, true);
  assert.equal(coordinator.isDirty("block:a"), false);
  assert.equal(coordinator.hasDirty(), false);
  assert.equal(coordinator.isDisposed("block:a"), true);
  timers.fireActive();
  await deletion;
  await coordinator.whenIdle();
  assert.deepEqual(calls, ["delete"]);
  assert.throws(
    () => coordinator.schedule("block:a", async () => {}),
    /停用/,
  );
});

test("delete mutation waits for an in-flight save and skips queued stale saves", async () => {
  const inFlightTimers = fakeTimers();
  const inFlightSave = deferred();
  const inFlightStarted = deferred();
  const inFlightCalls = [];
  const inFlightCoordinator = createCoordinator({
    setTimer: inFlightTimers.setTimer,
    clearTimer: inFlightTimers.clearTimer,
  });
  inFlightCoordinator.schedule("block:a", async () => {
    inFlightCalls.push("save");
    inFlightStarted.resolve();
    await inFlightSave.promise;
  });
  inFlightTimers.fireActive();
  await inFlightStarted.promise;
  const inFlightDeletion = inFlightCoordinator.dispose("block:a", async () => {
    inFlightCalls.push("delete");
  });
  await nextMicrotask();
  assert.deepEqual(inFlightCalls, ["save"]);
  inFlightSave.resolve();
  await inFlightDeletion;
  assert.deepEqual(inFlightCalls, ["save", "delete"]);

  const queuedTimers = fakeTimers();
  const blocker = deferred();
  const blockerStarted = deferred();
  const queuedCalls = [];
  const queuedCoordinator = createCoordinator({
    setTimer: queuedTimers.setTimer,
    clearTimer: queuedTimers.clearTimer,
  });
  queuedCoordinator.schedule("block:blocker", async () => {
    queuedCalls.push("blocker");
    blockerStarted.resolve();
    await blocker.promise;
  });
  queuedCoordinator.schedule("block:a", async () => { queuedCalls.push("stale-save"); });
  queuedTimers.fireActive();
  await blockerStarted.promise;
  const queuedDeletion = queuedCoordinator.dispose("block:a", async () => {
    queuedCalls.push("delete");
  });
  blocker.resolve();
  await queuedDeletion;
  assert.deepEqual(queuedCalls, ["blocker", "delete"]);
  assert.equal(queuedCoordinator.hasDirty(), false);
});

test("an older successful save never clears a newer edit for the same key", async () => {
  const timers = fakeTimers();
  const calls = [];
  const oldSave = deferred();
  const newSave = deferred();
  const newStarted = deferred();
  const coordinator = createCoordinator({
    setTimer: timers.setTimer,
    clearTimer: timers.clearTimer,
  });

  coordinator.schedule("block:a", async () => { calls.push("old"); await oldSave.promise; });
  timers.fireActive();
  await nextMicrotask();
  coordinator.schedule("block:a", async () => {
    calls.push("new");
    newStarted.resolve();
    await newSave.promise;
  });
  timers.fireActive();

  oldSave.resolve();
  await newStarted.promise;
  assert.deepEqual(calls, ["old", "new"]);
  assert.equal(coordinator.isDirty("block:a"), true);

  newSave.resolve();
  await coordinator.whenIdle();
  assert.equal(coordinator.isDirty("block:a"), false);
});

test("stale older-generation failure emits no current error after a newer edit", async () => {
  const timers = fakeTimers();
  const events = [];
  const oldSave = deferred();
  const newSave = deferred();
  const oldStarted = deferred();
  const newStarted = deferred();
  const coordinator = createCoordinator({
    setTimer: timers.setTimer,
    clearTimer: timers.clearTimer,
    onStateChange: (event) => events.push(event),
  });
  coordinator.schedule("block:a", async () => {
    oldStarted.resolve();
    await oldSave.promise;
  });
  timers.fireActive();
  await oldStarted.promise;
  coordinator.schedule("block:a", async () => {
    newStarted.resolve();
    await newSave.promise;
  });
  timers.fireActive();

  oldSave.reject(new Error("stale failure"));
  await newStarted.promise;
  assert.equal(events.some((event) => event.type === "error"), false);
  assert.equal(coordinator.errorFor("block:a"), null);

  newSave.resolve();
  await coordinator.whenIdle();
  assert.equal(coordinator.errorFor("block:a"), null);
  assert.deepEqual([...coordinator.failedKeys()], []);
});

test("delete disables only the target card before disposal and removes it only after success", async () => {
  const { deleteContractTemplateBlock } = loadEditorHelpers();
  const targetControls = [
    { disabled: false, dataset: {} },
    { disabled: false, dataset: {} },
    { disabled: true, dataset: {} },
  ];
  const unrelatedControl = { disabled: false, dataset: {} };
  const deleteGate = deferred();
  let removed = false;
  let mutationRan = false;
  const card = {
    dataset: { blockKey: "target" },
    draggable: true,
    querySelectorAll: () => targetControls,
    remove: () => { removed = true; },
  };
  const saveCoordinator = {
    async dispose(key, mutation) {
      assert.equal(key, "block:target");
      assert.ok(targetControls.every((control) => control.disabled));
      assert.equal(card.draggable, false);
      assert.equal(unrelatedControl.disabled, false, "unrelated cards stay editable");
      assert.equal(removed, false, "card stays present until delete succeeds");
      await mutation();
    },
  };

  const deletion = deleteContractTemplateBlock({
    card,
    saveCoordinator,
    ensureLock: async () => {},
    action: async (name, fields) => {
      assert.equal(name, "delete-block");
      assert.equal(fields.block_key, "target");
      mutationRan = true;
      await deleteGate.promise;
    },
  });

  await nextMicrotask();
  assert.equal(mutationRan, true);
  assert.equal(removed, false);
  assert.ok(targetControls.every((control) => control.disabled));
  assert.equal(card.draggable, false);
  deleteGate.resolve();
  const deleted = await deletion;
  assert.equal(deleted, true);
  assert.equal(removed, true);
  assert.ok(targetControls.every((control) => control.disabled));
  assert.equal(card.draggable, false);
  assert.equal(unrelatedControl.disabled, false);
});

test("failed delete restores controls and the prior autosave failure remains retryable", async () => {
  const { deleteContractTemplateBlock } = loadEditorHelpers();
  const timers = fakeTimers();
  const events = [];
  let autosaveAttempts = 0;
  const saveCoordinator = createCoordinator({
    setTimer: timers.setTimer,
    clearTimer: timers.clearTimer,
    onStateChange: (event) => events.push(event),
  });
  saveCoordinator.schedule("block:target", async () => {
    autosaveAttempts += 1;
    if (autosaveAttempts === 1) throw new Error("prior autosave failure");
  });
  timers.fireActive();
  await saveCoordinator.whenIdle();
  const controls = [
    { disabled: false, dataset: {} },
    { disabled: true, dataset: {} },
  ];
  let removed = false;
  const card = {
    dataset: { blockKey: "target" },
    draggable: true,
    querySelectorAll: () => controls,
    remove: () => { removed = true; },
  };

  const deleted = await deleteContractTemplateBlock({
    card,
    saveCoordinator,
    ensureLock: async () => {},
    action: async () => { throw new Error("delete failed"); },
  });

  assert.equal(deleted, false);
  assert.equal(removed, false);
  assert.deepEqual(controls.map((control) => control.disabled), [false, true]);
  assert.equal(card.draggable, true);
  assert.equal(saveCoordinator.isDisposed("block:target"), false);
  assert.equal(saveCoordinator.isDirty("block:target"), true);
  assert.equal(saveCoordinator.errorFor("block:target").message, "prior autosave failure");
  assert.equal(events.at(-1).error.message, "delete failed");
  assert.equal(events.at(-1).currentError.error.message, "prior autosave failure");

  saveCoordinator.retry("block:target");
  await saveCoordinator.whenIdle();
  assert.equal(autosaveAttempts, 2);
  assert.equal(saveCoordinator.hasDirty(), false);
});

test("aggregate status keeps an existing failed key actionable across other mutations", async () => {
  const { renderContractTemplateSaveStatus } = loadEditorHelpers();
  const timers = fakeTimers();
  const rendered = [];
  const coordinator = createCoordinator({
    setTimer: timers.setTimer,
    clearTimer: timers.clearTimer,
    onStateChange: (event) => rendered.push({ event, text: renderContractTemplateSaveStatus(event) }),
  });
  coordinator.schedule("block:a", async () => { throw new Error("A still needs retry"); });
  timers.fireActive();
  await coordinator.whenIdle();
  const mutationStart = rendered.length;

  await coordinator.dispose("block:b", async () => {});
  coordinator.report("operation", { message: "另一项操作完成" });

  const mutationEvents = rendered.slice(mutationStart);
  assert.deepEqual(mutationEvents.map(({ event }) => event.type), ["disposing", "disposed", "operation"]);
  assert.ok(mutationEvents.every(({ text }) => text.includes("A still needs retry")));
  assert.ok(mutationEvents.every(({ text }) => !text.includes("区块已删除")));
  assert.deepEqual([...coordinator.failedKeys()], ["block:a"]);
  assert.equal(coordinator.isDisposed("block:b"), true);
});

test("reentrant successful deletes join one disabled in-flight operation and remove once", async () => {
  const { deleteContractTemplateBlock } = loadEditorHelpers();
  const gate = deferred();
  const controlWrites = [];
  const draggableWrites = [];
  let disabled = false;
  let draggable = true;
  let disposeCalls = 0;
  let actionCalls = 0;
  let removeCalls = 0;
  const control = { dataset: {} };
  Object.defineProperty(control, "disabled", {
    get: () => disabled,
    set: (value) => { disabled = value; controlWrites.push(value); },
  });
  const card = {
    dataset: { blockKey: "target" },
    querySelectorAll: () => [control],
    remove: () => { removeCalls += 1; },
  };
  Object.defineProperty(card, "draggable", {
    get: () => draggable,
    set: (value) => { draggable = value; draggableWrites.push(value); },
  });
  const saveCoordinator = {
    async dispose(_key, mutation) {
      disposeCalls += 1;
      await mutation();
    },
  };
  const args = {
    card,
    saveCoordinator,
    ensureLock: async () => {},
    action: async () => { actionCalls += 1; await gate.promise; },
  };

  const first = deleteContractTemplateBlock(args);
  const second = deleteContractTemplateBlock(args);

  assert.equal(first, second, "reentrant calls join the same per-card promise");
  await nextMicrotask();
  assert.equal(disposeCalls, 1);
  assert.equal(actionCalls, 1);
  assert.deepEqual(controlWrites, [true]);
  assert.deepEqual(draggableWrites, [false]);
  assert.equal(disabled, true);
  assert.equal(draggable, false);
  assert.equal(removeCalls, 0);

  gate.resolve();
  assert.deepEqual(await Promise.all([first, second]), [true, true]);
  assert.equal(removeCalls, 1);
  assert.equal(disabled, true);
  assert.equal(draggable, false);
});

test("reentrant failed deletes join one operation and restore the card exactly once", async () => {
  const { deleteContractTemplateBlock } = loadEditorHelpers();
  const gate = deferred();
  const controlWrites = [];
  const draggableWrites = [];
  let disabled = false;
  let draggable = true;
  let disposeCalls = 0;
  let actionCalls = 0;
  let removeCalls = 0;
  const control = { dataset: {} };
  Object.defineProperty(control, "disabled", {
    get: () => disabled,
    set: (value) => { disabled = value; controlWrites.push(value); },
  });
  const card = {
    dataset: { blockKey: "target" },
    querySelectorAll: () => [control],
    remove: () => { removeCalls += 1; },
  };
  Object.defineProperty(card, "draggable", {
    get: () => draggable,
    set: (value) => { draggable = value; draggableWrites.push(value); },
  });
  const saveCoordinator = {
    async dispose(_key, mutation) {
      disposeCalls += 1;
      await mutation();
    },
  };
  const args = {
    card,
    saveCoordinator,
    ensureLock: async () => {},
    action: async () => {
      actionCalls += 1;
      await gate.promise;
      throw new Error("delete failed");
    },
  };

  const first = deleteContractTemplateBlock(args);
  const second = deleteContractTemplateBlock(args);

  assert.equal(first, second);
  await nextMicrotask();
  assert.equal(disposeCalls, 1);
  assert.equal(actionCalls, 1);
  assert.equal(disabled, true);
  assert.equal(draggable, false);
  gate.resolve();

  assert.deepEqual(await Promise.all([first, second]), [false, false]);
  assert.equal(removeCalls, 0);
  assert.equal(disabled, false);
  assert.equal(draggable, true);
  assert.deepEqual(controlWrites, [true, false]);
  assert.deepEqual(draggableWrites, [false, true]);
});

test("clean operation failures stay actionable without claiming unsaved edits", async () => {
  const { renderContractTemplateSaveStatus } = loadEditorHelpers();
  const timers = fakeTimers();
  const events = [];
  const coordinator = createCoordinator({
    setTimer: timers.setTimer,
    clearTimer: timers.clearTimer,
    onStateChange: (event) => events.push(event),
  });

  await assert.rejects(
    coordinator.dispose("block:clean", async () => { throw new Error("clean delete failed"); }),
    /clean delete failed/,
  );
  const deleteFailure = events.at(-1);
  assert.equal(deleteFailure.hasDirty, false);
  assert.deepEqual([...deleteFailure.failedKeys], []);
  assert.equal(coordinator.hasDirty(), false, "clean failure must not trigger beforeunload");
  assert.equal(renderContractTemplateSaveStatus(deleteFailure), "clean delete failed");

  const operationFailure = coordinator.report("error", { error: new Error("clean operation failed") });
  assert.equal(operationFailure.hasDirty, false);
  assert.equal(renderContractTemplateSaveStatus(operationFailure), "clean operation failed");

  coordinator.schedule("block:dirty", async () => {});
  const dirtyFailure = coordinator.report("error", { error: new Error("operation failed") });
  assert.equal(dirtyFailure.hasDirty, true);
  assert.match(renderContractTemplateSaveStatus(dirtyFailure), /仍有未保存修改/);
  assert.match(renderContractTemplateSaveStatus(dirtyFailure), /重试/);
});

test("formal action flushes multiple block and settings timers serially before dispatch", async () => {
  const runFormalAction = loadFormalActionHelper();
  const timers = fakeTimers();
  const first = deferred();
  const second = deferred();
  const settings = deferred();
  const firstStarted = deferred();
  const secondStarted = deferred();
  const settingsStarted = deferred();
  const calls = [];
  const coordinator = createCoordinator({
    setTimer: timers.setTimer,
    clearTimer: timers.clearTimer,
  });
  coordinator.schedule("block:a", async () => {
    calls.push("save:a");
    firstStarted.resolve();
    await first.promise;
  });
  coordinator.schedule("block:b", async () => {
    calls.push("save:b");
    secondStarted.resolve();
    await second.promise;
  });
  coordinator.schedule("settings", async () => {
    calls.push("save:settings");
    settingsStarted.resolve();
    await settings.promise;
  });

  const formalAction = runFormalAction({
    name: "simulate",
    saveCoordinator: coordinator,
    ensureLock: async () => { calls.push("lock"); },
    buildFields: () => {
      calls.push("fields");
      return { student_id: "student-after-save" };
    },
    action: async (name, fields) => {
      calls.push(`action:${name}:${fields.student_id}`);
      return { ok: true };
    },
  });

  await firstStarted.promise;
  assert.deepEqual(calls, ["lock", "save:a"]);
  assert.equal(timers.active().length, 0, "prepareForAction cancels debounce timers");
  first.resolve();
  await secondStarted.promise;
  assert.deepEqual(calls, ["lock", "save:a", "save:b"]);
  second.resolve();
  await settingsStarted.promise;
  assert.deepEqual(calls, ["lock", "save:a", "save:b", "save:settings"]);
  settings.resolve();
  assert.deepEqual(await formalAction, { ok: true });
  assert.deepEqual(calls, [
    "lock", "save:a", "save:b", "save:settings", "fields",
    "action:simulate:student-after-save",
  ]);
  assert.equal(coordinator.hasDirty(), false);
  assert.deepEqual([...coordinator.failedKeys()], []);
  timers.fireActive();
  await coordinator.whenIdle();
  assert.equal(calls.filter((item) => item.startsWith("save:")).length, 3);
});

test("failed pending save blocks every formal action without retry or data loss", async () => {
  const runFormalAction = loadFormalActionHelper();
  for (const name of ["simulate", "validate", "preview", "publish", "apply"]) {
    const timers = fakeTimers();
    let failedAttempts = 0;
    let actionCalls = 0;
    const coordinator = createCoordinator({
      setTimer: timers.setTimer,
      clearTimer: timers.clearTimer,
    });
    coordinator.schedule("block:a", async () => {});
    coordinator.schedule("settings", async () => {
      failedAttempts += 1;
      throw new Error(`settings failed before ${name}`);
    });

    await assert.rejects(
      runFormalAction({
        name,
        saveCoordinator: coordinator,
        ensureLock: async () => {},
        buildFields: () => ({}),
        action: async () => { actionCalls += 1; },
      }),
      new RegExp(`settings failed before ${name}`),
    );

    assert.equal(actionCalls, 0, `${name} must not dispatch while a key failed`);
    assert.equal(failedAttempts, 1, "prepareForAction never retries a failed save implicitly");
    assert.equal(coordinator.hasDirty(), true);
    assert.deepEqual([...coordinator.dirtyKeys()], ["settings"]);
    assert.deepEqual([...coordinator.failedKeys()], ["settings"]);
    assert.match(coordinator.errorFor("settings").message, /settings failed/);
    assert.equal(timers.active().length, 0);
  }
});

test("each formal action dispatches only after its immediate pending edit is clean", async () => {
  const runFormalAction = loadFormalActionHelper();
  for (const name of ["simulate", "validate", "preview", "publish", "apply"]) {
    const timers = fakeTimers();
    const calls = [];
    const coordinator = createCoordinator({
      setTimer: timers.setTimer,
      clearTimer: timers.clearTimer,
    });
    coordinator.schedule(`block:${name}`, async () => { calls.push(`save:${name}`); });

    await runFormalAction({
      name,
      saveCoordinator: coordinator,
      ensureLock: async () => { calls.push("lock"); },
      action: async (actionName) => { calls.push(`action:${actionName}`); },
    });

    assert.deepEqual(calls, ["lock", `save:${name}`, `action:${name}`]);
    assert.equal(coordinator.hasDirty(), false);
    assert.deepEqual([...coordinator.failedKeys()], []);
    assert.equal(timers.active().length, 0);
  }
});

test("formal action freeze restores exact mutation control and drag state without touching navigation", () => {
  const { setContractTemplateEditorFormalActionPending } = loadFormalActionController();
  const enabled = { disabled: false };
  const preDisabled = { disabled: true };
  const link = { disabled: false };
  const draggable = { draggable: true, classList: { remove() {} } };
  const fixed = { draggable: false, classList: { remove() {} } };
  const attributes = new Map();
  const editor = {
    querySelectorAll(selector) {
      if (selector === "input,textarea,select,button,[contenteditable]") return [enabled, preDisabled];
      if (selector === ".contract-template-block") return [draggable, fixed];
      return [];
    },
    hasAttribute: (name) => attributes.has(name),
    getAttribute: (name) => attributes.get(name) ?? null,
    setAttribute: (name, value) => attributes.set(name, String(value)),
    removeAttribute: (name) => attributes.delete(name),
  };

  setContractTemplateEditorFormalActionPending(editor, true);
  assert.equal(enabled.disabled, true);
  assert.equal(preDisabled.disabled, true);
  assert.equal(draggable.draggable, false);
  assert.equal(fixed.draggable, false);
  assert.equal(attributes.get("aria-busy"), "true");
  assert.equal(link.disabled, false, "unrelated navigation remains usable");

  setContractTemplateEditorFormalActionPending(editor, false);
  assert.equal(enabled.disabled, false);
  assert.equal(preDisabled.disabled, true);
  assert.equal(draggable.draggable, true);
  assert.equal(fixed.draggable, false);
  assert.equal(attributes.has("aria-busy"), false);
});

test("double simulate joins one request while cross-action concurrency is rejected", async () => {
  const { createContractTemplateFormalActionController } = loadFormalActionController();
  const gate = deferred();
  const pending = [];
  const requests = [];
  const edits = [];
  const controller = createContractTemplateFormalActionController({
    setPending: (value) => pending.push(value),
    execute: async ({ name }) => {
      requests.push(name);
      await gate.promise;
      return { name };
    },
  });

  const first = controller.run({ name: "simulate" });
  const second = controller.run({ name: "simulate" });
  assert.equal(first, second, "double-click joins the same in-flight promise");
  await assert.rejects(controller.run({ name: "publish", keepFrozenOnSuccess: true }), /正在执行/);
  await nextMicrotask();
  assert.deepEqual(requests, ["simulate"]);
  assert.equal(controller.isActive(), true);
  assert.equal(controller.runMutation(() => edits.push("forbidden")), false);
  assert.deepEqual(edits, []);
  assert.deepEqual(pending, [true]);

  gate.resolve();
  assert.deepEqual(await Promise.all([first, second]), [{ name: "simulate" }, { name: "simulate" }]);
  assert.deepEqual(requests, ["simulate"]);
  assert.deepEqual(pending, [true, false]);
  assert.equal(controller.isActive(), false);
  controller.runMutation(() => edits.push("allowed"));
  assert.deepEqual(edits, ["allowed"]);
});

test("double publish stays frozen after its single successful request until reload", async () => {
  const { createContractTemplateFormalActionController } = loadFormalActionController();
  const gate = deferred();
  const pending = [];
  let requests = 0;
  const controller = createContractTemplateFormalActionController({
    setPending: (value) => pending.push(value),
    execute: async () => { requests += 1; await gate.promise; return { ok: true }; },
  });

  const first = controller.run({ name: "publish", keepFrozenOnSuccess: true });
  const second = controller.run({ name: "publish", keepFrozenOnSuccess: true });
  assert.equal(first, second);
  await nextMicrotask();
  assert.equal(requests, 1);
  gate.resolve();
  await Promise.all([first, second]);

  assert.equal(requests, 1);
  assert.deepEqual(pending, [true], "publish success never restores an editable pre-reload window");
  assert.equal(controller.isActive(), true);
  assert.equal(controller.runMutation(() => { throw new Error("must stay frozen"); }), false);
  await assert.rejects(controller.run({ name: "simulate" }), /正在执行/);
});

test("failed action preparation restores controls and preserves failed save for explicit edit retry", async () => {
  const runFormalAction = loadFormalActionHelper();
  const { createContractTemplateFormalActionController } = loadFormalActionController();
  const timers = fakeTimers();
  const pending = [];
  let saveAttempts = 0;
  let actionCalls = 0;
  const coordinator = createCoordinator({
    setTimer: timers.setTimer,
    clearTimer: timers.clearTimer,
  });
  const controller = createContractTemplateFormalActionController({
    setPending: (value) => pending.push(value),
    execute: (options) => runFormalAction({
      ...options,
      saveCoordinator: coordinator,
      ensureLock: async () => {},
      action: async () => { actionCalls += 1; return { ok: true }; },
    }),
  });
  coordinator.schedule("settings", async () => {
    saveAttempts += 1;
    throw new Error("settings save failed");
  });

  await assert.rejects(controller.run({ name: "validate" }), /settings save failed/);
  assert.deepEqual(pending, [true, false]);
  assert.equal(controller.isActive(), false);
  assert.equal(actionCalls, 0);
  assert.equal(saveAttempts, 1);
  assert.equal(coordinator.isDirty("settings"), true);
  assert.deepEqual([...coordinator.failedKeys()], ["settings"]);

  coordinator.schedule("settings", async () => { saveAttempts += 1; });
  assert.deepEqual(await controller.run({ name: "validate" }), { ok: true });
  assert.deepEqual(pending, [true, false, true, false]);
  assert.equal(saveAttempts, 2);
  assert.equal(actionCalls, 1);
  assert.equal(coordinator.hasDirty(), false);
});

test("pending delete blocks simulate and publish without a formal request or freeze overlap", async () => {
  const { createContractTemplateFormalActionController } = loadFormalActionController();
  for (const name of ["simulate", "publish", "apply"]) {
    const deleteGate = deferred();
    const deleteStarted = deferred();
    const pending = [];
    let formalRequests = 0;
    const controller = createContractTemplateFormalActionController({
      setPending: (value) => pending.push(value),
      execute: async () => { formalRequests += 1; return { ok: true }; },
    });
    const deletion = controller.runMutation(async () => {
      deleteStarted.resolve();
      await deleteGate.promise;
      return true;
    }, { key: "delete:block:a" });
    await deleteStarted.promise;

    await assert.rejects(
      controller.run({ name, keepFrozenOnSuccess: name === "publish" || name === "apply" }),
      /编辑操作正在执行/,
    );
    assert.equal(formalRequests, 0);
    assert.deepEqual(pending, [true], "rejected formal admission adds no competing freeze owner");
    assert.equal(controller.isActive(), true);

    deleteGate.resolve();
    assert.equal(await deletion, true);
    assert.deepEqual(pending, [true, false]);
    assert.equal(controller.isActive(), false);
  }
});

test("failed delete and rejected formal action restore exact card state before later non-publish action", async () => {
  const { deleteContractTemplateBlock } = loadEditorHelpers();
  const {
    createContractTemplateFormalActionController,
    setContractTemplateEditorFormalActionPending,
  } = loadFormalActionController();
  const deleteGate = deferred();
  let controlDisabled = false;
  let cardDraggable = true;
  let formalRequests = 0;
  const control = { dataset: {} };
  Object.defineProperty(control, "disabled", {
    get: () => controlDisabled,
    set: (value) => { controlDisabled = value; },
  });
  const card = {
    dataset: { blockKey: "target" },
    querySelectorAll: () => [control],
    remove() { throw new Error("failed delete must not remove card"); },
    classList: { remove() {} },
  };
  Object.defineProperty(card, "draggable", {
    get: () => cardDraggable,
    set: (value) => { cardDraggable = value; },
  });
  const attributes = new Map();
  const editor = {
    querySelectorAll(selector) {
      if (selector === "input,textarea,select,button,[contenteditable]") return [control];
      if (selector === ".contract-template-block") return [card];
      return [];
    },
    hasAttribute: (name) => attributes.has(name),
    getAttribute: (name) => attributes.get(name) ?? null,
    setAttribute: (name, value) => attributes.set(name, String(value)),
    removeAttribute: (name) => attributes.delete(name),
  };
  const saveCoordinator = {
    async dispose(_key, mutation) { await mutation(); },
  };
  const controller = createContractTemplateFormalActionController({
    setPending: (pending) => setContractTemplateEditorFormalActionPending(editor, pending),
    execute: async () => {
      formalRequests += 1;
      assert.equal(controlDisabled, true, "formal request runs only under its own freeze");
      return { ok: true };
    },
  });
  const deletion = controller.runMutation(
    () => deleteContractTemplateBlock({
      card,
      saveCoordinator,
      ensureLock: async () => {},
      action: async () => { await deleteGate.promise; throw new Error("delete failed"); },
    }),
    { key: "delete:target" },
  );
  await nextMicrotask();
  assert.equal(controlDisabled, true);
  assert.equal(cardDraggable, false);

  await assert.rejects(controller.run({ name: "simulate" }), /编辑操作正在执行/);
  assert.equal(formalRequests, 0);
  assert.equal(controlDisabled, true, "formal rejection never releases the delete-owned freeze");
  deleteGate.resolve();
  assert.equal(await deletion, false);
  assert.equal(controlDisabled, false);
  assert.equal(cardDraggable, true);

  assert.deepEqual(await controller.run({ name: "simulate" }), { ok: true });
  assert.equal(formalRequests, 1);
  assert.equal(controlDisabled, false, "non-publish success leaves no stale disabled state");
  assert.equal(cardDraggable, true);
});

test("formal action rejects delete without side effects until the action settles", async () => {
  const { createContractTemplateFormalActionController } = loadFormalActionController();
  const formalGate = deferred();
  let deleteCalls = 0;
  const pending = [];
  const controller = createContractTemplateFormalActionController({
    setPending: (value) => pending.push(value),
    execute: async () => { await formalGate.promise; return { ok: true }; },
  });
  const formal = controller.run({ name: "simulate" });
  await nextMicrotask();

  const deletion = controller.runMutation(async () => {
    deleteCalls += 1;
    return true;
  }, { key: "delete:target" });
  assert.equal(deletion, false);
  assert.equal(deleteCalls, 0);
  assert.deepEqual(pending, [true]);

  formalGate.resolve();
  assert.deepEqual(await formal, { ok: true });
  assert.deepEqual(pending, [true, false]);
  assert.equal(controller.isActive(), false);
});

test("same pending delete key joins once through the editor operation gate without deadlock", async () => {
  const { createContractTemplateFormalActionController } = loadFormalActionController();
  const gate = deferred();
  let factories = 0;
  let deleteCalls = 0;
  const controller = createContractTemplateFormalActionController({
    setPending: () => {},
    execute: async () => ({ ok: true }),
  });
  const mutation = async () => {
    factories += 1;
    deleteCalls += 1;
    await gate.promise;
    return true;
  };

  const first = controller.runMutation(mutation, { key: "delete:target" });
  const second = controller.runMutation(mutation, { key: "delete:target" });
  assert.equal(first, second);
  await nextMicrotask();
  assert.equal(factories, 1);
  assert.equal(deleteCalls, 1);
  gate.resolve();
  assert.deepEqual(await Promise.all([first, second]), [true, true]);
  assert.equal(controller.isActive(), false);
});

function mutationEditorFixture() {
  let disabled = false;
  let draggable = true;
  const controlAttributes = new Map([["contenteditable", "true"]]);
  const editorAttributes = new Map();
  const control = {
    hasAttribute: (name) => controlAttributes.has(name),
    getAttribute: (name) => controlAttributes.get(name) ?? null,
    setAttribute: (name, value) => controlAttributes.set(name, String(value)),
  };
  Object.defineProperty(control, "disabled", {
    get: () => disabled,
    set: (value) => { disabled = value; },
  });
  const card = { classList: { remove() {} } };
  Object.defineProperty(card, "draggable", {
    get: () => draggable,
    set: (value) => { draggable = value; },
  });
  const editor = {
    querySelectorAll(selector) {
      if (selector === "input,textarea,select,button,[contenteditable]") return [control];
      if (selector === ".contract-template-block") return [card];
      return [];
    },
    hasAttribute: (name) => editorAttributes.has(name),
    getAttribute: (name) => editorAttributes.get(name) ?? null,
    setAttribute: (name, value) => editorAttributes.set(name, String(value)),
    removeAttribute: (name) => editorAttributes.delete(name),
  };
  return {
    editor,
    control,
    card,
    state: () => ({
      disabled,
      draggable,
      contenteditable: controlAttributes.get("contenteditable"),
      busy: editorAttributes.get("aria-busy"),
    }),
  };
}

test("all keyed async mutations freeze visible controls before local events can be rejected", async () => {
  const {
    createContractTemplateFormalActionController,
    setContractTemplateEditorFormalActionPending,
  } = loadFormalActionController();
  const cases = [
    ["delete:target", false],
    ["reorder", false],
    ["create-block", true],
    ["republish:41", true],
  ];
  for (const [key, keepFrozenOnSuccess] of cases) {
    const fixture = mutationEditorFixture();
    const gate = deferred();
    let networkCalls = 0;
    let scheduledDirty = 0;
    const controller = createContractTemplateFormalActionController({
      setPending: (pending) => {
        setContractTemplateEditorFormalActionPending(fixture.editor, pending);
      },
      execute: async () => { throw new Error("formal request must not run"); },
    });
    const operation = controller.runMutation(async () => {
      networkCalls += 1;
      assert.equal(
        controller.allowsLocalMutation(),
        false,
        `${key} owns the gate before its async callback can synchronously dispatch input`,
      );
      await gate.promise;
      return { ok: true };
    }, { key, keepFrozenOnSuccess });

    assert.deepEqual(fixture.state(), {
      disabled: true,
      draggable: false,
      contenteditable: "false",
      busy: "true",
    }, `${key} freezes before its async handler proceeds`);
    assert.equal(controller.allowsLocalMutation(), false);
    if (controller.allowsLocalMutation()) scheduledDirty += 1;
    assert.equal(scheduledDirty, 0, "a direct disabled-control event creates no untracked dirty edit");
    await assert.rejects(controller.run({ name: "simulate" }), /编辑操作正在执行/);
    assert.equal(networkCalls, 1);

    gate.resolve();
    assert.deepEqual(await operation, { ok: true });
    if (keepFrozenOnSuccess) {
      assert.deepEqual(fixture.state(), {
        disabled: true,
        draggable: false,
        contenteditable: "false",
        busy: "true",
      }, `${key} remains frozen for navigation/reload success`);
      assert.equal(controller.isActive(), true);
    } else {
      assert.deepEqual(fixture.state(), {
        disabled: false,
        draggable: true,
        contenteditable: "true",
        busy: undefined,
      }, `${key} restores exact state once after non-navigation success`);
      assert.equal(controller.isActive(), false);
    }
  }
});

test("every keyed mutation failure restores once and remains retryable without lost local dirtiness", async () => {
  const {
    createContractTemplateFormalActionController,
    setContractTemplateEditorFormalActionPending,
  } = loadFormalActionController();
  for (const key of ["delete:target", "reorder", "create-block", "republish:41"]) {
    const fixture = mutationEditorFixture();
    let attempts = 0;
    let restores = 0;
    const controller = createContractTemplateFormalActionController({
      setPending: (pending) => {
        setContractTemplateEditorFormalActionPending(fixture.editor, pending);
        if (!pending) restores += 1;
      },
      execute: async () => ({ ok: true }),
    });

    await assert.rejects(controller.runMutation(async () => {
      attempts += 1;
      throw new Error(`${key} failed`);
    }, { key, keepFrozenOnSuccess: key.startsWith("create") || key.startsWith("republish") }), /failed/);
    assert.equal(restores, 1);
    assert.deepEqual(fixture.state(), {
      disabled: false,
      draggable: true,
      contenteditable: "true",
      busy: undefined,
    });
    assert.equal(controller.allowsLocalMutation(), true);
    let scheduledDirty = 0;
    if (controller.allowsLocalMutation()) scheduledDirty += 1;
    assert.equal(scheduledDirty, 1, "restored control events remain trackable for retry");

    const retry = controller.runMutation(async () => {
      attempts += 1;
      return { ok: true };
    }, { key });
    assert.equal(fixture.state().disabled, true);
    assert.deepEqual(await retry, { ok: true });
    assert.equal(fixture.state().disabled, false);
    assert.equal(restores, 2);
    assert.equal(attempts, 2);
  }
});
