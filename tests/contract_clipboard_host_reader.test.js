#!/usr/bin/env node

const assert = require("node:assert/strict");
const path = require("node:path");
const { spawnSync } = require("node:child_process");
const test = require("node:test");

function loadReader() {
  try {
    return require("./contract_clipboard_host_reader");
  } catch (error) {
    assert.fail(`host Clipboard authority module is missing: ${error.message}`);
  }
}

test("rejects a Clipboard reader that is not explicitly host-authoritative", () => {
  const { requireHostClipboardAuthority } = loadReader();

  assert.throws(
    () => requireHostClipboardAuthority({}),
    (error) => error?.code === "HOST_CLIPBOARD_AUTHORITY_REQUIRED",
  );
  assert.throws(
    () => requireHostClipboardAuthority({ AAIE_HOST_CLIPBOARD_AUTHORITY: "sandboxed" }),
    (error) => error?.code === "HOST_CLIPBOARD_AUTHORITY_REQUIRED",
  );
});

test("waits for the authoritative host reader to observe the exact link", async () => {
  const { waitForExactHostClipboard } = loadReader();
  const values = ["HOST_SENTINEL", "HOST_SENTINEL", "https://signing.example.test/exact-42"];
  let now = 1000;

  const evidence = await waitForExactHostClipboard({
    expected: "https://signing.example.test/exact-42",
    read: () => values.shift(),
    now: () => now,
    sleep: async (milliseconds) => { now += milliseconds; },
    timeoutMs: 500,
    intervalMs: 25,
  });

  assert.deepEqual(evidence, {
    value: "https://signing.example.test/exact-42",
    attempts: 3,
    observedAt: 1050,
  });
});

test("fails closed when the host reader never observes the exact link", async () => {
  const { waitForExactHostClipboard } = loadReader();
  let now = 2000;

  await assert.rejects(
    waitForExactHostClipboard({
      expected: "https://signing.example.test/exact-42",
      read: () => "HOST_SENTINEL",
      now: () => now,
      sleep: async (milliseconds) => { now += milliseconds; },
      timeoutMs: 50,
      intervalMs: 25,
    }),
    (error) => (
      error?.code === "HOST_CLIPBOARD_EXACT_VALUE_NOT_OBSERVED"
      && error?.lastObserved === "HOST_SENTINEL"
      && error?.attempts === 3
    ),
  );
});

test("real Chrome acceptance exits before browser work without host authority", () => {
  const script = path.join(__dirname, "contract_creation_real_chrome_clipboard_acceptance.js");
  const result = spawnSync(process.execPath, [script, "--authority-preflight"], {
    encoding: "utf8",
    env: {
      ...process.env,
      AAIE_HOST_CLIPBOARD_AUTHORITY: "sandboxed",
    },
    timeout: 5000,
  });

  assert.equal(result.status, 2);
  assert.match(result.stderr, /HOST_CLIPBOARD_AUTHORITY_REQUIRED/);
  assert.doesNotMatch(result.stderr, /ERR_CONNECTION_REFUSED|Invalid URL/);
});
