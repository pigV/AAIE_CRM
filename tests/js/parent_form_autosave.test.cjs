const assert = require("node:assert/strict");
const { createParentFormDraftController } = require("../../assets/parent-form.js");

const timers = [];
const calls = [];
const controller = createParentFormDraftController({
  delayMs: 2000,
  setTimer: (fn, ms) => {
    timers.push({ fn, ms, cleared: false });
    return timers.length - 1;
  },
  clearTimer: (id) => {
    if (timers[id]) timers[id].cleared = true;
  },
  save: (source) => {
    calls.push(source);
    return Promise.resolve();
  },
});

controller.changed();
controller.changed();
assert.equal(timers.at(-1).ms, 2000);
assert.equal(timers.at(-2).cleared, true);
timers.at(-1).fn();
assert.deepEqual(calls, ["auto"]);

controller.changed();
controller.saveNow();
assert.equal(timers.at(-1).cleared, true);
assert.deepEqual(calls, ["auto", "manual"]);

console.log("parent form autosave controller: ok");
