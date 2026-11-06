/* Isolated-development local PDF enhancement. The existing confirmation form owns all writes. */
(() => {
  const display = (input, value) => input.hasAttribute('data-date-input')
    ? String(value).replace(/^(\d{4})-(\d{2})-(\d{2})$/, '$3/$2/$1') : String(value);
  const install = (card) => {
    if (card.dataset.localReviewInstalled) return;
    card.dataset.localReviewInstalled = 'true';
    const select = card.querySelector('[data-document-type-select]');
    if (!select) return;
    const dirty = new WeakSet();
    let notifying = false;
    card.querySelectorAll('[data-extraction-field]').forEach(input => {
      input.addEventListener('input', () => { if (!notifying) dirty.add(input); });
      input.addEventListener('change', () => { if (!notifying) dirty.add(input); });
    });
    let generation = 0;
    let timer;
    const status = document.createElement('p');
    status.setAttribute('data-local-review-status', '');
    status.setAttribute('role', 'status');
    (card.querySelector('[data-extraction-review]') || card).appendChild(status);
    const apply = (input, row, candidate, explicit = false) => {
      input.value = display(input, candidate.value);
      row.dataset.sourcePage = String(candidate.page || 1);
      row.dataset.sourceSnippet = candidate.snippet || candidate.quote || '';
      const quote = row.querySelector('blockquote');
      if (quote) quote.textContent = row.dataset.sourceSnippet;
      const label = row.querySelector('.extraction-status');
      if (label) label.textContent = '本机识别候选，请核对原文';
      // Notify existing handlers without turning automatic fills into human edits.
      if (explicit) dirty.add(input);
      notifying = true;
      try {
        input.dispatchEvent(new Event('input', {bubbles:true}));
        input.dispatchEvent(new Event('change', {bubbles:true}));
      } finally { notifying = false; }
    };
    const buttonFor = (input, row, candidate) => {
      let button = row.querySelector('[data-local-candidate]');
      if (!button) {
        button = document.createElement('button');
        button.type = 'button';
        button.className = 'secondary mini';
        button.setAttribute('data-local-candidate', '');
        (row.querySelector('.extraction-tools') || row).appendChild(button);
      }
      button.textContent = '使用本机候选：' + candidate.value;
      button.title = candidate.snippet || candidate.quote || '';
      button.onclick = () => {apply(input,row,candidate,true);button.remove();};
    };
    // Conflicts already present at first render use exactly the same explicit selection path.
    card.querySelectorAll('[data-local-candidate]').forEach(button => {
      const row = button.closest('[data-extraction-field-row]');
      const input = row?.querySelector('[data-extraction-field]');
      if (input) buttonFor(input,row,{value:button.dataset.value,snippet:button.dataset.snippet,page:button.dataset.page});
    });
    const start = (clearCandidates = true) => {
      const token = ++generation;
      clearTimeout(timer);
      if (clearCandidates) card.querySelectorAll('[data-local-candidate]').forEach(b=>b.remove());
      const chosen = select.value;
      let attempts = 0;
      const poll = async () => {
        try {
          const url = new URL(card.dataset.localReviewUrl, location.href);
          url.searchParams.set('document_type', chosen);
          const response = await fetch(url, {credentials:'same-origin', cache:'no-store',headers:{Accept:'application/json'}});
          if (!response.ok) throw new Error('unavailable');
          const result = await response.json();
          if (token !== generation || select.value !== chosen || !card.isConnected) return;
          if (result.status === 'queued' || result.status === 'running') {
            status.textContent = result.status === 'queued' ? '本机识别排队中；可先核对或填写字段。' : '本机识别中；可先核对或填写字段。';
            if (++attempts < 240) timer = setTimeout(poll, 2000);
            else status.textContent = '识别等待较久，请人工核对填写。';
            return;
          }
          if (result.status !== 'completed') throw new Error('manual');
          Object.entries(result.fields || {}).forEach(([field,candidate]) => {
            const reference = Array.from(card.querySelectorAll('[data-local-informational-field]')).find(i=>i.dataset.localInformationalField===field);
            if (reference && candidate && typeof candidate.value === 'string') {
              reference.textContent = candidate.value;
              const row = reference.closest('[data-extraction-field-row]');
              if (row) {
                row.dataset.sourceSnippet = candidate.snippet || candidate.quote || '';
                row.dataset.sourcePage = String(candidate.page || 1);
                const quote = row.querySelector('blockquote');
                if (quote) quote.textContent = row.dataset.sourceSnippet;
              }
              return;
            }
            const input = Array.from(card.querySelectorAll('[data-extraction-field]')).find(i=>i.name.match(/^extract_(.+)_\d+$/)?.[1]===field);
            if (!input || dirty.has(input) || !candidate || typeof candidate.value !== 'string' || !candidate.value) return;
            const row = input.closest('[data-extraction-field-row]');
            if (!row) return;
            if (!input.value.trim()) apply(input,row,candidate);
            else if (input.value !== display(input,candidate.value)) buttonFor(input,row,candidate);
          });
          status.textContent = '本机识别完成，请核对原文；已有填写内容已保留。';
        } catch (_) {
          if (token === generation) status.textContent = '本机识别暂不可用，请人工核对填写。';
        }
      };
      poll();
    };
    select.addEventListener('change', () => start(true));
    start(false);
  };
  document.querySelectorAll('[data-local-review-url]').forEach(install);
})();
