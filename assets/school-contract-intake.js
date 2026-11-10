(() => {
    const form = document.querySelector("[data-school-contract-intake]");
    if (!form) return;

    const search = form.querySelector("[data-school-search]");
    const school = form.elements.institution;
    const upload = form.querySelector("[data-contract-upload-fields]");
    const noResults = form.querySelector("[data-school-no-results]");
    const dropZone = form.querySelector("[data-school-contract-drop]");
    const file = form.elements.contract_file;
    const status = dropZone.querySelector("[data-file-drop-summary]");
    const describeFile = () => {
        const selected = file.files;
        const valid = selected.length === 1 && /\.pdf$/i.test(selected[0].name);
        file.setCustomValidity(selected.length && !valid ? "请选择一份 PDF 合同。" : "");
        status.textContent = valid ? selected[0].name : selected.length ? "请选择一份 PDF 合同。" : "尚未选择文件";
    };
    file.addEventListener("change", describeFile);
    for (const event of ["dragenter", "dragover"]) {
        dropZone.addEventListener(event, (e) => {
            e.preventDefault();
            if (!upload.disabled) dropZone.classList.add("is-dragover");
        });
    }
    dropZone.addEventListener("dragleave", () => dropZone.classList.remove("is-dragover"));
    dropZone.addEventListener("drop", (e) => {
        e.preventDefault();
        dropZone.classList.remove("is-dragover");
        if (upload.disabled) return;
        const dropped = e.dataTransfer?.files;
        if (!dropped || dropped.length !== 1 || !/\.pdf$/i.test(dropped[0].name)) {
            file.value = "";
            file.setCustomValidity("请拖入一份 PDF 合同。");
            status.textContent = "请拖入一份 PDF 合同。";
            return;
        }
        file.files = dropped;
        describeFile();
    });
    describeFile();
    const groups = Array.from(school.querySelectorAll("optgroup"), (group) => ({
        group,
        options: Array.from(group.querySelectorAll("option")),
    }));

    function updateUpload() {
        upload.hidden = !school.value;
        upload.disabled = upload.hidden;
    }

    function filterSchools() {
        const selection = school.value;
        const keyword = search.value.trim().toLowerCase();
        let matches = 0;
        for (const {group, options} of groups) {
            const matching = options.filter((option) => option.textContent.toLowerCase().includes(keyword));
            group.replaceChildren(...matching);
            group.hidden = matching.length === 0;
            matches += matching.length;
        }
        school.value = selection;
        if (!school.value) school.selectedIndex = 0;
        noResults.hidden = matches !== 0;
        updateUpload();
    }

    search.addEventListener("input", filterSchools);
    school.addEventListener("change", updateUpload);
    filterSchools();
})();
