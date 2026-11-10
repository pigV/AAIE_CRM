document.querySelectorAll("[data-commission-gst-review]").forEach((form) => {
  const recalculate = form.querySelector("[data-gst-recalculate]");
  form.addEventListener("change", (event) => {
    if (event.target.name === "gst_mode" && typeof form.requestSubmit === "function") {
      form.requestSubmit(recalculate);
    }
  });
});
