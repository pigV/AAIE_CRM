/* Progressive presentation only. All authority and posting stay server-side. */
document.querySelectorAll('[data-hcr-editor]').forEach((form) => {
  const kind = form.querySelector('select[name="service_kind"]');
  const cycle = form.querySelector('select[name="application_cycle_id"]');
  const slots = JSON.parse(form.dataset.hcrSlots || '{}');
  const currency = form.querySelector('select[name="currency_code"]');
  const visa = form.querySelector('[data-hcr-visa]');
  const settlement = form.querySelector('[data-hcr-settlement]');
  const cnyNote = form.querySelector('[data-hcr-cny-note]');
  function updateFields() {
    const isVisa = kind.value === 'student_visa';
    const isCny = currency.value === 'CNY';
    visa.hidden = !isVisa;
    visa.querySelector('input').disabled = !isVisa;
    settlement.hidden = isCny;
    settlement.querySelector('input').disabled = isCny;
    cnyNote.hidden = !isCny;
  }
  function updateKinds() {
    if (!kind.disabled) {
      const allowed = slots[cycle.value] || [];
      [...kind.options].forEach((option) => { option.disabled = !allowed.includes(option.value); });
      if (!allowed.includes(kind.value)) kind.value = allowed[0] || '';
    }
    updateFields();
  }
  cycle.addEventListener('change', updateKinds);
  kind.addEventListener('change', updateFields);
  currency.addEventListener('change', updateFields);
  updateKinds();
});
document.querySelectorAll('[data-hcr-disclose]').forEach((link) => {
  link.addEventListener('click', (event) => {
    const details = document.getElementById(link.dataset.hcrDisclose);
    if (!details) return;
    event.preventDefault();
    details.open = true;
    const kind = details.querySelector('select[name="evidence_kind"]');
    if (kind && link.dataset.evidenceKind) kind.value = link.dataset.evidenceKind;
    details.scrollIntoView({block: 'start'});
    const target = kind || details.querySelector('select:not(:disabled),input:not([type="hidden"]):not(:disabled),textarea') || details.querySelector('summary');
    if (target) target.focus({preventScroll:true});
  });
});
