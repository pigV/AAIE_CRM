const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");

const source = fs.readFileSync("assets/app.js", "utf8");
const startMarker = "const initContractActionFilePickers =";
const endMarker = "// End contract action file pickers";
const start = source.indexOf(startMarker);
const end = source.indexOf(endMarker);
assert.notEqual(start, -1, "contract action file picker initializer marker is absent");
assert.notEqual(end, -1, "contract action file picker end marker is absent");

const listeners = new WeakMap();
const qa = (selector, root) => root.querySelectorAll(selector);
const q = (selector, root) => root.querySelector(selector);
const context = { Array, qa, q };
vm.createContext(context);
vm.runInContext(`${source.slice(start, end)}\nthis.initContractActionFilePickers = initContractActionFilePickers;`, context);

const createRow = () => {
  const status = { textContent: "尚未选择文件" };
  const card = { querySelector: (selector) => selector === "[data-contract-file-name]" ? status : null };
  const input = {
    files: [],
    addEventListener: (eventName, callback) => {
      const eventListeners = listeners.get(input) || {};
      eventListeners[eventName] = callback;
      listeners.set(input, eventListeners);
    },
    closest: (selector) => selector === "[data-contract-action-card]" ? card : null,
    emit: (eventName) => listeners.get(input)?.[eventName]?.(),
  };
  return { input, status };
};

const first = createRow();
const second = createRow();
const root = {
  querySelectorAll: (selector) => selector === "[data-contract-signed-file]" ? [first.input, second.input] : [],
};

context.initContractActionFilePickers(root);
first.input.files = [{ name: "Bo-Hu-signed-contract.pdf" }];
first.input.emit("change");
assert.equal(first.status.textContent, "Bo-Hu-signed-contract.pdf");
assert.equal(second.status.textContent, "尚未选择文件");
first.input.files = [];
first.input.emit("change");
assert.equal(first.status.textContent, "尚未选择文件");

console.log("contract center action card interaction: ok");
