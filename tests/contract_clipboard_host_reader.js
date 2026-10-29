"use strict";

const HOST_AUTHORITY = "macos-host";

function authorityError() {
  const error = new Error(
    "HOST_CLIPBOARD_AUTHORITY_REQUIRED: run this acceptance harness with an authoritative macOS host Clipboard reader",
  );
  error.code = "HOST_CLIPBOARD_AUTHORITY_REQUIRED";
  return error;
}

function requireHostClipboardAuthority(env = process.env) {
  if (env.AAIE_HOST_CLIPBOARD_AUTHORITY !== HOST_AUTHORITY) {
    throw authorityError();
  }
  return HOST_AUTHORITY;
}

const defaultSleep = (milliseconds) => new Promise((resolve) => setTimeout(resolve, milliseconds));

async function waitForExactHostClipboard({
  expected,
  read,
  timeoutMs = 3000,
  intervalMs = 25,
  now = Date.now,
  sleep = defaultSleep,
}) {
  const startedAt = now();
  let attempts = 0;
  let lastObserved;
  do {
    attempts += 1;
    const value = read();
    lastObserved = value;
    if (value === expected) {
      return { value, attempts, observedAt: now() };
    }
    await sleep(intervalMs);
  } while (now() - startedAt <= timeoutMs);

  const error = new Error("HOST_CLIPBOARD_EXACT_VALUE_NOT_OBSERVED");
  error.code = "HOST_CLIPBOARD_EXACT_VALUE_NOT_OBSERVED";
  error.lastObserved = lastObserved;
  error.attempts = attempts;
  throw error;
}

module.exports = {
  HOST_AUTHORITY,
  requireHostClipboardAuthority,
  waitForExactHostClipboard,
};
