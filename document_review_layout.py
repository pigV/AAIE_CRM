"""Shared source preview controls for material confirmation pages."""
from html import escape

def preview_panel(preview_url, download_url, filename):
    src=escape(str(preview_url),quote=True)
    download=escape(str(download_url),quote=True)
    title=escape(str(filename),quote=True)
    return f'''                  <section class="review-preview-workspace upload-review-preview-panel" data-review-preview-root data-preview-src="{src}?size=large" data-download-url="{download}" data-preview-title="{title}">
                    <div class="review-preview-head">
                      <div><strong>文件预览 / Document Preview</strong><small>对照右侧原件核对左侧字段；两边可分别滚动。</small></div>
                      <div class="review-preview-head-actions">
                        <button type="button" class="secondary mini" data-review-preview-fullscreen>全屏查看</button>
                        <button type="button" class="ghost mini" data-review-preview-open-window>新窗口打开</button>
                        <a class="secondary button-link mini" href="{download}" data-review-preview-download>下载原文件</a>
                      </div>
                    </div>
                    <div class="review-preview-toolbar upload-review-preview-toolbar" role="toolbar" aria-label="文件预览工具栏">
                      <button type="button" class="ghost mini" data-review-preview-thumbnail-toggle>隐藏缩略图</button>
                      <button type="button" class="ghost mini" data-review-preview-prev>上一页</button>
                      <span class="review-preview-page-indicator" data-review-preview-indicator>1 / 1</span>
                      <button type="button" class="ghost mini" data-review-preview-next>下一页</button>
                      <label class="review-preview-jump">跳转到<input inputmode="numeric" pattern="[0-9]*" data-review-preview-jump-input aria-label="跳转到页码">页</label>
                      <button type="button" class="ghost mini" data-review-preview-jump-button>跳转</button>
                      <button type="button" class="ghost mini" data-review-preview-zoom-out>缩小</button>
                      <select data-review-preview-zoom aria-label="缩放">
                        <option value="0.5">50%</option>
                        <option value="0.75">75%</option>
                        <option value="1" selected>100%</option>
                        <option value="1.25">125%</option>
                        <option value="1.5">150%</option>
                        <option value="2">200%</option>
                      </select>
                      <button type="button" class="ghost mini" data-review-preview-zoom-in>放大</button>
                      <button type="button" class="ghost mini" data-review-preview-fit-width>适合宽度</button>
                      <button type="button" class="ghost mini" data-review-preview-fit-page>适合页面</button>
                      <button type="button" class="ghost mini" data-review-preview-rotate>旋转</button>
                      <label class="review-preview-search">搜索<input type="search" placeholder="搜索文字" data-review-preview-search></label>
                      <span class="review-preview-message" data-review-preview-message role="status" aria-live="polite"></span>
                    </div>
                    <div class="review-preview-body">
                      <aside class="review-preview-thumbnails" data-review-preview-thumbnails aria-label="页面缩略图"></aside>
                      <div class="review-preview-canvas upload-review-preview-stage" data-review-preview-canvas>
                        <div class="review-source-highlight" data-review-source-highlight hidden></div>
                        <img data-review-preview-image alt="{title} preview">
                      </div>
                    </div>
                  </section>'''
