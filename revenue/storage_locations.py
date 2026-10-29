"""Read-only physical location resolution; financial document rows stay untouched."""
import hashlib
import json
import sqlite3
from revenue.secure_storage import StorageUnavailable, read_reference, _path

TABLE = 'revenue_document_locations'
VERSION = '20260908_036'

def document_fingerprint(document):
    return hashlib.sha256(json.dumps(dict(document), sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode()).hexdigest()


def location_history(document, query_all):
    """Absent pre-migration authority is explicit; broken registered schema is not fallback."""
    try:
        names = {r['name']:r['type'] for r in query_all(
            "SELECT name,type FROM sqlite_master WHERE name IN ('revenue_document_locations','schema_migrations')")}
        if TABLE not in names:
            if names.get('schema_migrations') != 'table':
                raise StorageUnavailable()
            if query_all("SELECT version FROM schema_migrations WHERE version=? OR name LIKE ?", (VERSION,'%revenue_storage_locations%')):
                raise StorageUnavailable()
            if not query_all("SELECT version FROM schema_migrations WHERE version=?", ('20260901_034',)):
                raise StorageUnavailable()
            return []
        if names[TABLE] != 'table':
            raise StorageUnavailable()
        rows = [dict(r) for r in query_all(
            'SELECT * FROM revenue_document_locations WHERE document_id=? ORDER BY sequence', (document['id'],))]
        prior = None
        path = str(document['storage_path'])
        fingerprint = document_fingerprint(document)
        for sequence, event in enumerate(rows, 1):
            if (event['sequence'] != sequence or event['previous_event_id'] != (prior['id'] if prior else None)
                or event['document_uid'] != document['uid'] or event['old_path'] != path
                or event['document_fingerprint'] != fingerprint or event['sha256'] != document['sha256']
                or event['size_bytes'] != document['size_bytes'] or event['verification_result'] != 'PASS'
                or event['destination_sha256'] != document['sha256'] or event['new_path'] == path):
                raise StorageUnavailable()
            _path(event['new_path']); _path(event['old_path'])
            if event['kind'] == 'restore':
                if (not prior or prior['kind'] != 'relocate' or event['reverses_event_id'] != prior['id']
                    or event['new_path'] != prior['old_path']):
                    raise StorageUnavailable()
            elif event['kind'] != 'relocate' or event['reverses_event_id'] is not None or event['source_sha256'] != document['sha256']:
                raise StorageUnavailable()
            path, prior = event['new_path'], event
        return rows
    except (sqlite3.Error, KeyError, TypeError, ValueError) as exc:
        raise StorageUnavailable() from exc


def resolve_location(document, query_all):
    history = location_history(document, query_all)
    return history[-1]['new_path'] if history else str(document['storage_path'])


def read_document(document, query_all, legacy_root, secure_root=None):
    return read_reference(legacy_root, resolve_location(document, query_all),
                          str(document['sha256']), int(document['size_bytes']), secure_root=secure_root)
