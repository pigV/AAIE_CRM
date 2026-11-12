document.querySelectorAll("[data-payer-email-pattern]").forEach((input) => {
  const error = document.getElementById(input.getAttribute("aria-describedby"));
  const verification = input.form.elements.namedItem("verified");
  const originalAddresses = input.value.toLowerCase().split(",").map((address) => address.trim()).join(",");
  const pattern = new RegExp("^" + input.dataset.payerEmailPattern + "$");

  function validate() {
    const addresses = input.value.split(",").map((address) => address.trim());
    const testRecipient = addresses.findIndex((address) => address.toLowerCase() === input.dataset.payerTestRecipient);
    const invalid = addresses.findIndex((address) => address.length > 254 || !pattern.test(address));
    let message = "";
    if (!input.value.trim()) {
      message = input.required ? "请填写收票邮箱。" : "";
    } else if (testRecipient !== -1) {
      message = "第 " + (testRecipient + 1) + " 个地址是测试专用邮箱，请填写学校收票邮箱。";
    } else if (invalid !== -1) {
      message = "第 " + (invalid + 1) + " 个收票邮箱格式有误。";
    }
    input.setCustomValidity(message);
    input.setAttribute("aria-invalid", message ? "true" : "false");
    error.hidden = !message;
    error.textContent = message;
  }

  input.addEventListener("input", () => {
    const currentAddresses = input.value.toLowerCase().split(",").map((address) => address.trim()).join(",");
    if (currentAddresses !== originalAddresses) verification.checked = false;
    validate();
  });
  input.addEventListener("invalid", validate);
  if (input.value) validate();
});

document.querySelectorAll("[data-use-address-parts]").forEach((toggle) => {
  const parts = toggle.form.querySelector("[data-address-parts]");
  const legacy = toggle.form.elements.namedItem("billing_address");
  function update() {
    parts.hidden = !toggle.checked;
    parts.disabled = !toggle.checked;
    legacy.disabled = toggle.checked;
    legacy.closest("label").hidden = toggle.checked;
  }
  toggle.addEventListener("change", update);
  update();
});
