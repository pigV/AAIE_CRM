"""Read-only compatibility guidance for retired External Intake URLs."""

from __future__ import annotations

from html import escape


def render_external_intake(
    kind, *, csrf="", values=None, resolution=None, staff_users=(),
):
    """Render guidance only; this compatibility surface has no write form."""

    if kind not in {"transfer", "progression"}:
        raise ValueError("kind")
    label = "转学" if kind == "transfer" else "升学"
    return f'''
    <link rel="stylesheet" href="/assets/student-lifecycle-phase6.css?v=1">
    <main class="external-intake-page external-intake-compatibility"
          data-intake-kind="{escape(kind)}">
      <section class="panel notice info">
        <div class="panel-head"><div>
          <span class="eyebrow">Student Lifecycle</span>
          <h1>External {escape(kind.title())} Intake 已停用</h1>
          <p>此旧入口仅保留兼容说明，不会建立 Student、Case 或 Lifecycle。</p>
        </div></div>
        <ol>
          <li>先查找并复用现有 Student；若确为新学生，请先完成学生建档或家长表单。</li>
          <li>进入 Student Profile。</li>
          <li>点击「建立{escape(label)}记录」，由该操作事务性建立 Case + Active Lifecycle。</li>
        </ol>
        <div class="row-actions">
          <a class="primary button-link" href="/students">查找 Student</a>
          <a class="secondary button-link" href="/students/new">新建 Student</a>
          <a class="secondary button-link" href="/parent-links">生成家长表单链接</a>
        </div>
      </section>
    </main>'''
