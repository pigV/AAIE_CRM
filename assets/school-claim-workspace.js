document.querySelectorAll("[data-school-claim-picker]").forEach((form) => {
    const candidates = [...form.querySelectorAll("input[data-claim-net]")];
    const review = form.querySelector('button[type="submit"]');
    const count = form.querySelector("[data-school-claim-count]");
    const currency = new Intl.NumberFormat("en-US", { style: "currency", currency: "AUD" });
    const amounts = [
        ["claimNet", form.querySelector('[data-school-claim-total="net_cents"]')],
        ["claimGst", form.querySelector('[data-school-claim-total="gst_cents"]')],
        ["claimGross", form.querySelector('[data-school-claim-total="gross_cents"]')],
    ];

    function refresh() {
        const selected = candidates.filter((candidate) => candidate.checked);
        count.textContent = `${selected.length} 项`;
        for (const [field, output] of amounts) {
            const cents = selected.reduce((total, candidate) => total + Number(candidate.dataset[field]), 0);
            output.textContent = currency.format(cents / 100);
        }
        review.disabled = selected.length === 0;
    }

    form.addEventListener("change", refresh);
    window.addEventListener("pageshow", refresh);
    refresh();
});
