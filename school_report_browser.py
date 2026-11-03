"""Fixed installed browser tools for managed school-report rendering.

No caller-supplied executable paths; existing runtime file/network restrictions
remain in force for Node and all browser descendants.
"""
import json
from contextlib import contextmanager
from pathlib import Path

NODE = Path('/Users/faqiang/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/bin/node')
BROWSER_APP = Path('/Applications/Google Chrome for Testing.app')
BROWSER = BROWSER_APP / 'Contents/MacOS/Google Chrome for Testing'


def sandbox_rules(runtime_tmp):
    q = lambda path: json.dumps(str(path))
    socket_prefix = q(str(Path(runtime_tmp).resolve()) + '/')
    return [
        f'(allow process-exec (literal {q(NODE)}) (literal {q(NODE.resolve())}) (subpath {q(BROWSER_APP)}))',
        '(allow signal (target children))',
        f'(allow network-bind network-inbound (local unix-socket (path-prefix {socket_prefix})))',
        f'(allow network-outbound (remote unix-socket (path-prefix {socket_prefix})))',
    ]


@contextmanager
def exclusive_file_lock(path):
    """Keep the lock inode stable across threads and worker processes."""
    import fcntl
    import os
    lock = os.open(str(path), os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    with os.fdopen(lock, 'r+b') as guard:
        fcntl.flock(guard, fcntl.LOCK_EX)
        yield


def save_html_snapshot(target, document):
    """Serialize retries and publish a complete confirmation snapshot once."""
    import os
    import tempfile
    target = Path(target)
    with exclusive_file_lock(str(target) + '.lock'):
        if target.is_file():
            return
        with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=target.parent,
                                         prefix='.parent-snapshot-', delete=False) as stream:
            temporary = Path(stream.name)
            try:
                stream.write(document)
                stream.flush()
                os.fsync(stream.fileno())
            except BaseException:
                temporary.unlink(missing_ok=True)
                raise
        try:
            temporary.replace(target)
        finally:
            temporary.unlink(missing_ok=True)


def renderer_environment(source):
    """Pass runtime directories, never CRM credentials or proxy configuration."""
    keys = ('PATH', 'HOME', 'TMPDIR', 'TMP', 'TEMP', 'XDG_CACHE_HOME',
            'LANG', 'LC_ALL', 'AAIE_CRM_PDF_BROWSER_PATH', 'PLAYWRIGHT_BROWSERS_PATH')
    env = {key: str(source[key]) for key in keys if source.get(key)}
    if env.get('TMPDIR'):
        env['MAC_CHROMIUM_TMPDIR'] = env['TMPDIR']
    return env


def append_original_brochures(target, original, *, expected=False):
    """Copy the original report's appendix pages without restyling their contents."""
    import os
    import tempfile
    from pypdf import PdfReader, PdfWriter
    target, original = Path(target), Path(original)
    source = PdfReader(original)
    divider = next((i for i, page in enumerate(source.pages)
                    if 'Appendix: Official School Brochures' in (page.extract_text() or '')
                    and 'Confidential Client Report' in (page.extract_text() or '')), None)
    if divider is None:
        if expected:
            raise ValueError('原报告的宣传册附录缺失，未生成不完整归档。')
        return 0
    if divider == len(source.pages) - 1:
        raise ValueError('原报告只有宣传册分隔页，缺少原始宣传册。')
    writer = PdfWriter()
    writer.append(str(target), import_outline=False)
    writer.append(source, pages=(divider, len(source.pages)), import_outline=False)
    with tempfile.NamedTemporaryFile(dir=target.parent, suffix='.appendix.pdf', delete=False) as stream:
        temporary = Path(stream.name)
        try:
            writer.write(stream)
            stream.flush()
            os.fsync(stream.fileno())
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise
    try:
        temporary.replace(target)
    finally:
        temporary.unlink(missing_ok=True)
    return len(source.pages) - divider
