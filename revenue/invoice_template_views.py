"""Safe, form-based editor for the commission invoice template layout."""

from html import escape
from urllib.parse import urlencode

from revenue.invoice_templates import FIELD_LABELS, HEADINGS, LABELS


URL = "/revenue/commission-invoice-templates"
BLOCK_NAMES = {"issuer":"AAIE 抬头","invoice":"发票与日期","bill_to":"学校账单信息",
               "items":"佣金明细","totals":"金额汇总","payment":"付款说明","footer":"页脚"}


def _value(value):
    return escape(str(value),quote=True)


def _hidden(name,value):
    return f'<input type="hidden" name="{_value(name)}" value="{_value(value)}">'


def _security(csrf,token,suffix):
    return _hidden("csrf",csrf)+_hidden("idempotency_key",f"commission-template:{token}:{suffix}")


def _option(key,label,selected):
    return f'<option value="{_value(key)}"{" selected" if key==selected else ""}>{escape(label)}</option>'


def _field(field, locked=False):
    key=field["key"]
    return f"""<li class="cit-field" data-field="{_value(key)}">
      <span>{escape(FIELD_LABELS.get(key,LABELS[key]))}</span>
      <label>显示标签<input data-label maxlength="50" value="{_value(field["label"])}"></label>
      <div class="cit-tools">
        <button type="button" data-move-field="-1" aria-label="上移字段">↑</button>
        <button type="button" data-move-field="1" aria-label="下移字段">↓</button>
        {'' if locked else '<button type="button" data-remove-field aria-label="移除字段">移除</button>'}
      </div></li>"""


def render_commission_invoice_templates(data,csrf,token, *, entity_id=None):
    scope = {"entity_id":entity_id} if entity_id else {}
    suffix = "?"+urlencode(scope) if scope else ""
    layout=data["layout"]
    style=layout["style"]
    active=data["active_uid"]
    uid=data["selected_uid"]
    cards=[]
    for block in layout["blocks"]:
        kind=block["kind"]
        fields="".join(_field(field,kind=="items") for field in block["fields"])
        add=""
        if kind not in {"items","footer"}:
            options="".join(_option(k,v,"") for k,v in FIELD_LABELS.items())
            add=f'<div class="cit-insert"><label>CRM 字段<select data-field-select>{options}</select></label><button type="button" data-add-field>插入字段</button></div>'
        footer = f'<label>页脚文案<textarea data-footer maxlength="240">{escape(layout["footer"])}</textarea></label>' if kind=="footer" else ""
        cards.append(f"""<article class="cit-block" data-block="{kind}">
          <header><button type="button" draggable="true" data-drag aria-label="拖动{BLOCK_NAMES[kind]}">⠿</button>
            <h3>{BLOCK_NAMES[kind]}</h3><div class="cit-tools">
              <button type="button" data-move-block="-1" aria-label="上移区块">↑</button>
              <button type="button" data-move-block="1" aria-label="下移区块">↓</button></div></header>
          <details><summary>配置区块</summary>
            <label>区块标题<input data-heading maxlength="50" value="{_value(block["heading"])}"></label>
            <ul data-fields>{fields}</ul>{add}{footer}
          </details></article>""")

    colors=_value(style["accent"])
    preset=layout.get("preset","block_flow")
    presets="".join(_option(k,v,preset) for k,v in (("invoice_1762","Invoice 1762 原件版式"),("block_flow","自由区块排列")))
    fonts="".join(_option(k,v,style["font"]) for k,v in (("unicode","无衬线 / Unicode"),("serif","Times Serif")))
    aligns="".join(_option(k,v,style["alignment"]) for k,v in (("left","左对齐"),("center","居中"),("right","右对齐")))
    numbers="".join(f'<label>{label}<input data-style="{key}" type="number" min="{lo}" max="{hi}" value="{style[key]}" required></label>'
                    for key,label,lo,hi in (("size","正文大小",8,14),("spacing","区块间距",4,24),("margin","页边距",16,64)))
    logo_notice = "当前版本含 Logo" if layout.get("logo") else "当前版本无 Logo"
    formal = data.get("formal",False)
    notice = "隔离测试 · " if data.get("simulation",True) else ""
    approval = ('<label><input type="checkbox" name="approved" value="1" required>'
                '已核对并批准开票主体、学校地址及收款账户资料</label>') if formal else ""
    profile_label = "正式开票资料" if formal else "隔离测试财务资料"
    profile_note = "核对正式开票资料后确认批准。" if formal else "填写合成数据或待确认占位。正式 ABN、银行账户须经财务确认。"
    profile_inputs="".join(
        f'<label>{escape(FIELD_LABELS[key])}<input name="{key}" maxlength="180" required value="{_value(value)}"></label>'
        for key,value in data["profile"].items()
    )
    versions=[]
    for version in data["versions"]:
        version_uid=version["uid"]
        href=URL+"?"+urlencode({**scope,"version":version_uid})
        status="当前发布" if version_uid==active else "历史发布" if version_uid in data["published"] else "草稿"
        versions.append(f'<li><a href="{_value(href)}">{escape(version_uid[-10:])}</a> · {status} · {escape(version["created_at"])}</li>')
    preview=[]
    for sample in data["samples"]:
        href=URL+"/preview?"+urlencode({**scope,"invoice":sample["uid"],"version":uid})
        preview.append(f'<li><a target="_blank" rel="noopener" href="{_value(href)}">{escape(sample["invoice_number"])} · PDF 预览</a></li>')
    if formal:
        sample_href = URL+"/preview?"+urlencode({**scope,"sample":"1","version":uid})
        preview.insert(0,f'<li><a target="_blank" rel="noopener" href="{_value(sample_href)}">合成样张 · PDF 预览</a></li>')
    actions=""
    if uid and uid!=active:
        state=_hidden("version_uid",uid)+_hidden("expected_active",active)
        actions=f'<form method="post" action="{URL}/publish{suffix}">{_security(csrf,token,"publish")}{state}<button class="primary">发布所选版本</button></form>'
        if uid in data["published"]:
            actions+=f'<form method="post" action="{URL}/rollback{suffix}">{_security(csrf,token,"rollback")}{state}<button type="submit">回退到所选版本</button></form>'
    return f"""<link rel="stylesheet" href="/assets/commission-invoice-template.css">
    <section class="cit-workspace">
      <header class="cit-title"><p class="cit-kicker">REVENUE · COMMISSION INVOICES</p><h1>佣金发票模板</h1>
        <p>{notice}拖动区块或用上下键按钮调整。保存草稿后可预览版式。</p></header>
      <div class="cit-columns">
        <form method="post" action="{URL}/save{suffix}" enctype="multipart/form-data" data-invoice-template class="cit-editor">
          {_security(csrf,token,"save")}{_hidden("base_uid",uid)}
          <input type="hidden" name="layout_json" data-layout>
          <section class="cit-panel"><h2>版式与样式</h2>
            <label>版式预设<select data-preset>{presets}</select></label>
            <small>拖动区块会切换为自由排列；切回 Invoice 1762 会恢复原件区块顺序。</small>
            <div class="cit-inputs"><label>强调色<input type="color" data-style="accent" value="{colors}"></label>
            <label>字体<select data-style="font">{fonts}</select></label>
            <label>对齐<select data-style="alignment">{aligns}</select></label>{numbers}</div>
            <label>Logo（PNG / JPEG，最大 100 KB）<input type="file" name="template_logo" accept="image/png,image/jpeg"></label>
            <label><input type="checkbox" name="remove_logo" value="1">移除 Logo</label><small>{logo_notice}</small>
          </section>
          <section class="cit-panel"><h2>发票区块</h2><div data-block-list>{''.join(cards)}</div></section>
          <button class="primary" type="submit">保存草稿</button>
        </form>
        <aside class="cit-sidebar">
          <section class="cit-panel"><h2>预览与发布</h2>
            <p>所选 {escape(uid[-10:] or "默认模板")} · 当前发布 {escape(active[-10:] or "未发布")}</p>
            <ul>{''.join(preview)}</ul>{actions}</section>
          <section class="cit-panel"><h2>模板版本</h2><ul>{''.join(versions)}</ul></section>
          <details class="cit-panel"><summary>{profile_label}</summary>
            <p>{profile_note}</p>
            <form method="post" action="{URL}/profile{suffix}">{_security(csrf,token,"profile")}
              {_hidden("return_version",uid)}{_hidden("expected_profile",data["profile_uid"])}{profile_inputs}{approval}
              <button type="submit">保存{profile_label}</button>
            </form>
          </details>
        </aside>
      </div>
    </section><script src="/assets/commission-invoice-template.js" defer></script>"""
