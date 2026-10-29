(function () {
  "use strict";

  function parseMajorUnits(value) {
    var normalized = String(value || "").trim().replace(/,/g, "");
    var match = normalized.match(/^(\d+)(?:\.(\d{0,2}))?$/);
    if (!match) return null;
    var cents = Number(match[1] + (match[2] || "").padEnd(2, "0"));
    return Number.isSafeInteger(cents) ? cents : null;
  }

  function applyBasisPoints(cents, basisPoints) {
    var numerator = cents * basisPoints + 5000;
    if (!Number.isSafeInteger(numerator)) return null;
    return Math.floor(numerator / 10000);
  }

  function majorUnits(cents) {
    return Math.floor(cents / 100) + "." + String(cents % 100).padStart(2, "0");
  }

  function formatMoney(currency, cents) {
    var sign = cents < 0 ? "-" : "";
    var value = cents < 0 ? -cents : cents;
    var symbol = currency === "AUD" ? "$" : "¥";
    return sign + currency + " " + symbol + Math.floor(value / 100).toLocaleString("en-AU") + "." + String(value % 100).padStart(2, "0");
  }

  function initialize(form) {
    var fee = form.querySelector("[data-fee-item]");
    var method = form.querySelector("[data-payment-method]");
    var currency = form.querySelector("[data-actual-currency]");
    var actualMajor = form.querySelector("[data-actual-major]");
    var actualCents = form.querySelector("[data-actual-cents]");
    var actualLabel = form.querySelector("[data-actual-amount-label]");
    var actualPrefix = form.querySelector("[data-actual-prefix]");
    var baseMajor = form.querySelector("[data-base-major]");
    var baseCents = form.querySelector("[data-base-cents]");
    var outstandingLabel = form.querySelector("[data-outstanding-base]");
    var evidence = form.querySelector('input[name="payment_evidence"]');
    var receivedDate = form.querySelector('input[name="received_date"]');
    var previewBase = form.querySelector("[data-preview-base]");
    var previewGst = form.querySelector("[data-preview-gst]");
    var previewGross = form.querySelector("[data-preview-gross]");
    var previewGstLabel = form.querySelector("[data-preview-gst-label]");
    var errorMessage = form.querySelector("[data-payment-error]");
    var submit = form.querySelector('button[type="submit"], input[type="submit"]');
    var originalSubmitLabel = submit && submit.tagName === "BUTTON" ? submit.textContent : "";

    function selectedRule() {
      var option = method.options[method.selectedIndex];
      return {
        currency: option && option.dataset.currency || "",
        gstBasisPoints: Number(option && option.dataset.gstBasisPoints || 0)
      };
    }

    function selectedOutstanding() {
      var option = fee.options[fee.selectedIndex];
      var cents = Number(option && option.dataset.outstandingBaseCents || 0);
      return Number.isSafeInteger(cents) && cents >= 0 ? cents : 0;
    }

    function clearError() {
      errorMessage.hidden = true;
      errorMessage.textContent = "";
    }

    function showError(message) {
      errorMessage.textContent = message;
      errorMessage.hidden = false;
      errorMessage.setAttribute("tabindex", "-1");
      errorMessage.focus();
    }

    function clearValidity() {
      [fee, method, actualMajor, baseMajor, evidence, receivedDate].forEach(function (field) {
        if (field) field.setCustomValidity("");
      });
    }

    function setComputedValidity(state) {
      fee.setCustomValidity(fee.value ? "" : "请选择收费项目。");
      method.setCustomValidity(method.value ? "" : "请选择付款方式。");
      if (state.base === null || state.base <= 0) {
        baseMajor.setCustomValidity("请输入有效的本次服务费金额。");
      } else if (state.base > state.outstanding) {
        baseMajor.setCustomValidity("本次服务费不能超过当前未收金额 " + formatMoney("AUD", state.outstanding) + "。");
      } else {
        baseMajor.setCustomValidity("");
      }
      if (state.rule.currency === "CNY" && !actualCents.value) {
        actualMajor.setCustomValidity("请输入实际收到的人民币金额。");
      } else if (state.rule.currency === "AUD" && !actualCents.value) {
        actualMajor.setCustomValidity("请输入有效的实际到账澳元金额。");
      } else {
        actualMajor.setCustomValidity("");
      }
    }

    function restoreSubmit() {
      form.dataset.submitting = "false";
      form.removeAttribute("aria-busy");
      if (submit) {
        submit.disabled = false;
        if (submit.tagName === "BUTTON") submit.textContent = originalSubmitLabel;
      }
    }

    function setSubmitting() {
      form.dataset.submitting = "true";
      form.setAttribute("aria-busy", "true");
      if (submit) {
        submit.disabled = true;
        if (submit.tagName === "BUTTON") submit.textContent = "处理中...";
      }
    }

    function recalculate(options) {
      var settings = options || {};
      var rule = selectedRule();
      var outstanding = selectedOutstanding();
      if (settings.resetServiceFee) {
        baseMajor.value = outstanding > 0 ? majorUnits(outstanding) : "";
      }
      if (settings.resetActual) {
        actualMajor.value = "";
        delete actualMajor.dataset.autofilled;
      }
      var base = parseMajorUnits(baseMajor.value);
      var hasMethod = Boolean(rule.currency);
      var isAud = rule.currency === "AUD";
      var gst = base === null ? 0 : applyBasisPoints(base, rule.gstBasisPoints);
      var gross = base === null || gst === null ? 0 : base + gst;

      outstandingLabel.textContent = "当前未收服务费：" + formatMoney("AUD", outstanding);
      currency.value = rule.currency;
      actualLabel.textContent = rule.currency === "CNY" ? "实际收到人民币" : (isAud ? "实际收到澳元" : "实际到账金额");
      actualPrefix.textContent = rule.currency === "CNY" ? "CNY ¥" : (isAud ? "AUD $" : "—");
      actualMajor.readOnly = isAud;
      if (isAud) {
        actualMajor.value = base === null || gst === null ? "" : majorUnits(gross);
        actualMajor.dataset.autofilled = "true";
      } else if (actualMajor.dataset.autofilled === "true") {
        actualMajor.value = "";
        delete actualMajor.dataset.autofilled;
      }
      previewGstLabel.textContent = rule.gstBasisPoints === 1000 ? "GST 10%" : "GST";
      previewBase.textContent = formatMoney("AUD", base === null ? 0 : base);
      previewGst.textContent = formatMoney("AUD", gst);
      previewGross.textContent = formatMoney("AUD", gross);

      var parsedActual = hasMethod ? parseMajorUnits(actualMajor.value) : null;
      baseCents.value = base === null || base <= 0 ? "" : String(base);
      actualCents.value = parsedActual === null || parsedActual <= 0 ? "" : String(parsedActual);
      return {base: base, outstanding: outstanding, rule: rule};
    }

    function validatePayment(state) {
      clearError();
      setComputedValidity(state);
      if (!form.checkValidity()) {
        form.reportValidity();
        return false;
      }
      return true;
    }

    fee.addEventListener("change", function () {
      clearValidity();
      clearError();
      setComputedValidity(recalculate({resetServiceFee: true, resetActual: true}));
    });
    method.addEventListener("change", function () {
      clearValidity();
      clearError();
      setComputedValidity(recalculate());
    });
    baseMajor.addEventListener("input", function () {
      clearError();
      setComputedValidity(recalculate());
    });
    actualMajor.addEventListener("input", function () {
      delete actualMajor.dataset.autofilled;
      clearError();
      setComputedValidity(recalculate());
    });
    evidence.addEventListener("change", function () {
      evidence.setCustomValidity("");
      clearError();
    });
    receivedDate.addEventListener("change", function () {
      receivedDate.setCustomValidity("");
      clearError();
    });
    fee.addEventListener("invalid", function () {
      if (!fee.value) fee.setCustomValidity("请选择收费项目。");
    });
    method.addEventListener("invalid", function () {
      if (!method.value) method.setCustomValidity("请选择付款方式。");
    });
    receivedDate.addEventListener("invalid", function () {
      if (!receivedDate.value) receivedDate.setCustomValidity("请选择收款日期。");
    });
    evidence.addEventListener("invalid", function () {
      if (!evidence.value) evidence.setCustomValidity("请选择付款凭证（PNG、JPG/JPEG 或 PDF）。");
    });
    form.addEventListener("submit", async function (event) {
      event.preventDefault();
      event.stopImmediatePropagation();
      if (form.dataset.submitting === "true") return;
      var state = recalculate();
      if (!validatePayment(state)) {
        restoreSubmit();
        return;
      }
      setSubmitting();
      try {
        var response;
        try {
          response = await fetch(form.action, {
            method: "POST",
            body: new FormData(form),
            credentials: "same-origin",
            headers: {"Accept": "application/json", "X-Requested-With": "fetch"}
          });
        } catch (_) {
          throw new Error("网络连接失败，收款未保存，请稍后重试。");
        }
        var data = await response.json().catch(function () { return {}; });
        if (!response.ok || data.ok !== true) {
          throw new Error(data.message || "收款未保存，请检查输入或付款凭证后重试。");
        }
        window.location.assign(data.redirect || "/revenue/billing");
      } catch (error) {
        showError(error && error.message || "收款未保存，请稍后重试。");
        restoreSubmit();
      }
    });
    setComputedValidity(recalculate({ resetServiceFee: true, resetActual: true }));
  }

  document.querySelectorAll("[data-revenue-payment-form]").forEach(initialize);
  window.RevenuePaymentUI = {
    parseMajorUnits: parseMajorUnits,
    applyBasisPoints: applyBasisPoints
  };
}());
