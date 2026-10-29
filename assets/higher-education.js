(function () {
  "use strict";

  const workspace = document.querySelector("[data-higher-education-workspace]");
  if (!workspace) return;

  const parseSource = (selector) => {
    const node = workspace.querySelector(selector);
    if (!node) return [];
    try {
      return JSON.parse(node.textContent || "[]");
    } catch (_error) {
      return [];
    }
  };

  const courseLevelLabel = (value) => ({
    Foundation: "预科", Diploma: "文凭课程", Bachelor: "本科", Master: "硕士",
    Postgraduate: "研究生", Package: "打包课程"
  })[value] || value;

  const pathways = parseSource("[data-he-pathway-source]");
  const courses = parseSource("[data-he-course-source]");

  workspace.querySelectorAll("[data-he-university-form]").forEach((form) => {
    const university = form.querySelector("[data-he-university-select]");
    const pathway = form.querySelector("[data-he-pathway-select]");
    const courseInput = form.querySelector("[data-he-course-input]");
    const courseList = form.querySelector("[data-he-course-list]");
    const courseLevel = form.querySelector("[data-he-course-level]");
    if (!university || !pathway || !courseInput || !courseList || !courseLevel) return;

    const refresh = () => {
      const universityId = Number(university.value || 0);
      pathway.replaceChildren(new Option(
        universityId ? "请选择可用的升学路径" : "请先选择大学",
        ""
      ));
      pathways
        .filter((item) => Number(item.university_id) === universityId)
        .forEach((item) => pathway.add(new Option(item.name, String(item.id))));
      pathway.disabled = !universityId || pathway.options.length === 1;

      courseList.replaceChildren();
      courses
        .filter((item) => Number(item.university_id) === universityId)
        .forEach((item) => {
          const option = document.createElement("option");
          option.value = item.name;
          option.label = courseLevelLabel(item.level);
          courseList.appendChild(option);
        });
      courseInput.value = "";
      courseInput.disabled = !universityId;
      courseLevel.replaceChildren(new Option("请先选择升学路径", ""));
      courseLevel.disabled = true;
    };

    pathway.addEventListener("change", () => {
      const selected = pathways.find((item) => String(item.id) === pathway.value);
      courseLevel.replaceChildren();
      if (selected && selected.final_level) {
        courseLevel.add(new Option(courseLevelLabel(selected.final_level), selected.final_level));
        courseLevel.disabled = false;
      } else {
        courseLevel.add(new Option("请先选择升学路径", ""));
        courseLevel.disabled = true;
      }
    });

    university.addEventListener("change", refresh);
    refresh();
  });
})();
