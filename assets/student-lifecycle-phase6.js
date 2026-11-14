document.addEventListener("click", (event) => {
  const button = event.target.closest("[data-lifecycle-stage-filter]");
  if (!button) return;
  const chart = button.closest("[data-lifecycle-stage-distribution]");
  if (!chart) return;
  const requested = button.dataset.lifecycleStageFilter;
  chart.querySelectorAll("[data-lifecycle-stage-filter]").forEach((item) => {
    const active = item.dataset.lifecycleStageFilter === requested;
    item.classList.toggle("active", active);
    item.setAttribute("aria-pressed", active ? "true" : "false");
  });
  const rows = Array.from(chart.querySelectorAll(".dashboard-workflow-distribution-chart__row"));
  const counts = rows.map((row) => {
    let visible = 0;
    row.querySelectorAll("[data-lifecycle-type]").forEach((student) => {
      const show = requested === "all" || student.dataset.lifecycleType === requested;
      student.hidden = !show;
      if (show) visible += 1;
    });
    const count = row.querySelector("[data-lifecycle-filter-count]");
    if (count) count.textContent = String(visible);
    row.classList.toggle("dashboard-workflow-distribution-chart__row--muted", visible === 0);
    const summary = row.querySelector("summary");
    const stage = row.querySelector(".dashboard-workflow-distribution-chart__name").textContent;
    summary.setAttribute("aria-label", `展开${stage}明细，共 ${visible} 个申请周期`);
    row.querySelector("svg").setAttribute("aria-label", `${stage}：${visible}`);
    row.querySelector("title").textContent = `${stage}：${visible}`;
    return visible;
  });
  const max = Math.max(1, ...counts);
  rows.forEach((row, index) => {
    const width = `${counts[index] / max * 100}%`;
    const bar = row.querySelector(".dashboard-workflow-distribution-chart__svg-fill");
    bar.setAttribute("width", width);
    bar.dataset.width = width;
  });
});

document.addEventListener("change", (event) => {
  const select = event.target;
  if (!select.matches("[data-handling-select]")) return;
  const form = select.closest("[data-lifecycle-business-disposition]");
  if (!form) return;
  const existingChoice = form.querySelector("[data-existing-choice]");
  const naReason = form.querySelector("[data-na-reason]");
  if (existingChoice) existingChoice.hidden = select.value !== "reuse";
  if (naReason) naReason.hidden = select.value !== "skip";
});
