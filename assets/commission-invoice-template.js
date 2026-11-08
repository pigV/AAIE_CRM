(() => {
  "use strict";
  const form = document.querySelector("[data-invoice-template]");
  if (!form) return;
  const list = form.querySelector("[data-block-list]");
  const preset = form.querySelector("[data-preset]");
  const referenceOrder = ["issuer","invoice","bill_to","items","totals","footer","payment"];
  preset.addEventListener("change", () => {
    if (preset.value === "invoice_1762") {
      for (const kind of referenceOrder) list.append(list.querySelector(`[data-block="${kind}"]`));
    }
  });
  let dragged = null;

  function move(node, direction) {
    const sibling = direction < 0 ? node.previousElementSibling : node.nextElementSibling;
    if (sibling) node.parentElement.insertBefore(direction < 0 ? node : sibling, direction < 0 ? sibling : node);
  }

  list.addEventListener("dragstart", event => {
    const handle = event.target.closest("[data-drag]");
    if (!handle) return;
    dragged = handle.closest("[data-block]");
    event.dataTransfer.setData("text/plain", dragged.dataset.block);
    event.dataTransfer.effectAllowed = "move";
    dragged.classList.add("is-dragging");
  });
  list.addEventListener("dragover", event => {
    if (!dragged || !event.target.closest("[data-block]")) return;
    event.preventDefault();
    event.dataTransfer.dropEffect = "move";
  });
  list.addEventListener("drop", event => {
    const target = event.target.closest("[data-block]");
    if (!dragged || !target) return;
    event.preventDefault();
    const rect = target.getBoundingClientRect();
    if (target !== dragged) {
      const after = event.clientY >= rect.top + rect.height / 2;
      list.insertBefore(dragged, after ? target.nextElementSibling : target);
      preset.value = "block_flow";
    }
    dragged.classList.remove("is-dragging");
    dragged = null;
  });
  list.addEventListener("dragend", () => {
    if (dragged) dragged.classList.remove("is-dragging");
    dragged = null;
  });

  function addField(block) {
    const select = block.querySelector("[data-field-select]");
    const fields = block.querySelector("[data-fields]");
    const key = select.value;
    if (Array.from(fields.children).some(field => field.dataset.field === key)) return;
    const label = select.selectedOptions[0].textContent;
    const field = document.createElement("li");
    field.className = "cit-field";
    field.dataset.field = key;
    const name = document.createElement("span");
    name.textContent = label;
    const inputLabel = document.createElement("label");
    inputLabel.textContent = "显示标签";
    const input = document.createElement("input");
    input.dataset.label = "";
    input.maxLength = 50;
    input.value = label;
    inputLabel.append(input);
    const actions = document.createElement("div");
    actions.className = "cit-tools";
    for (const [title, key, value, aria] of [
      ["↑", "moveField", "-1", "上移字段"], ["↓", "moveField", "1", "下移字段"],
      ["移除", "removeField", "", "移除字段"]
    ]) {
      const button = document.createElement("button");
      button.type = "button";
      button.textContent = title;
      button.dataset[key] = value;
      button.setAttribute("aria-label", aria);
      actions.append(button);
    }
    field.append(name, inputLabel, actions);
    fields.append(field);
    input.focus();
  }

  list.addEventListener("click", event => {
    const button = event.target.closest("button");
    if (!button) return;
    if ("moveBlock" in button.dataset) {
      move(button.closest("[data-block]"), Number(button.dataset.moveBlock));
      preset.value = "block_flow";
    }
    if ("moveField" in button.dataset) move(button.closest("[data-field]"), Number(button.dataset.moveField));
    if ("removeField" in button.dataset) button.closest("[data-field]").remove();
    if ("addField" in button.dataset) addField(button.closest("[data-block]"));
  });

  form.addEventListener("submit", () => {
    const blocks = Array.from(list.children).map(block => ({
      kind: block.dataset.block,
      heading: block.querySelector("[data-heading]").value,
      fields: Array.from(block.querySelector("[data-fields]").children).map(field => ({
        key: field.dataset.field,
        label: field.querySelector("[data-label]").value
      }))
    }));
    const style = Object.fromEntries(Array.from(form.querySelectorAll("[data-style]")).map(input => [
      input.dataset.style, input.type === "number" ? Number(input.value) : input.value
    ]));
    form.querySelector("[data-layout]").value = JSON.stringify({
      blocks, style, preset: preset.value, footer: form.querySelector("[data-footer]").value
    });
  });
})();
