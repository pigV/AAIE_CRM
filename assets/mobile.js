(() => {
  const root = document.querySelector(".mobile-ui");
  if (!root) return;

  const openButtons = Array.from(root.querySelectorAll("[data-mobile-sheet-open]"));
  const sheets = Array.from(root.querySelectorAll("[data-mobile-sheet]"));
  if (!openButtons.length || !sheets.length) return;

  root.dataset.mobileEnhanced = "";
  sheets.forEach((sheet) => { sheet.hidden = true; });

  let activeController = null;
  let previousFocus = null;
  let previousOverflow = "";
  let backgroundState = [];

  const focusableSelector = [
    'a[href]:not([tabindex="-1"])',
    "button:not([disabled])",
    "input:not([disabled])",
    "select:not([disabled])",
    "textarea:not([disabled])",
    '[tabindex]:not([tabindex="-1"])',
  ].join(",");

  const restoreBackground = () => {
    backgroundState.forEach((state) => {
      state.element.inert = state.wasInert;
      if (state.hadInertAttribute) state.element.setAttribute("inert", "");
      else state.element.removeAttribute("inert");
    });
    backgroundState = [];
  };

  const closeSheet = (restoreFocus = true) => {
    if (!activeController) return;
    const { openButton, sheet } = activeController;
    sheet.hidden = true;
    openButton.setAttribute("aria-expanded", "false");
    document.documentElement.style.overflow = previousOverflow;
    restoreBackground();
    activeController = null;
    if (restoreFocus) (previousFocus || openButton).focus();
  };

  const openSheet = (controller) => {
    if (activeController) closeSheet(false);
    const { openButton, sheet, panel } = controller;
    previousFocus = document.activeElement;
    previousOverflow = document.documentElement.style.overflow;
    backgroundState = Array.from(root.children)
      .filter((element) => element !== sheet)
      .map((element) => ({
        element,
        wasInert: Boolean(element.inert),
        hadInertAttribute: element.hasAttribute("inert"),
      }));
    backgroundState.forEach(({ element }) => { element.inert = true; });
    activeController = controller;
    sheet.hidden = false;
    openButton.setAttribute("aria-expanded", "true");
    document.documentElement.style.overflow = "hidden";
    panel.focus();
  };

  openButtons.forEach((openButton) => {
    const sheetId = openButton.getAttribute("aria-controls");
    const sheet = sheets.find((candidate) => candidate.id === sheetId);
    const panel = sheet?.querySelector("[data-mobile-sheet-panel]");
    if (!sheet || !panel) return;
    const controller = { openButton, sheet, panel };
    openButton.addEventListener("click", (event) => {
      event.preventDefault();
      openSheet(controller);
    });
    sheet.addEventListener("click", (event) => {
      if (event.target.closest("[data-mobile-sheet-close]")) {
        event.preventDefault();
        closeSheet();
      }
    });
  });

  document.addEventListener("keydown", (event) => {
    if (!activeController) return;
    const { panel } = activeController;
    if (event.key === "Escape") {
      event.preventDefault();
      closeSheet();
      return;
    }
    if (event.key === "Tab") {
      const focusable = Array.from(panel.querySelectorAll(focusableSelector))
        .filter((element) => element.getAttribute("aria-hidden") !== "true" && !element.closest("[hidden]"));
      if (!focusable.length) {
        event.preventDefault();
        panel.focus();
        return;
      }
      const first = focusable[0];
      const last = focusable[focusable.length - 1];
      const active = document.activeElement;
      if (event.shiftKey && (active === first || active === panel || !panel.contains(active))) {
        event.preventDefault();
        last.focus();
      } else if (!event.shiftKey && (active === last || active === panel || !panel.contains(active))) {
        event.preventDefault();
        first.focus();
      }
    }
  });

  document.addEventListener("focusin", (event) => {
    if (!activeController) return;
    const { panel } = activeController;
    if (event.target !== panel && !panel.contains(event.target)) panel.focus();
  });
})();
