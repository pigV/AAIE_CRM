"""Local Apple Vision OCR with bounded work and safe, per-call diagnostics."""
from pathlib import Path
import os
import re
import subprocess
import tempfile
import time

OCR_TOTAL_BUDGET_SECONDS = 120


def command(data_root, source_root, staging):
    executable = Path(data_root) / 'tools/document-vision-ocr'
    if executable.is_file() and os.access(executable, os.X_OK):
        return [str(executable)]
    script = Path(source_root) / 'tools/macos_vision_ocr.swift'
    if not staging and script.is_file():
        return ['/usr/bin/swift', str(script)]
    return []


def recognize(paths, data_root, source_root, staging, diagnostics, timeout=60):
    args = command(data_root, source_root, staging)
    if not args:
        diagnostics['status'] = 'engine_unavailable'
        return ''
    env = os.environ.copy()
    cache = Path(data_root) / 'cache/swift_module_cache'
    cache.mkdir(parents=True, exist_ok=True)
    env['CLANG_MODULE_CACHE_PATH'] = str(cache)
    env['SWIFT_MODULE_CACHE_PATH'] = str(cache)
    try:
        result = subprocess.run(args + [str(p) for p in paths], cwd=str(source_root),
                                capture_output=True, text=True, env=env, timeout=timeout,
                                check=False)
    except subprocess.TimeoutExpired:
        diagnostics['status'] = 'timeout'
        return ''
    except OSError:
        diagnostics['status'] = 'engine_failed'
        return ''
    if result.returncode:
        diagnostics['status'] = 'engine_failed'
        return ''
    text = result.stdout.strip()
    diagnostics['status'] = 'ok' if text else 'no_text'
    return text


def extract_image(data, suffix, *, data_root, source_root, staging, diagnostics):
    if not command(data_root, source_root, staging):
        diagnostics['status'] = 'engine_unavailable'
        return ''
    safe_suffix = suffix if re.fullmatch(r'\.[A-Za-z0-9]+', suffix or '') else '.png'
    work = Path(data_root) / 'cache/ocr_work'
    try:
        work.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=work) as folder:
            image = Path(folder) / ('image' + safe_suffix)
            image.write_bytes(data)
            return recognize([image], data_root, source_root, staging, diagnostics)
    except (OSError, ValueError):
        diagnostics['status'] = 'image_failed'
        return ''


def extract_pdf(data, max_pages, *, data_root, source_root, staging, diagnostics):
    if not command(data_root, source_root, staging):
        diagnostics['status'] = 'engine_unavailable'
        return ''
    try:
        import pypdfium2 as pdfium
        work = Path(data_root) / 'cache/ocr_work'
        work.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=work) as folder:
            with pdfium.PdfDocument(data) as pdf:
                count = min(len(pdf), max_pages)
                diagnostics.update(pages_processed=count, pages_total=len(pdf))
                images = []
                for index in range(count):
                    page = pdf[index]
                    bitmap = None
                    try:
                        width, height = page.get_size()
                        scale = min(2.5, 3000 / max(width, height))
                        bitmap = page.render(scale=scale)
                        image = bitmap.to_pil()
                        path = Path(folder) / f'page-{index + 1}.png'
                        image.save(path)
                        image.close()
                        images.append(path)
                    finally:
                        if bitmap is not None:
                            bitmap.close()
                        page.close()
            if not images:
                diagnostics['status'] = 'no_text'
                return ''
            return recognize(images, data_root, source_root, staging, diagnostics)
    except ImportError:
        diagnostics['status'] = 'renderer_unavailable'
    except Exception:
        # No exception text: it can contain internal paths or document contents.
        diagnostics['status'] = 'pdf_failed'
    return ''


def extract_pdf_pages(data, page_indexes, *, data_root, source_root, staging, diagnostics):
    """OCR the requested zero-based pages, one recognizer call per page.

    The recognizer emits one line stream for all input images with no page
    separators, so a single call per page is the only way to keep page
    ownership trustworthy when a document mixes text and scanned pages.
    """
    indexes = sorted({int(index) for index in page_indexes if int(index) >= 0})
    result = {index: '' for index in indexes}
    if not indexes:
        diagnostics.setdefault('status', 'no_text')
        return result
    if not command(data_root, source_root, staging):
        diagnostics['status'] = 'engine_unavailable'
        return result
    try:
        import pypdfium2 as pdfium
        work = Path(data_root) / 'cache/ocr_work'
        work.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=work) as folder:
            with pdfium.PdfDocument(data) as pdf:
                diagnostics.update(pages_total=len(pdf))
                statuses = []
                deadline = time.monotonic() + OCR_TOTAL_BUDGET_SECONDS
                for index in indexes:
                    if index >= len(pdf):
                        continue
                    page = pdf[index]
                    bitmap = None
                    path = Path(folder) / f'page-{index + 1}.png'
                    try:
                        width, height = page.get_size()
                        scale = min(2.5, 3000 / max(width, height))
                        bitmap = page.render(scale=scale)
                        image = bitmap.to_pil()
                        image.save(path)
                        image.close()
                    finally:
                        if bitmap is not None:
                            bitmap.close()
                        page.close()
                    remaining = deadline - time.monotonic()
                    if remaining <= 1:
                        statuses.append('timeout')
                        break
                    page_diagnostics = {}
                    result[index] = recognize([path], data_root, source_root, staging,
                                              page_diagnostics, timeout=min(60, remaining))
                    statuses.append(page_diagnostics.get('status', 'no_text'))
                    diagnostics['pages_processed'] = diagnostics.get('pages_processed', 0) + 1
    except ImportError:
        diagnostics['status'] = 'renderer_unavailable'
        return result
    except Exception:
        # No exception text: it can contain internal paths or document contents.
        diagnostics['status'] = 'pdf_failed'
        return result
    if 'ok' in statuses:
        diagnostics['status'] = 'ok'
    elif statuses:
        diagnostics['status'] = next((status for status in statuses if status != 'no_text'), 'no_text')
    else:
        diagnostics['status'] = 'no_text'
    return result
