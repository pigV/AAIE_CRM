(() => {
  const create = document.querySelector('[data-transfer-v2-create]');
  const schoolInteractionRoot = create || document.querySelector('[data-transfer-v2-workspace]');
  if (schoolInteractionRoot) {
    const AUTOCOMPLETE_DELAY_MS = 250;
    const AUTOCOMPLETE_LIMIT = 20;
    const list = schoolInteractionRoot.querySelector('[data-transfer-v2-target-list]');
    const autocompleteTimers = new WeakMap();
    const activeSuggestionIndexes = new WeakMap();
    const writeSelectorFact = (field, value) => {
      if (!field) return;
      field.value = value;
      field.setAttribute('value', value);
    };
    const clearSchoolSelection = (selector) => {
      const schoolState = selector.querySelector('[data-transfer-v2-school-state]');
      const schoolRegion = selector.querySelector('[data-transfer-v2-school-region]');
      const schoolType = selector.querySelector('[data-transfer-v2-school-type]');
      const schoolLibrary = selector.querySelector('[data-transfer-v2-school-library-result]');
      writeSelectorFact(schoolState, '');
      writeSelectorFact(schoolRegion, '');
      writeSelectorFact(schoolType, '');
      if (schoolLibrary) {
        schoolLibrary.value = '';
        schoolLibrary.querySelectorAll('option').forEach((option) => {
          option.hidden = false;
        });
      }
      selector.dataset.selectedSchoolName = '';
    };
    const closeSuggestions = (selector) => {
      const suggestions = selector?.querySelector('[data-transfer-v2-school-suggestions]');
      const schoolName = selector?.querySelector('[data-transfer-v2-school-name]');
      if (!suggestions || !schoolName) return;
      suggestions.replaceChildren();
      suggestions.hidden = true;
      schoolName.setAttribute('aria-expanded', 'false');
      schoolName.removeAttribute('aria-activedescendant');
      activeSuggestionIndexes.delete(selector);
    };
    const matchingSchoolOptions = (selector, query) => {
      const tokens = query.toLocaleLowerCase().split(/\s+/).filter(Boolean);
      return Array.from(selector.querySelectorAll(
        '[data-transfer-v2-school-library-result] option[data-school-id]'
      )).filter((option) => {
        const searchable = [
          option.dataset.schoolName,
          option.dataset.schoolState,
          option.dataset.schoolRegion,
          option.dataset.schoolType,
          option.textContent,
        ].join(' ').toLocaleLowerCase();
        const matched = tokens.every((token) => searchable.includes(token));
        option.hidden = !matched;
        return matched;
      }).slice(0, AUTOCOMPLETE_LIMIT);
    };
    const selectSchoolOption = (selector, option) => {
      const schoolLibrary = selector.querySelector('[data-transfer-v2-school-library-result]');
      const schoolName = selector.querySelector('[data-transfer-v2-school-name]');
      if (!schoolLibrary || !schoolName || !option?.dataset.schoolId) return;
      schoolLibrary.value = option.value;
      writeSelectorFact(selector.querySelector('[data-transfer-v2-school-state]'), option.dataset.schoolState || '');
      writeSelectorFact(selector.querySelector('[data-transfer-v2-school-region]'), option.dataset.schoolRegion || '');
      writeSelectorFact(selector.querySelector('[data-transfer-v2-school-type]'), option.dataset.schoolType || '');
      schoolName.value = option.dataset.schoolName || option.value;
      selector.dataset.selectedSchoolName = schoolName.value;
      closeSuggestions(selector);
    };
    const renderSuggestions = (selector, query) => {
      const suggestions = selector.querySelector('[data-transfer-v2-school-suggestions]');
      const schoolName = selector.querySelector('[data-transfer-v2-school-name]');
      if (!suggestions || !schoolName || query.length < 2) {
        closeSuggestions(selector);
        return;
      }
      const matches = matchingSchoolOptions(selector, query);
      suggestions.replaceChildren();
      matches.forEach((option, index) => {
        const result = document.createElement('button');
        result.type = 'button';
        result.id = `transfer-v2-school-option-${Date.now()}-${index}`;
        result.setAttribute('role', 'option');
        result.setAttribute('aria-selected', 'false');
        result.dataset.schoolId = option.dataset.schoolId;
        result.textContent = option.textContent.trim();
        suggestions.append(result);
      });
      suggestions.hidden = matches.length === 0;
      schoolName.setAttribute('aria-expanded', String(matches.length > 0));
      activeSuggestionIndexes.set(selector, -1);
    };
    const scheduleSuggestions = (selector, query) => {
      const prior = autocompleteTimers.get(selector);
      if (prior) clearTimeout(prior);
      if (query.length < 2) {
        closeSuggestions(selector);
        return;
      }
      autocompleteTimers.set(selector, setTimeout(() => {
        renderSuggestions(selector, query);
      }, AUTOCOMPLETE_DELAY_MS));
    };
    const libraryOptionForResult = (selector, result) => selector.querySelector(
      `[data-transfer-v2-school-library-result] option[data-school-id="${result.dataset.schoolId}"]`
    );
    schoolInteractionRoot.addEventListener('input', (event) => {
      const schoolName = event.target.closest('[data-transfer-v2-school-name]');
      if (!schoolName) return;
      const selector = schoolName.closest('[data-transfer-v2-school-selector]');
      const selectedId = selector?.querySelector('[data-transfer-v2-school-library-result]')?.value;
      if (
        selector && selectedId &&
        schoolName.value === selector.dataset.selectedSchoolName
      ) return;
      if (!selector) return;
      clearSchoolSelection(selector);
      const query = schoolName.value.trim().toLocaleLowerCase();
      selector.querySelectorAll('[data-transfer-v2-school-library-result] option[data-school-id]').forEach((option) => {
        option.hidden = Boolean(query) && !option.textContent.toLocaleLowerCase().includes(query);
      });
      scheduleSuggestions(selector, query);
    });
    schoolInteractionRoot.addEventListener('keydown', (event) => {
      const schoolName = event.target.closest('[data-transfer-v2-school-name]');
      if (!schoolName) return;
      const selector = schoolName.closest('[data-transfer-v2-school-selector]');
      const results = Array.from(selector?.querySelectorAll(
        '[data-transfer-v2-school-suggestions] [role="option"]'
      ) || []);
      if (!selector || !results.length) {
        if (event.key === 'Escape' && selector) closeSuggestions(selector);
        return;
      }
      let index = activeSuggestionIndexes.get(selector) ?? -1;
      if (event.key === 'ArrowDown') index = (index + 1) % results.length;
      else if (event.key === 'ArrowUp') index = (index - 1 + results.length) % results.length;
      else if (event.key === 'Enter' && index >= 0) {
        event.preventDefault();
        selectSchoolOption(selector, libraryOptionForResult(selector, results[index]));
        return;
      } else if (event.key === 'Escape') {
        closeSuggestions(selector);
        return;
      } else return;
      event.preventDefault();
      activeSuggestionIndexes.set(selector, index);
      results.forEach((result, resultIndex) => {
        result.setAttribute('aria-selected', String(resultIndex === index));
      });
      schoolName.setAttribute('aria-activedescendant', results[index].id);
    });
    schoolInteractionRoot.addEventListener('change', (event) => {
      const schoolLibrary = event.target.closest('[data-transfer-v2-school-library-result]');
      if (!schoolLibrary) return;
      const selector = schoolLibrary.closest('[data-transfer-v2-school-selector]');
      const option = schoolLibrary.selectedOptions[0];
      if (!selector || !option?.dataset.schoolId) return;
      selectSchoolOption(selector, option);
    });
    schoolInteractionRoot.querySelector('[data-transfer-v2-add-target]')?.addEventListener('click', () => {
      const original = list?.querySelector('[data-transfer-v2-target-row]');
      if (!original || !list) return;
      const row = original.cloneNode(true);
      const selector = row.querySelector('[data-transfer-v2-school-selector]');
      const schoolName = selector?.querySelector('[data-transfer-v2-school-name]');
      if (schoolName) schoolName.value = '';
      if (selector) clearSchoolSelection(selector);
      list.append(row);
    });
    schoolInteractionRoot.addEventListener('click', (event) => {
      const suggestion = event.target.closest('[data-transfer-v2-school-suggestions] [data-school-id]');
      if (suggestion) {
        const selector = suggestion.closest('[data-transfer-v2-school-selector]');
        selectSchoolOption(selector, libraryOptionForResult(selector, suggestion));
        return;
      }
      const remove = event.target.closest('[data-transfer-v2-remove-target]');
      if (!remove || !list) return;
      const rows = list.querySelectorAll('[data-transfer-v2-target-row]');
      if (rows.length > 1) remove.closest('[data-transfer-v2-target-row]')?.remove();
    });
    create?.querySelector('form')?.addEventListener('submit', (event) => {
      const submitter = event.submitter || create.querySelector('[data-transfer-v2-create]');
      if (submitter) submitter.disabled = true;
    });
    document.addEventListener('click', (event) => {
      schoolInteractionRoot.querySelectorAll('[data-transfer-v2-school-selector]').forEach((selector) => {
        if (!selector.contains(event.target)) closeSuggestions(selector);
      });
    });
  }

  const workspace = document.querySelector('[data-transfer-v2-workspace]');
  workspace?.addEventListener('submit', (event) => {
    const form = event.target.closest('[data-transfer-v2-missing-override]');
    if (!form) return;
    const message = form.getAttribute('data-transfer-v2-confirm-message') ||
      '确认在材料尚未齐全的情况下继续学校申请？';
    if (!window.confirm(message)) {
      event.preventDefault();
      event.stopPropagation();
    }
  }, true);
  workspace?.addEventListener('click', (event) => {
    const tab = event.target.closest('[data-transfer-v2-tab]');
    if (!tab) return;
    const key = tab.dataset.transferV2Tab;
    workspace.querySelectorAll('[data-transfer-v2-tab]').forEach((item) => {
      item.setAttribute('aria-selected', String(item === tab));
    });
    workspace.querySelectorAll('[data-transfer-v2-panel]').forEach((panel) => {
      panel.hidden = panel.dataset.transferV2Panel !== key;
    });
  });
  if (workspace && location.hash.startsWith('#transfer-v2-')) {
    const panel = document.querySelector(location.hash);
    const key = panel?.dataset.transferV2Panel;
    workspace.querySelector(`[data-transfer-v2-tab="${key}"]`)?.click();
    const blocker = new URLSearchParams(location.search).get('highlight');
    if (blocker) workspace.dataset.transferV2Highlight = blocker;
  }
})();
