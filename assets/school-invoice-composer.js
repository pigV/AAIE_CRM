document.querySelectorAll("[data-invoice-composer]").forEach((form) => {
    const rows = Array.from(form.querySelectorAll("[data-invoice-manual-row]"));
    const add = form.querySelector("[data-invoice-manual-add]");
    const totals = form.querySelector("[data-invoice-totals]");
    const currency = new Intl.NumberFormat("en-US", { style: "currency", currency: "AUD" });

    function updateDates(checked) {
        const manualTerms = form.elements.namedItem("manual_terms");
        const claimTerms = form.querySelector("[data-invoice-claim-terms]");
        const issueDate = form.querySelector("[data-invoice-date]");
        const dueDate = form.querySelector("[data-invoice-due]");
        const days = checked.length ? [...new Set(checked.map((row) => row.dataset.invoiceTerms))] : [manualTerms?.value];
        const valid = days.length === 1 && ["0", "7", "15", "30"].includes(days[0]);
        if (manualTerms) manualTerms.closest("label").hidden = checked.length > 0;
        claimTerms.hidden = !checked.length;
        claimTerms.textContent = valid ? `付款期限 · Net ${days[0]}（CRM 申领）` : "请核对所选申领的账期";
        if (valid) {
            const due = new Date(`${issueDate.dataset.invoiceDate}T00:00:00Z`);
            due.setUTCDate(due.getUTCDate() + Number(days[0]));
            dueDate.textContent = due.toISOString().slice(0, 10).split("-").reverse().join("/");
        } else {
            dueDate.textContent = "以预览为准";
        }
    }

    function updateTotals() {
        let net = 0;
        let gst = 0;
        const checked = [...form.querySelectorAll("input[data-invoice-net]:checked")];
        checked.forEach((claim) => {
            net += Number(claim.dataset.invoiceNet);
            gst += Number(claim.dataset.invoiceGst);
        });
        rows.filter((row) => !row.hidden).forEach((row) => {
            const fields = Object.fromEntries(
                [...row.querySelectorAll("input, select")].map((input) => [input.name.replace(/_\d+$/, ""), input])
            );
            const amount = row.querySelector("[data-invoice-line-net]");
            const gross = row.querySelector("[data-invoice-line-gross]");
            amount.textContent = currency.format(0);
            gross.textContent = currency.format(0);
            if (!fields.manual_product.value.trim() || !fields.manual_gst.value ||
                !fields.manual_quantity.checkValidity() || !fields.manual_unit_price.checkValidity()) return;
            const netCents = Number(fields.manual_quantity.value) *
                Math.round(Number(fields.manual_unit_price.value) * 100);
            const lineGst = fields.manual_gst.value === "taxable" ? Math.round(netCents / 10) : 0;
            amount.textContent = currency.format(netCents / 100);
            gross.textContent = currency.format((netCents + lineGst) / 100);
            net += netCents;
            gst += lineGst;
        });
        totals.querySelector("[data-invoice-total-net]").textContent = currency.format(net / 100);
        totals.querySelector("[data-invoice-total-gst]").textContent = currency.format(gst / 100);
        totals.querySelector("[data-invoice-total-gross]").textContent = currency.format((net + gst) / 100);
        updateDates(checked);
    }

    function refresh() {
        if (add) add.disabled = rows.every((row) => !row.hidden);
        updateTotals();
    }

    add?.addEventListener("click", () => {
        const row = rows.find((item) => item.hidden);
        if (row) {
            row.hidden = false;
            row.querySelector("input").focus();
        }
        refresh();
    });
    function resetRow(row) {
        row.querySelectorAll("input").forEach((input) => {
            input.value = input.name.startsWith("manual_quantity_") ? "1" : "";
        });
        row.querySelectorAll("select").forEach((select) => { select.selectedIndex = 0; });
        row.hidden = true;
    }

    rows.forEach((row) => {
        row.querySelector("[data-invoice-manual-remove]").addEventListener("click", () => {
            resetRow(row);
            add.focus();
            refresh();
        });
    });
    form.querySelector("[data-invoice-clear]")?.addEventListener("click", () => {
        rows.forEach(resetRow);
        form.querySelectorAll("input[data-invoice-net]:checked").forEach((claim) => { claim.checked = false; });
        rows[0].hidden = false;
        refresh();
        rows[0].querySelector("input").focus();
    });
    form.querySelector("[data-invoice-select-claim]")?.addEventListener("click", () => {
        form.querySelector("input[data-invoice-net]:not(:checked), input[data-invoice-net]")?.focus();
    });
    form.addEventListener("input", updateTotals);
    form.addEventListener("change", updateTotals);
    refresh();
});

document.querySelectorAll("[data-school-picker]").forEach((picker) => {
    const search = picker.querySelector("[data-school-search]");
    const contract = picker.querySelector("[data-school-contract]");
    const rows = [...picker.querySelectorAll("[data-school-row]")];
    const empty = picker.querySelector("[data-school-no-results]");
    function filterSchools() {
        const query = search.value.trim().toLocaleLowerCase();
        rows.forEach((row) => {
            const name = row.querySelector("a").firstChild.textContent.trim().toLocaleLowerCase();
            row.hidden = !name.includes(query) || (contract.value !== "" && contract.value !== row.dataset.contract);
        });
        if (empty) empty.hidden = rows.some((row) => !row.hidden);
    }
    document.addEventListener("click", (event) => {
        if (picker.open && !picker.contains(event.target)) picker.open = false;
    });
    picker.addEventListener("toggle", () => {
        if (picker.open) search.focus();
    });
    picker.addEventListener("keydown", (event) => {
        if (event.key === "Escape" && picker.open) {
            picker.open = false;
            picker.querySelector("summary").focus();
        }
    });
    search.addEventListener("input", filterSchools);
    contract.addEventListener("change", filterSchools);
});

document.querySelectorAll("[data-invoice-services]").forEach((source) => {
    let services = JSON.parse(source.textContent);
    const composer = source.closest(".invoice-composer");
    const dialog = composer.querySelector(".invoice-service-directory");
    const fields = composer.querySelector("[data-invoice-composer]");
    const pickers = [...composer.querySelectorAll("[data-invoice-service-select]")];
    let activeProduct = null;

    function refreshPickers() {
        pickers.forEach((picker) => {
            picker.replaceChildren(new Option("选择服务", ""), ...services.map((item) => new Option(item.name, item.uid)));
            if (dialog) {
                picker.add(new Option("＋ 新增通用服务", "new"));
                picker.add(new Option("管理产品／服务", "manage"));
            }
            picker.hidden = false;
        });
    }

    function selectService(input, service) {
        input.value = service.name;
        input.dispatchEvent(new Event("input", { bubbles: true }));
        input.focus();
    }

    refreshPickers();
    pickers.forEach((picker) => picker.addEventListener("change", () => {
        const choice = picker.value;
        picker.value = "";
        const input = picker.closest(".invoice-product-selector").querySelector("input");
        if (dialog && (choice === "new" || choice === "manage")) {
            activeProduct = choice === "new" ? input : null;
            form.hidden = true;
            status.textContent = "";
            refresh();
            dialog.showModal();
            if (activeProduct) edit();
            return;
        }
        const service = services.find((item) => item.uid === choice);
        if (service) selectService(input, service);
    }));

    fields?.addEventListener("input", (event) => {
        const input = event.target;
        if (!input.matches("input[name^='manual_product_']")) return;
        const service = services.find((item) => item.name === input.value.trim());
        const description = input.closest("tr").querySelector("input[name^='manual_description_']");
        if (!description.value.trim() || description.value === description.dataset.serviceDefault) {
            description.value = service?.description || "";
            description.dataset.serviceDefault = description.value;
        }
    });
    if (!dialog) return;
    const filter = dialog.querySelector("[data-invoice-service-filter]");
    const list = dialog.querySelector("[data-invoice-service-rows]");
    const form = dialog.querySelector("[data-invoice-service-form]");
    const status = dialog.querySelector("[data-invoice-service-status]");

    function edit(item = {}) {
        form.elements.namedItem("service_uid").value = item.uid || "";
        form.elements.namedItem("service_revision").value = item.revision || "";
        form.elements.namedItem("service_name").value = item.name || "";
        form.elements.namedItem("service_description").value = item.description || "";
        form.hidden = false;
        status.textContent = "";
        form.elements.namedItem("service_name").focus();
    }

    function refresh() {
        const query = filter.value.trim().toLocaleLowerCase();
        list.replaceChildren(...services.filter((item) =>
            `${item.name} ${item.description}`.toLocaleLowerCase().includes(query)
        ).map((item) => {
            const row = document.createElement("tr");
            row.insertCell().textContent = item.name;
            row.insertCell().textContent = item.description;
            const button = document.createElement("button");
            button.type = "button";
            button.textContent = "编辑";
            button.setAttribute("aria-label", `编辑 ${item.name}`);
            button.addEventListener("click", () => edit(item));
            row.insertCell().append(button);
            return row;
        }));
        if (!list.rows.length) {
            const cell = list.insertRow().insertCell();
            cell.colSpan = 3;
            cell.textContent = query ? "未找到匹配的项目。" : "还没有产品或服务。";
        }
    }

    dialog.querySelector("[data-invoice-service-close]").addEventListener("click", () => dialog.close());
    dialog.querySelector("[data-invoice-service-new]").addEventListener("click", () => edit());
    dialog.addEventListener("close", () => { activeProduct = null; });
    filter.addEventListener("input", refresh);
    form.addEventListener("submit", async (event) => {
        event.preventDefault();
        if (form.dataset.saving === "true") return;
        form.dataset.saving = "true";
        const button = form.querySelector("button[type=submit]");
        button.disabled = true;
        const destination = activeProduct;
        const serviceName = form.elements.namedItem("service_name").value.trim();
        try {
            const response = await fetch(form.action, {
                method: "POST",
                headers: { Accept: "application/json" },
                body: new URLSearchParams(new FormData(form)),
            });
            const result = await response.json();
            if (result.token) form.elements.namedItem("idempotency_key").value = result.token;
            if (!response.ok) throw new Error(result.error || "未能保存产品／服务。");
            services = result.items;
            refreshPickers();
            form.hidden = true;
            filter.value = "";
            refresh();
            status.textContent = "已保存到通用目录。";
            if (destination && dialog.open && activeProduct === destination) {
                const saved = services.find((item) => item.name === serviceName);
                dialog.close();
                if (saved) selectService(destination, saved);
            }
        } catch (error) {
            status.textContent = error.message || "未能保存产品／服务。";
        } finally {
            form.dataset.saving = "false";
            button.disabled = false;
        }
    });
});
