for (const fields of document.querySelectorAll("[data-student-intake-tuition]")) {
  const card = fields.closest("[data-upload-review-workbench]");
  const type = card.querySelector("[data-document-type-select]");
  const year = fields.querySelector("input[name^='intake_year_']");
  const grade = fields.querySelector("input[name^='intake_grade_']");
  const amount = fields.querySelector("input[name^='intake_aud_']");
  const choice = fields.querySelector("[data-intake-choice]");
  const deferTuition = fields.querySelector("input[name^='intake_defer_']");
  const gradeKey = value => value.trim().toLowerCase().replace(/^year\s*/, "");
  const sync = () => {
    const kinds = fields.dataset.module === "Offer"
      ? ["Offer", "SignedOffer", "OfferAcceptance"] : ["COE", "COE_CAAW"];
    fields.hidden = fields.disabled = !kinds.includes(type.value);
    const deferred = deferTuition?.checked;
    if (deferTuition) {
      for (const input of [year, grade, amount, fields.querySelector("input[name^='intake_coe_school_']")]) {
        if (input) input.disabled = deferred;
      }
    }
    if (choice) {
      const differs = year.value.trim() !== fields.dataset.offerYear
        || gradeKey(grade.value) !== gradeKey(fields.dataset.offerGrade)
        || !amount.value.trim() || Number(amount.value) !== Number(fields.dataset.offerAmount);
      choice.hidden = fields.hidden || deferred || !differs;
      const select = choice.querySelector("select");
      select.disabled = choice.hidden;
    }
  };
  type.addEventListener("change", sync);
  fields.addEventListener("input", sync);
  sync();
}
for (const fields of document.querySelectorAll('[data-school-course-tuition]')) {
  const card = fields.closest('[data-upload-review-workbench]');
  const documentType = card?.querySelector('[data-document-type-select]');
  const mode = fields.querySelector('[data-school-course-mode]');
  const sync = () => {
    fields.hidden = fields.disabled = !['COE', 'COE_CAAW'].includes(documentType?.value);
    for (const section of fields.querySelectorAll('[data-school-course-kind]')) {
      const enabled = !fields.hidden && [section.dataset.schoolCourseKind, 'both'].includes(mode.value);
      section.hidden = !enabled;
      for (const input of section.querySelectorAll('input,select,textarea')) input.disabled = !enabled;
    }
  };
  documentType?.addEventListener('change', sync);
  mode?.addEventListener('change', sync);
  sync();
}
