const assert = require("node:assert/strict");
const {
  actionButtonLabel,
  createScanPoller,
  createDriveUpdateController,
  eligibleSelection,
  selectAllEligible,
} = require("../../assets/google-drive-update.js");

const deferred = () => {
  let resolve;
  let reject;
  const promise = new Promise((ok, fail) => { resolve = ok; reject = fail; });
  return { promise, resolve, reject };
};

(async () => {
  assert.equal(actionButtonLabel("scan", true), "正在扫描 Drive，请稍候…");
  assert.equal(actionButtonLabel("scan", false), "立即检查 Drive");

  const items = [
    { id: "new", eligible: true, checked: false },
    { id: "conflict", eligible: false, checked: false },
    { id: "manual", eligible: false, checked: false },
    { id: "failed", eligible: false, checked: false },
  ];
  assert.deepEqual(eligibleSelection(items), []);
  assert.equal(selectAllEligible(items).find((item) => item.id === "new").checked, true);
  assert.equal(selectAllEligible(items).find((item) => item.id === "conflict").checked, false);
  assert.equal(selectAllEligible(items).find((item) => item.id === "manual").checked, false);
  assert.equal(selectAllEligible(items).find((item) => item.id === "failed").checked, false);

  const pending = deferred();
  const requests = [];
  const locks = [];
  const renders = [];
  const focusCalls = [];
  let startConfirmed = false;
  const controller = createDriveUpdateController({
    request: (action, payload) => {
      requests.push([action, payload]);
      return pending.promise;
    },
    confirmStart: () => startConfirmed,
    setLocked: (action, locked) => locks.push([action, locked]),
    render: (result) => renders.push(result),
    focusResult: (options) => focusCalls.push(options),
  });

  const first = controller.submit("confirm", { selectedIds: ["new"] });
  const second = await controller.submit("confirm", { selectedIds: ["new"] });
  assert.equal(second.ignored, true, "second click must make no request");
  assert.equal(requests.length, 1);
  assert.deepEqual(locks, [["confirm", true]]);
  pending.resolve({ html: "<h3>saved replay report</h3>" });
  await first;
  assert.equal(renders[0].html.includes("saved replay report"), true);
  assert.deepEqual(focusCalls, [{ preventScroll: true }]);
  assert.deepEqual(locks.at(-1), ["confirm", false]);

  const noSelection = await controller.submit("confirm", { selectedIds: [] });
  assert.equal(noSelection.validationError, true);
  assert.equal(requests.length, 1);

  const cancelled = await controller.submit("start", {});
  assert.equal(cancelled.cancelled, true);
  assert.equal(requests.length, 1);
  startConfirmed = true;
  const startPending = deferred();
  controller.setRequest(() => {
    requests.push(["start", {}]);
    return startPending.promise;
  });
  const start = controller.submit("start", {});
  assert.equal(requests.length, 2);
  startPending.resolve({ status: "running" });
  await start;

  const failure = deferred();
  controller.setRequest(() => failure.promise);
  const failedRequest = controller.submit("scan", {});
  assert.deepEqual(locks.at(-1), ["scan", true]);
  failure.reject(new Error("network down"));
  await assert.rejects(failedRequest, /network down/);
  assert.deepEqual(locks.at(-1), ["scan", false], "retry unlock happens only after settlement");

  assert.equal(controller.isInFlight("scan"), false);

  const scheduled = [];
  const progressStates = [];
  const completedStates = [];
  const pollErrors = [];
  const pollResponses = [
    new Error("temporary network error"),
    { ok: true, job_id: "scanjob-safe", state: "running", percent: 47 },
    { ok: true, job_id: "scanjob-safe", state: "completed", percent: 100, workspace_html: "<section>preview</section>" },
  ];
  const poller = createScanPoller({
    request: async (statusUrl) => {
      assert.equal(statusUrl, "/content/google-drive/scan-jobs/scanjob-safe");
      const next = pollResponses.shift();
      if (next instanceof Error) throw next;
      return next;
    },
    schedule: (callback) => {
      scheduled.push(callback);
      return callback;
    },
    onProgress: (job) => progressStates.push(job),
    onComplete: (job) => completedStates.push(job),
    onError: (error) => pollErrors.push(error.message),
  });
  poller.start({
    job_id: "scanjob-safe",
    state: "running",
    percent: 2,
    status_url: "/content/google-drive/scan-jobs/scanjob-safe",
  });
  assert.equal(scheduled.length, 1, "only one polling callback may be scheduled");
  await scheduled.shift()();
  assert.deepEqual(pollErrors, ["temporary network error"]);
  assert.equal(poller.jobId(), "scanjob-safe", "transient errors must keep the same job");
  assert.equal(scheduled.length, 1);
  await scheduled.shift()();
  assert.equal(progressStates.at(-1).percent, 47);
  assert.equal(scheduled.length, 1);
  await scheduled.shift()();
  assert.equal(completedStates.length, 1);
  assert.equal(completedStates[0].workspace_html, "<section>preview</section>");
  assert.equal(scheduled.length, 0, "terminal completion stops polling");
  assert.equal(poller.isActive(), false);

  console.log("google drive update interaction: ok");
})().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
