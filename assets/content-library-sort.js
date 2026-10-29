(function () {
  const qa = (selector, root = document) => Array.from(root.querySelectorAll(selector));

  qa("[data-content-sort-control]").forEach((control) => {
    control.addEventListener("change", () => {
      const form = control.form;
      if (!form) return;
      if (form.elements.page) form.elements.page.value = "1";
      form.requestSubmit();
    });
  });
}());
