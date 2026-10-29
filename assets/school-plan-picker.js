(() => {
  const root = document.querySelector("[data-school-plan-picker]");
  if (!root || root.dataset.initialized === "true") return;

  const rowsContainer = root.querySelector("[data-school-plan-rows]");
  const addButton = root.querySelector("#school-plan-add-school");
  const source = root.querySelector("[data-school-plan-source]");
  if (!rowsContainer || !addButton || !source) return;

  let schools = [];
  try {
    schools = JSON.parse(source.textContent || "[]");
  } catch (_) {
    schools = [];
  }

  const normalise = (value) => String(value || "").trim().toLowerCase();
  const findSchool = (value) => {
    const query = normalise(value);
    if (!query) return null;
    const exact = schools.find((school) => (
      normalise(school.label) === query || normalise(school.name) === query
    ));
    if (exact) return exact;
    return schools.find((school) => (
      normalise(school.label).includes(query)
      || normalise(school.name).includes(query)
      || normalise(school.suburb).includes(query)
      || normalise(school.state).includes(query)
      || normalise(school.postcode).includes(query)
    )) || null;
  };

  const getRows = () => Array.from(rowsContainer.querySelectorAll("[data-school-plan-row]"));

  const renderMetadata = (meta, school) => {
    meta.querySelectorAll("[data-school-plan-meta-chip]").forEach((chip) => chip.remove());
    if (!school) return;
    [school.school_type, school.year_levels, school.state, school.suburb]
      .filter((value) => String(value || "").trim())
      .forEach((value) => {
        const chip = document.createElement("span");
        chip.className = "school-plan-picker__meta-chip";
        chip.setAttribute("data-school-plan-meta-chip", "");
        chip.textContent = value;
        meta.appendChild(chip);
      });
  };

  const updateRow = (row) => {
    const input = row.querySelector("[data-school-plan-search]");
    const hidden = row.querySelector("[data-school-plan-school-id]");
    const meta = row.querySelector("[data-school-plan-meta]");
    if (!input || !hidden || !meta) return;
    const school = findSchool(input.value);
    hidden.value = school ? school.id : "";
    renderMetadata(meta, school);
    row.classList.toggle("has-selection", Boolean(school));
  };

  const renumberRows = () => {
    getRows().forEach((row, index) => {
      const number = index + 1;
      const rowText = `学校 ${number}`;
      const input = row.querySelector("[data-school-plan-search]");
      const rowLabel = row.querySelector("[data-school-plan-row-label]");
      const inputLabel = row.querySelector("[data-school-plan-input-label]");
      const removeButton = row.querySelector("[data-school-plan-remove]");
      if (rowLabel) rowLabel.textContent = rowText;
      if (inputLabel) inputLabel.textContent = `${rowText} 搜索`;
      if (input) {
        input.id = `school-plan-school-search-${number}`;
        input.setAttribute("aria-label", `${rowText} 搜索`);
      }
      if (removeButton) {
        removeButton.setAttribute("aria-label", `移除${rowText}`);
        removeButton.setAttribute("title", `移除${rowText}`);
      }
    });
  };

  const clearRow = (row) => {
    const input = row.querySelector("[data-school-plan-search]");
    const hidden = row.querySelector("[data-school-plan-school-id]");
    const meta = row.querySelector("[data-school-plan-meta]");
    if (input) input.value = "";
    if (hidden) hidden.value = "";
    if (meta) meta.replaceChildren();
    row.classList.remove("has-selection");
  };

  const wireRow = (row) => {
    if (!row || row.dataset.schoolPlanWired === "true") return;
    const input = row.querySelector("[data-school-plan-search]");
    const removeButton = row.querySelector("[data-school-plan-remove]");
    if (!input || !removeButton) return;
    row.dataset.schoolPlanWired = "true";
    input.addEventListener("input", () => updateRow(row));
    input.addEventListener("change", () => updateRow(row));
    input.addEventListener("keydown", (event) => {
      if (event.key === "Enter") {
        event.preventDefault();
        updateRow(row);
        addRow();
      }
    });
    removeButton.addEventListener("click", () => {
      const rows = getRows();
      const currentIndex = rows.indexOf(row);
      if (rows.length > 1) {
        row.remove();
        renumberRows();
        const remainingRows = getRows();
        const focusRow = remainingRows[Math.min(currentIndex, remainingRows.length - 1)];
        focusRow?.querySelector("[data-school-plan-search]")?.focus();
        return;
      }
      clearRow(row);
      input.focus();
    });
  };

  const addRow = () => {
    const templateRow = getRows()[0];
    if (!templateRow) return;
    const row = templateRow.cloneNode(true);
    delete row.dataset.schoolPlanWired;
    clearRow(row);
    rowsContainer.appendChild(row);
    renumberRows();
    wireRow(row);
    row.querySelector("[data-school-plan-search]")?.focus();
  };

  root.dataset.initialized = "true";
  getRows().forEach((row) => wireRow(row));
  renumberRows();
  addButton.addEventListener("click", addRow);
})();
