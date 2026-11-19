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
