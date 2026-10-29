/* Local Visa form interactions, served under the existing same-origin CSP. */
(() => {
  function bindVisaForms() {
    document.querySelectorAll('[data-staff-repeat]').forEach(group => {
      const add = group.querySelector('[data-add-row]');
      if (!add || add.dataset.visaBound) return;
      add.dataset.visaBound = '1';
      add.addEventListener('click', () => {
        const index = Number(group.dataset.next);
        group.dataset.next = String(index + 1);
        const holder = document.createElement('div');
        holder.innerHTML = group.querySelector('template').innerHTML.replaceAll('__INDEX__', '__' + index + '__');
        const row = holder.firstElementChild;
        group.querySelector('[data-rows]').append(row);
        row.querySelector('input,textarea,select').focus();
      });
    });
    const summary = document.querySelector('.visa-validation-summary');
    if (summary) {
      summary.focus();
      summary.scrollIntoView({block: 'center'});
    }
    const name = new URLSearchParams(location.search).get('focus');
    if (name) {
      const field = Array.from(document.querySelectorAll('input,select,textarea')).find(e => e.name === name);
      if (field) {
        field.classList.add('visa-field-error');
        field.setAttribute('aria-invalid', 'true');
        field.focus();
        field.scrollIntoView({block: 'center'});
      }
    }
  }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', bindVisaForms, {once:true});
  else bindVisaForms();
})();
