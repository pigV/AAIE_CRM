"""Material Checklist presentation. No database, filesystem or app startup effects."""
from collections import OrderedDict
from html import escape
from urllib.parse import quote, urlencode
from visa_document_processing_ui import document_return_context


def material_upload_target(form, query, available_items):
    """A row shortcut preselects only an item offered by this case's review."""
    key = 'target_material_item'
    source = form if key in (form or {}) else query or {}
    value = (source or {}).get(key, '')
    if isinstance(value, (list, tuple)):
        value = value[0] if value else ''
    value = str(value or '')
    return value if value in {str(item['id']) for item in available_items} else ''


def render_material_workspace(handler, student_id, case, rows, *, panel_id, title,
                              description, show_smart_batch=True, layout="record"):
    """Render effective, initialized rows using existing business/file controls."""
    h = handler
    case_id = int(case['id'])
    panel = escape(panel_id, quote=True)
    form_id = f'visa-material-save-{case_id}'
    active_filter = h.visa_material_current_filter()
    if student_id:
        base = f'/students/{quote(str(student_id))}/visa-cases/{case_id}'
        batch_url = base + '/material-batch-upload?visa_tab=material_checklist'
    else:
        base = f'/visa-management/cases/{case_id}/applicant'
        batch_url = base + '/material-batch-upload?visa_tab=material_checklist'
    counts = {
        'all': len(rows),
        'required_unfinished': sum(i['required_status'] == 'required' and not h.visa_material_is_completed(i) for i in rows),
        'unfinished': sum(not h.visa_material_is_completed(i) for i in rows),
        'uploaded': sum(i['document_status'] == 'uploaded' for i in rows),
        'not_applicable': sum(i['document_status'] == 'not_applicable' for i in rows),
    }
    quick_filters = []
    for key, label in [('required_unfinished', '必交未完成'), ('unfinished', '全部未完成'),
                       ('all', '全部材料'), ('uploaded', '已收到'), ('not_applicable', '不适用')]:
        href = h.visa_case_return_path(str(student_id), case_id, 'material_checklist', panel_id, key)
        if not student_id:
            href = f'/visa-management/cases/{case_id}/workspace?visa_tab=material_checklist&material_filter={key}#{quote(panel_id)}'
        quick_filters.append(
            f'<a class="visa-material-quick-filter" href="{escape(href)}" '
            f'data-quick-filter="{key}" data-count="{counts[key]}" '
            f'aria-current="{"true" if key == active_filter else "false"}">'
            f'<span>{label}</span><strong data-filter-count>{counts[key]}</strong></a>'
        )
    groups = OrderedDict()
    for item in rows:
        groups.setdefault(item['category'], []).append(item)
    categories = ['<button type="button" data-category-choice="" aria-pressed="true">全部分类</button>']
    sections = []
    for index, (category, items) in enumerate(groups.items()):
        category_label = h.visa_material_category_label(category)
        category_id = f'{panel_id}-category-{index}'
        completed = sum(h.visa_material_is_completed(i) for i in items)
        categories.append(
            f'<button type="button" data-category-choice="{escape(str(index))}" aria-pressed="false" '
            f'aria-controls="{escape(category_id)}"><span>{escape(category_label)}</span>'
            f'<small data-category-count="{index}">{completed} / {len(items)}</small></button>'
        )
        item_html = []
        for item in items:
            item_id = int(item['id'])
            row_id = f'{panel_id}-item-{item_id}'
            status = h.visa_material_normalized_document_status(item['document_status'])
            files = (h.visa_material_file_rows(case_id, item_id, item, student_id) if student_id
                     else h.visa_only_material_file_rows(case_id, item_id))
            name = h.visa_material_name_label(item['material_name'])
            owner = h.visa_material_owner_type_label(item['owner_type'])
            purpose = h.visa_material_purpose_label(item['purpose'])
            required = h.visa_material_required_status_label(item['required_status'])
            files_html = document_return_context(h.visa_material_file_area_html(student_id, case_id, item),
                                                 'material_checklist', row_id, active_filter)
            file_names = ' '.join(str(f['original_name'] or '') for f in files)
            search_text = ' '.join([name, str(item['material_name']), owner, purpose, file_names])
            status_select = h.visa_material_status_select_html(item, form_id)
            preview = ''
            if files:
                first = files[0]
                if student_id and h.visa_material_previewable_file(str(first['original_name'] or '')):
                    preview = (f'<button class="secondary compact" type="button" data-preview-open '
                               f'data-preview-src="/documents/{int(first["id"])}/preview?size=large" '
                               f'data-download-url="/documents/{int(first["id"])}/download" '
                               f'data-preview-title="{escape(str(first["original_name"] or name))}">预览</button>')
                elif student_id:
                    preview = f'<a class="secondary button-link compact" href="/documents/{int(first["id"])}/download">下载文件</a>'
                else:
                    preview = (f'<a class="secondary button-link compact" data-material-resume '
                               f'href="/visa-management/cases/{case_id}/documents/{int(first["id"])}/file">查看文件</a>')
            if student_id:
                upload = f'''<details class="visa-material-upload" data-material-upload-details>
                  <summary>上传到此项</summary>
                  <form method="post" enctype="multipart/form-data" action="{base}/material-items/{item_id}/files" data-material-upload>
                    <input type="hidden" name="csrf" value="{escape(h.csrf)}">
                    <input type="hidden" name="visa_tab" value="material_checklist">
                    <input type="hidden" name="return_anchor" value="{escape(row_id)}">
                    <input type="hidden" name="material_filter" value="{escape(active_filter)}">
                    <label>选择{name and escape(name)}文件<input type="file" name="files" accept=".pdf,.doc,.docx,.jpg,.jpeg,.png,.heic" multiple required></label>
                    <button class="primary" type="submit">确认上传</button>
                  </form>
                </details>'''
            else:
                href = batch_url + '&' + urlencode({'target_material_item':item_id})
                upload = f'<a class="secondary button-link compact" data-item-upload data-material-resume href="{escape(href)}">上传到此项</a>'
            hidden = '' if h.visa_material_matches_filter(item, active_filter) else ' hidden'
            item_html.append(f'''
              <article class="visa-material-row visa-material-compact-row" id="{escape(row_id)}"
                data-material-item="{item_id}" data-category-index="{index}" data-status="{escape(status)}"
                data-required="{escape(item['required_status'])}" data-search="{escape(search_text)}"{hidden}>
                <div class="visa-material-item-main">
                  <div class="visa-material-item-label visa-material-item-identity">{('<button type="button" class="visa-material-select" data-material-select="'+str(item_id)+'" aria-pressed="false">') if layout == 'split' else ''}<strong class="visa-material-item-name" data-search-label>{escape(name)}</strong>{'</button>' if layout == 'split' else ''}
                  </div>
                    <div class="visa-material-item-meta visa-material-item-ownership"><span>{escape(owner)}</span><span class="visa-material-requirement">{escape(required)}</span>
                    <span>{len(files)} 个文件</span></div>
                  <label class="visa-material-item-status"><span>当前状态</span>{status_select}</label>
                  <div class="visa-material-item-actions">{upload if layout != "split" else ""}{preview}<button class="secondary compact" type="button" data-open-files="{item_id}">文件与备注</button></div>
                </div>
                {upload if layout == "split" else ""}
                <details class="visa-material-item-details" data-material-details="{item_id}">
                  <summary>展开文件、备注与材料要求</summary>
                  <div class="visa-material-item-detail-content">
                    <p class="visa-material-cell--purpose">{escape(purpose)} · 是否递交移民局：{escape(h.visa_material_upload_to_immi_label(item['upload_to_immi']))}</p>
                    {files_html}
                    <label>备注<textarea name="notes_{item_id}" rows="2" form="{form_id}" aria-label="{escape(name)}备注">{escape(str(item['notes'] or ''))}</textarea></label>
                  </div>
                </details>
              </article>''')
        sections.append(f'''<details class="visa-material-category" id="{escape(category_id)}" data-material-category="{index}" open>
          <summary class="visa-material-category-head"><h3>{escape(category_label)}</h3><span data-group-progress="{index}">{completed} / {len(items)} 已完成</span></summary>
          <div class="visa-material-list">{''.join(item_html)}</div></details>''')
    batch_link = (f'<a class="secondary button-link" data-material-resume href="{escape(batch_url)}">Smart Batch Upload / 智能批量上传</a>'
                  if show_smart_batch else '')
    initialize_link = (f'<form method="post" action="{base}/material-items/init">'
        f'<input type="hidden" name="csrf" value="{escape(h.csrf)}">'
        '<button class="secondary" type="submit">更新材料清单</button></form>'
        if getattr(h, 'business_changes_enabled', lambda: False)() else '')
    extra_options = ''.join(f'<option value="{key}"'+(' selected' if key==active_filter else '')+f'>{label}</option>'
                            for key,label in [('all','全部材料'),('required_unfinished','必交未完成'),('unfinished','全部未完成'),
                                              ('uploaded','已收到'),('not_applicable','不适用'),('missing','未收到'),
                                              ('required','必交'),('conditional','条件材料'),('optional','可选')])
    split_preview = '''          <aside class="visa-material-preview" data-material-preview aria-label="材料原件预览">
            <header class="visa-material-preview-header"><strong data-material-preview-title>选择材料查看原件</strong>
              <div class="row-actions"><a class="secondary button-link compact" data-material-preview-download hidden>下载文件</a>
                <button class="secondary compact" type="button" data-material-preview-fullscreen data-preview-open hidden>全屏预览</button></div></header>
            <div class="visa-material-preview-pagination">
              <button class="secondary compact" type="button" data-material-preview-prev disabled>上一页</button>
              <span data-material-preview-page>— / —</span>
              <button class="secondary compact" type="button" data-material-preview-next disabled>下一页</button>
            </div>
            <div class="visa-material-preview-canvas"><p data-material-preview-message role="status" aria-live="polite">选择左侧材料，在这里对照原始文件。</p>
              <img data-material-preview-image alt="材料原件预览" hidden></div>
            <footer><span data-material-preview-context>状态和备注仍通过原材料清单保存。</span>
              <button class="secondary compact" type="button" data-material-preview-retry hidden>重试预览</button></footer>
          </aside>''' if layout == 'split' else ''
    layout_assets = ('<link rel="stylesheet" href="/assets/visa-case-scheme-b.css?v=1"><script src="/assets/visa-material-split-preview.js?v=1" defer></script>' if layout == 'split' else '<link rel="stylesheet" href="/assets/visa-case-scheme-c.css?v=1">')
    return f'''
      <link rel="stylesheet" href="/assets/visa-material-workspace.css?v=1">
      <script src="/assets/visa-material-workspace.js?v=4" defer></script>
      {layout_assets}
      <section class="panel visa-material-checklist-panel visa-material-workspace {"visa-material-record" if layout != "split" else ""}" id="{panel}" data-visa-material-workspace data-case-id="{case_id}" data-initial-filter="{escape(active_filter)}">
        <div class="panel-head"><div><h2>{escape(title)}</h2><p>{escape(description)}</p></div><div class="row-actions">{initialize_link}{batch_link}</div></div>
        <div class="visa-material-quick-filters">{''.join(quick_filters)}</div>
        <div class="visa-material-workspace-toolbar visa-material-compact-toolbar">
          <label class="visa-material-search-label">搜索材料<input type="search" data-material-search placeholder="护照 / Passport / 文件名" aria-label="搜索材料和文件名"></label>
          <label>材料筛选<select data-material-filter>{extra_options}</select></label>
        <label class="visa-material-category-mobile">材料分类<select data-category-mobile><option value="">全部分类</option>{''.join(f'<option value="{index}">{escape(h.visa_material_category_label(category))}</option>' for index,category in enumerate(groups))}</select></label>
          <button type="button" class="secondary" data-material-clear>清除搜索与筛选</button>
        </div>
        <form id="{form_id}" method="post" action="{base}/material-items" data-material-save>
          {getattr(h,'business_change_checklist_hidden_fields',lambda case_id:'')(case_id)}
          <input type="hidden" name="csrf" value="{escape(h.csrf)}"><input type="hidden" name="visa_tab" value="material_checklist">
          <input type="hidden" name="return_anchor" value="{panel}"><input type="hidden" name="material_filter" value="{escape(active_filter)}">
        </form>

        <div class="visa-material-workspace-body"><nav class="visa-material-category-nav" aria-label="材料分类">{''.join(categories)}</nav>
          <div class="visa-material-workspace-list"><p class="visa-material-search-count" data-material-matches role="status" aria-live="polite">共 {len(rows)} 项</p>
            {''.join(sections)}<p class="visa-management-empty" data-material-empty hidden>没有匹配材料，请清除搜索或切换筛选。</p>
          </div>
          {split_preview}
        </div>
        <div class="visa-material-workspace-save"><span data-material-save-status role="status" aria-live="polite">保存状态和备注后生效</span>
          <button class="primary" form="{form_id}" type="submit" data-material-save-button>保存材料状态</button></div>
        <p class="visa-material-workspace-legend">橙色：未完成　蓝色：未保存修改　绿色：已收到　灰色：不适用。已收到不代表审核通过。</p>
        {('<p class="visa-material-phase-note">系统会从 Application / Offer / COE+OSHC / Homestay 等来源自动引用匹配文件；源文件仍保留在原模块。</p>' if student_id else '<p class="visa-material-phase-note">已确认的材料存放在当前签证案件中。</p>')}
      </section>'''
