import json
import re
import sqlite3
import urllib.parse
import uuid
from datetime import datetime
from pathlib import Path

from .models import (
    OCRProviderAttempt,
    OCRResult,
    RedirectHop,
    RunRecord,
    StageEvent,
    SubtitleSegment,
)


PHASE2_TRANSITIONS = {
    "created": {"parsing_link", "ingesting_upload", "failed"},
    "parsing_link": {"resolving_platform", "failed"},
    "resolving_platform": {"downloading_video", "needs_upload", "failed"},
    "downloading_video": {"downloaded", "needs_upload", "failed"},
    "ingesting_upload": {"downloaded", "failed"},
    "needs_upload": {"ingesting_upload", "parsing_link"},
    "downloaded": {"extracting_frames", "failed"},
    "extracting_frames": {"segmenting_subtitles", "failed"},
    "segmenting_subtitles": {"recognizing_ocr", "failed"},
    "recognizing_ocr": {"recognizing_asr", "failed"},
    "recognizing_asr": {"fusing_asr", "failed"},
    "fusing_asr": {"fusing_cross_modal", "failed"},
    "fusing_cross_modal": {"correcting_transcript", "failed"},
    "correcting_transcript": {"preparing_review", "failed"},
    "preparing_review": {"completed", "failed"},
    "completed": set(),
    "failed": {"parsing_link", "ingesting_upload"},
}


class InvalidTransition(ValueError):
    pass


class ContentExtractionRepository:
    def __init__(self, db_path, now_iso=None, id_factory=None):
        self.db_path = Path(db_path)
        self._now_iso = now_iso or (
            lambda: datetime.now().astimezone().replace(microsecond=0).isoformat()
        )
        self._id_factory = id_factory or (lambda: str(uuid.uuid4()))

    def _connect(self):
        connection = sqlite3.connect(self.db_path)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=5000")
        return connection

    def initialize(self):
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS service_meta (
                  key TEXT PRIMARY KEY,
                  value TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS runs (
                  id TEXT PRIMARY KEY,
                  crm_job_id INTEGER NOT NULL UNIQUE,
                  pipeline_version TEXT NOT NULL DEFAULT 'v2-phase2',
                  raw_input TEXT NOT NULL DEFAULT '',
                  cleaned_input TEXT NOT NULL DEFAULT '',
                  extracted_url TEXT NOT NULL DEFAULT '',
                  resolved_url TEXT NOT NULL DEFAULT '',
                  platform TEXT NOT NULL DEFAULT '',
                  upload_path TEXT NOT NULL DEFAULT '',
                  video_path TEXT NOT NULL DEFAULT '',
                  file_hash TEXT NOT NULL DEFAULT '',
                  duration_ms INTEGER NOT NULL DEFAULT 0,
                  status TEXT NOT NULL,
                  current_stage TEXT NOT NULL,
                  error_code TEXT NOT NULL DEFAULT '',
                  error_message TEXT NOT NULL DEFAULT '',
                  created_at TEXT NOT NULL,
                  updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS stage_events (
                  id INTEGER PRIMARY KEY,
                  run_id TEXT NOT NULL,
                  stage TEXT NOT NULL,
                  status TEXT NOT NULL,
                  error_code TEXT NOT NULL DEFAULT '',
                  error_message TEXT NOT NULL DEFAULT '',
                  started_at TEXT NOT NULL,
                  finished_at TEXT NOT NULL DEFAULT '',
                  FOREIGN KEY(run_id) REFERENCES runs(id)
                );
                CREATE TABLE IF NOT EXISTS redirect_hops (
                  id INTEGER PRIMARY KEY,
                  run_id TEXT NOT NULL,
                  hop_index INTEGER NOT NULL,
                  source_url TEXT NOT NULL,
                  target_url TEXT NOT NULL,
                  status_code INTEGER NOT NULL,
                  UNIQUE(run_id, hop_index),
                  FOREIGN KEY(run_id) REFERENCES runs(id)
                );
                CREATE TABLE IF NOT EXISTS media_artifacts (
                  id INTEGER PRIMARY KEY,
                  run_id TEXT NOT NULL,
                  artifact_type TEXT NOT NULL,
                  path TEXT NOT NULL,
                  mime_type TEXT NOT NULL DEFAULT '',
                  size_bytes INTEGER NOT NULL DEFAULT 0,
                  sha256 TEXT NOT NULL DEFAULT '',
                  duration_ms INTEGER NOT NULL DEFAULT 0,
                  created_at TEXT NOT NULL,
                  UNIQUE(run_id, artifact_type, sha256),
                  FOREIGN KEY(run_id) REFERENCES runs(id)
                );
                CREATE TABLE IF NOT EXISTS subtitle_segments (
                  id TEXT PRIMARY KEY,
                  run_id TEXT NOT NULL,
                  start_ms INTEGER NOT NULL,
                  end_ms INTEGER NOT NULL,
                  representative_path TEXT NOT NULL,
                  image_hash TEXT NOT NULL,
                  preprocessing_version TEXT NOT NULL,
                  created_at TEXT NOT NULL,
                  UNIQUE(run_id,start_ms,end_ms,image_hash,preprocessing_version),
                  FOREIGN KEY(run_id) REFERENCES runs(id)
                );
                CREATE TABLE IF NOT EXISTS ocr_provider_runs (
                  id INTEGER PRIMARY KEY,
                  segment_id TEXT NOT NULL,
                  provider TEXT NOT NULL,
                  attempt_no INTEGER NOT NULL,
                  status TEXT NOT NULL,
                  error_code TEXT NOT NULL DEFAULT '',
                  error_message TEXT NOT NULL DEFAULT '',
                  created_at TEXT NOT NULL,
                  UNIQUE(segment_id,provider,attempt_no),
                  FOREIGN KEY(segment_id) REFERENCES subtitle_segments(id)
                );
                CREATE TABLE IF NOT EXISTS ocr_results (
                  id INTEGER PRIMARY KEY,
                  segment_id TEXT NOT NULL,
                  provider TEXT NOT NULL,
                  start_ms INTEGER NOT NULL,
                  end_ms INTEGER NOT NULL,
                  text TEXT NOT NULL DEFAULT '',
                  confidence REAL NOT NULL DEFAULT 0,
                  boxes_json TEXT NOT NULL DEFAULT '[]',
                  status TEXT NOT NULL,
                  model_version TEXT NOT NULL,
                  error_code TEXT NOT NULL DEFAULT '',
                  error_message TEXT NOT NULL DEFAULT '',
                  created_at TEXT NOT NULL,
                  UNIQUE(segment_id,provider,model_version),
                  FOREIGN KEY(segment_id) REFERENCES subtitle_segments(id)
                );
                CREATE TABLE IF NOT EXISTS ocr_raw_artifacts (
                  id INTEGER PRIMARY KEY,
                  segment_id TEXT NOT NULL,
                  provider TEXT NOT NULL,
                  path TEXT NOT NULL,
                  sha256 TEXT NOT NULL,
                  created_at TEXT NOT NULL,
                  UNIQUE(segment_id,provider,sha256),
                  FOREIGN KEY(segment_id) REFERENCES subtitle_segments(id)
                );
                CREATE TABLE IF NOT EXISTS recognition_cache (
                  image_hash TEXT NOT NULL,
                  provider TEXT NOT NULL,
                  model_version TEXT NOT NULL,
                  preprocessing_version TEXT NOT NULL,
                  ocr_result_id INTEGER NOT NULL,
                  created_at TEXT NOT NULL,
                  PRIMARY KEY(image_hash,provider,model_version,preprocessing_version),
                  FOREIGN KEY(ocr_result_id) REFERENCES ocr_results(id)
                );
                CREATE TABLE IF NOT EXISTS ocr_conflicts (
                  id INTEGER PRIMARY KEY,
                  segment_id TEXT NOT NULL,
                  category TEXT NOT NULL,
                  severity TEXT NOT NULL,
                  recommended_text TEXT NOT NULL DEFAULT '',
                  variants_json TEXT NOT NULL DEFAULT '[]',
                  created_at TEXT NOT NULL,
                  FOREIGN KEY(segment_id) REFERENCES subtitle_segments(id)
                );
                CREATE TABLE IF NOT EXISTS provider_metrics (
                  provider TEXT NOT NULL,
                  corpus_version TEXT NOT NULL,
                  sample_count INTEGER NOT NULL DEFAULT 0,
                  accuracy REAL NOT NULL DEFAULT 0,
                  updated_at TEXT NOT NULL,
                  PRIMARY KEY(provider,corpus_version)
                );
                CREATE TABLE IF NOT EXISTS recognition_alignments (
                  id INTEGER PRIMARY KEY,
                  run_id TEXT NOT NULL,
                  segment_id TEXT NOT NULL,
                  start_ms INTEGER NOT NULL,
                  end_ms INTEGER NOT NULL,
                  recommended_text TEXT NOT NULL DEFAULT '',
                  reliability TEXT NOT NULL,
                  sources_json TEXT NOT NULL DEFAULT '[]',
                  created_at TEXT NOT NULL,
                  FOREIGN KEY(run_id) REFERENCES runs(id),
                  FOREIGN KEY(segment_id) REFERENCES subtitle_segments(id)
                );
                """
            )
            connection.execute(
                """INSERT INTO service_meta(key,value) VALUES('schema_version','2')
                   ON CONFLICT(key) DO UPDATE SET value=excluded.value"""
            )

    @staticmethod
    def _segment_from_row(row):
        if row is None:
            return None
        return SubtitleSegment(
            row["id"], row["run_id"], row["start_ms"], row["end_ms"],
            row["representative_path"], row["image_hash"], row["preprocessing_version"],
        )

    @staticmethod
    def _ocr_result_from_row(row):
        if row is None:
            return None
        import json

        is_local = row["provider"] in {"paddle", "easyocr", "apple_vision"}
        image_hash = row["cache_image_hash"] if "cache_image_hash" in row.keys() else ""
        return OCRResult(
            row["provider"], row["start_ms"], row["end_ms"], row["text"],
            float(row["confidence"]), tuple(json.loads(row["boxes_json"])),
            row["status"], row["model_version"], row["error_code"], row["error_message"],
            request_id=(
                f"local:{row['provider']}:cache:{str(image_hash)[:12]}" if is_local else ""
            ),
            request_id_source="local" if is_local else "",
            billing_type="local" if is_local else "",
            cost=0 if is_local else None,
        )

    def add_subtitle_segment(
        self, run_id, start_ms, end_ms, representative_path, image_hash,
        preprocessing_version,
    ):
        if int(start_ms) < 0 or int(end_ms) <= int(start_ms):
            raise ValueError("invalid subtitle segment timeline")
        segment_id = str(self._id_factory())
        stamp = self._now_iso()
        with self._connect() as connection:
            connection.execute(
                """INSERT INTO subtitle_segments(
                   id,run_id,start_ms,end_ms,representative_path,image_hash,
                   preprocessing_version,created_at
                   ) VALUES(?,?,?,?,?,?,?,?)""",
                (
                    segment_id, run_id, int(start_ms), int(end_ms),
                    str(representative_path), str(image_hash),
                    str(preprocessing_version), stamp,
                ),
            )
            row = connection.execute(
                "SELECT * FROM subtitle_segments WHERE id=?", (segment_id,)
            ).fetchone()
        return self._segment_from_row(row)

    def save_completed_ocr_result(self, segment_id, result, preprocessing_version):
        if result.status != "completed":
            raise ValueError("only completed OCR results are cacheable")
        import json

        stamp = self._now_iso()
        with self._connect() as connection:
            segment = connection.execute(
                "SELECT * FROM subtitle_segments WHERE id=?", (segment_id,)
            ).fetchone()
            if segment is None:
                raise KeyError(segment_id)
            cursor = connection.execute(
                """INSERT OR IGNORE INTO ocr_results(
                   segment_id,provider,start_ms,end_ms,text,confidence,boxes_json,status,
                   model_version,error_code,error_message,created_at
                   ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    segment_id, result.provider, int(result.start_ms), int(result.end_ms),
                    result.text, float(result.confidence), json.dumps(result.boxes, ensure_ascii=False),
                    result.status, result.model_version, result.error_code,
                    result.error_message, stamp,
                ),
            )
            if cursor.lastrowid:
                result_id = cursor.lastrowid
            else:
                result_id = connection.execute(
                    "SELECT id FROM ocr_results WHERE segment_id=? AND provider=? AND model_version=?",
                    (segment_id, result.provider, result.model_version),
                ).fetchone()[0]
            connection.execute(
                """INSERT OR IGNORE INTO recognition_cache(
                   image_hash,provider,model_version,preprocessing_version,ocr_result_id,created_at
                   ) VALUES(?,?,?,?,?,?)""",
                (
                    segment["image_hash"], result.provider, result.model_version,
                    str(preprocessing_version), result_id, stamp,
                ),
            )

    def get_cached_ocr_result(
        self, image_hash, provider, model_version, preprocessing_version,
    ):
        with self._connect() as connection:
            row = connection.execute(
                """SELECT r.*, c.image_hash AS cache_image_hash FROM recognition_cache c
                   JOIN ocr_results r ON r.id=c.ocr_result_id
                   WHERE c.image_hash=? AND c.provider=? AND c.model_version=?
                     AND c.preprocessing_version=? AND r.status='completed'""",
                (image_hash, provider, model_version, preprocessing_version),
            ).fetchone()
        return self._ocr_result_from_row(row)

    def record_ocr_attempt(
        self, segment_id, provider, status, error_code="", error_message="",
    ):
        stamp = self._now_iso()
        with self._connect() as connection:
            attempt_no = connection.execute(
                "SELECT COALESCE(MAX(attempt_no),0)+1 FROM ocr_provider_runs WHERE segment_id=? AND provider=?",
                (segment_id, provider),
            ).fetchone()[0]
            connection.execute(
                """INSERT INTO ocr_provider_runs(
                   segment_id,provider,attempt_no,status,error_code,error_message,created_at
                   ) VALUES(?,?,?,?,?,?,?)""",
                (
                    segment_id, provider, attempt_no, status,
                    str(error_code or ""), str(error_message or ""), stamp,
                ),
            )
        return OCRProviderAttempt(
            segment_id, provider, attempt_no, status,
            str(error_code or ""), str(error_message or ""),
        )

    def list_ocr_attempts(self, segment_id, provider):
        with self._connect() as connection:
            rows = connection.execute(
                """SELECT * FROM ocr_provider_runs
                   WHERE segment_id=? AND provider=? ORDER BY attempt_no""",
                (segment_id, provider),
            ).fetchall()
        return tuple(
            OCRProviderAttempt(
                row["segment_id"], row["provider"], row["attempt_no"],
                row["status"], row["error_code"], row["error_message"],
            )
            for row in rows
        )

    def save_ocr_raw_artifact(self, segment_id, provider, path, sha256):
        with self._connect() as connection:
            connection.execute(
                """INSERT OR IGNORE INTO ocr_raw_artifacts(
                   segment_id,provider,path,sha256,created_at
                   ) VALUES(?,?,?,?,?)""",
                (segment_id, provider, str(path), str(sha256), self._now_iso()),
            )

    @staticmethod
    def _run_from_row(row):
        if row is None:
            return None
        return RunRecord(
            id=row["id"],
            crm_job_id=row["crm_job_id"],
            raw_input=row["raw_input"],
            extracted_url=row["extracted_url"],
            resolved_url=row["resolved_url"],
            platform=row["platform"],
            upload_path=row["upload_path"],
            video_path=row["video_path"],
            file_hash=row["file_hash"],
            duration_ms=row["duration_ms"],
            status=row["status"],
            current_stage=row["current_stage"],
            error_code=row["error_code"],
            error_message=row["error_message"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    @staticmethod
    def _event_from_row(row):
        return StageEvent(
            id=row["id"],
            run_id=row["run_id"],
            stage=row["stage"],
            status=row["status"],
            error_code=row["error_code"],
            error_message=row["error_message"],
            started_at=row["started_at"],
            finished_at=row["finished_at"],
        )

    def create_run(self, crm_job_id: int, raw_input: str, upload_path: str = ""):
        with self._connect() as connection:
            existing = connection.execute(
                "SELECT * FROM runs WHERE crm_job_id=?", (int(crm_job_id),)
            ).fetchone()
            if existing is not None:
                return self._run_from_row(existing)
            stamp = self._now_iso()
            run_id = str(self._id_factory())
            connection.execute(
                """INSERT INTO runs(
                   id,crm_job_id,raw_input,upload_path,status,current_stage,created_at,updated_at
                   ) VALUES(?,?,?,?,?,?,?,?)""",
                (
                    run_id,
                    int(crm_job_id),
                    str(raw_input or ""),
                    str(upload_path or ""),
                    "running",
                    "created",
                    stamp,
                    stamp,
                ),
            )
            connection.execute(
                """INSERT INTO stage_events(
                   run_id,stage,status,started_at
                   ) VALUES(?,?,?,?)""",
                (run_id, "created", "running", stamp),
            )
            row = connection.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
            return self._run_from_row(row)

    def get_run(self, run_id: str):
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
        return self._run_from_row(row)

    def get_run_by_crm_job_id(self, crm_job_id: int):
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM runs WHERE crm_job_id=?",
                (int(crm_job_id),),
            ).fetchone()
        return self._run_from_row(row)

    def set_upload_path(self, run_id: str, upload_path: str):
        stamp = self._now_iso()
        with self._connect() as connection:
            cursor = connection.execute(
                "UPDATE runs SET upload_path=?,updated_at=? WHERE id=?",
                (str(upload_path or ""), stamp, run_id),
            )
            if cursor.rowcount != 1:
                raise KeyError(run_id)
            row = connection.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
        return self._run_from_row(row)

    def transition(
        self,
        run_id: str,
        next_stage: str,
        error_code: str = "",
        error_message: str = "",
    ):
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            current = connection.execute(
                "SELECT * FROM runs WHERE id=?", (run_id,)
            ).fetchone()
            if current is None:
                raise KeyError(run_id)
            allowed = PHASE2_TRANSITIONS.get(current["current_stage"], set())
            if next_stage not in allowed:
                raise InvalidTransition(
                    f"{current['current_stage']} -> {next_stage} is not allowed"
                )
            stamp = self._now_iso()
            connection.execute(
                """UPDATE stage_events
                   SET status='completed',finished_at=?
                   WHERE run_id=? AND status='running'""",
                (stamp, run_id),
            )
            terminal_event_status = {
                "completed": "completed",
                "needs_upload": "needs_upload",
                "failed": "failed",
            }.get(next_stage, "running")
            finished_at = stamp if terminal_event_status != "running" else ""
            connection.execute(
                """INSERT INTO stage_events(
                   run_id,stage,status,error_code,error_message,started_at,finished_at
                   ) VALUES(?,?,?,?,?,?,?)""",
                (
                    run_id,
                    next_stage,
                    terminal_event_status,
                    str(error_code or ""),
                    str(error_message or ""),
                    stamp,
                    finished_at,
                ),
            )
            run_status = {
                "completed": "completed",
                "needs_upload": "needs_upload",
                "failed": "failed",
            }.get(next_stage, "running")
            connection.execute(
                """UPDATE runs
                   SET status=?,current_stage=?,error_code=?,error_message=?,updated_at=?
                   WHERE id=?""",
                (
                    run_status,
                    next_stage,
                    str(error_code or ""),
                    str(error_message or ""),
                    stamp,
                    run_id,
                ),
            )
            row = connection.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
            return self._run_from_row(row)

    def list_stage_events(self, run_id: str):
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM stage_events WHERE run_id=? ORDER BY id", (run_id,)
            ).fetchall()
        return tuple(self._event_from_row(row) for row in rows)

    def record_redirect_hops(self, run_id: str, hops):
        with self._connect() as connection:
            connection.execute("DELETE FROM redirect_hops WHERE run_id=?", (run_id,))
            for index, hop in enumerate(hops):
                connection.execute(
                    """INSERT INTO redirect_hops(
                       run_id,hop_index,source_url,target_url,status_code
                       ) VALUES(?,?,?,?,?)""",
                    (
                        run_id,
                        index,
                        hop.source_url,
                        hop.target_url,
                        int(hop.status_code),
                    ),
                )

    @staticmethod
    def _sanitized_redirect_url(url: str) -> str:
        parsed = urllib.parse.urlsplit(str(url or ""))
        scheme = parsed.scheme.lower() if parsed.scheme.lower() in {"http", "https"} else ""
        host = str(parsed.hostname or "").rstrip(".").lower()
        path = str(parsed.path or "/").split("?", 1)[0]
        return f"{scheme}://{host}{path}" if scheme and host else ""

    def record_redirect_failure(self, run_id: str, diagnostic, hops=()):
        allowed_fields = (
            "failure_stage", "hop_index", "http_method",
            "source_scheme", "source_host", "source_path_category",
            "dns_classification", "http_status", "location_present",
            "target_scheme", "target_host", "target_path_category",
            "transport_error_class", "internal_reason_code",
            "exception_class", "reason_class", "cause_class", "context_class",
            "errno", "timeout", "ssl_error", "dns_error",
            "connection_reset", "remote_disconnected", "connection_refused",
            "network_unreachable", "encoding", "encoding_start", "encoding_end",
            "encoding_reason", "object_type", "object_length",
            "offending_segment_length", "offending_codepoints", "is_ascii",
            "is_latin1_encodable", "encoding_target", "offending_header_name",
            "header_shape",
        )
        safe = {field: diagnostic.get(field) for field in allowed_fields}
        for field in (
            "failure_stage", "http_method", "source_scheme", "source_host",
            "source_path_category", "dns_classification", "target_scheme",
            "target_host", "target_path_category", "transport_error_class",
            "internal_reason_code", "exception_class", "reason_class",
            "cause_class", "context_class", "encoding", "encoding_reason",
            "object_type", "encoding_target", "offending_header_name",
        ):
            safe[field] = str(safe.get(field) or "").split("?", 1)[0][:160]
        safe["hop_index"] = int(safe.get("hop_index") or 0)
        safe["http_status"] = (
            int(safe["http_status"]) if safe.get("http_status") is not None else None
        )
        safe["location_present"] = bool(safe.get("location_present"))
        safe["errno"] = (
            int(safe["errno"])
            if isinstance(safe.get("errno"), int)
            and not isinstance(safe.get("errno"), bool)
            else None
        )
        for field in (
            "encoding_start", "encoding_end", "object_length",
            "offending_segment_length",
        ):
            value = safe.get(field)
            safe[field] = (
                int(value)
                if isinstance(value, int) and not isinstance(value, bool) and value >= 0
                else None
            )
        safe["offending_codepoints"] = [
            str(value)[:12]
            for value in (safe.get("offending_codepoints") or ())[:16]
            if re.fullmatch(r"U\+[0-9A-F]{4,8}", str(value))
        ]
        safe["header_shape"] = [
            {
                "name": str(item.get("name") or "")[:80],
                "value_length": max(0, int(item.get("value_length") or 0)),
                "ascii_safe": bool(item.get("ascii_safe")),
                "latin1_safe": bool(item.get("latin1_safe")),
                "contains_non_latin1": bool(item.get("contains_non_latin1")),
            }
            for item in (safe.get("header_shape") or ())[:32]
            if isinstance(item, dict)
        ]
        for field in (
            "timeout", "ssl_error", "dns_error", "connection_reset",
            "remote_disconnected", "connection_refused", "network_unreachable",
            "is_ascii", "is_latin1_encodable",
        ):
            safe[field] = bool(safe.get(field))
        if not diagnostic.get("encoding"):
            for field in (
                "encoding", "encoding_start", "encoding_end", "encoding_reason",
                "object_type", "object_length", "offending_segment_length",
                "offending_codepoints", "is_ascii", "is_latin1_encodable",
                "encoding_target", "offending_header_name", "header_shape",
            ):
                safe.pop(field, None)
        with self._connect() as connection:
            connection.execute(
                "INSERT OR REPLACE INTO service_meta(key,value) VALUES(?,?)",
                (f"redirect_failure:{run_id}", json.dumps(safe, ensure_ascii=False, sort_keys=True)),
            )
            connection.execute("DELETE FROM redirect_hops WHERE run_id=?", (run_id,))
            for index, hop in enumerate(hops):
                connection.execute(
                    """INSERT INTO redirect_hops(
                       run_id,hop_index,source_url,target_url,status_code
                       ) VALUES(?,?,?,?,?)""",
                    (
                        run_id, index,
                        self._sanitized_redirect_url(hop.source_url),
                        self._sanitized_redirect_url(hop.target_url),
                        int(hop.status_code),
                    ),
                )

    def get_redirect_failure_diagnostic(self, run_id: str):
        with self._connect() as connection:
            row = connection.execute(
                "SELECT value FROM service_meta WHERE key=?",
                (f"redirect_failure:{run_id}",),
            ).fetchone()
        if row is None:
            return None
        value = json.loads(row[0])
        return value if isinstance(value, dict) else None

    def save_link_result(self, run_id, normalized):
        stamp = self._now_iso()
        platform = getattr(normalized.platform, "value", normalized.platform)
        with self._connect() as connection:
            cursor = connection.execute(
                """UPDATE runs
                   SET cleaned_input=?,extracted_url=?,resolved_url=?,platform=?,updated_at=?
                   WHERE id=?""",
                (
                    str(normalized.cleaned_input), str(normalized.extracted_url),
                    str(normalized.resolved_url), str(platform), stamp, run_id,
                ),
            )
            if cursor.rowcount != 1:
                raise KeyError(run_id)

    def list_redirect_hops(self, run_id: str):
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM redirect_hops WHERE run_id=? ORDER BY hop_index", (run_id,)
            ).fetchall()
        return tuple(
            RedirectHop(row["source_url"], row["target_url"], row["status_code"])
            for row in rows
        )

    def save_media_result(self, run_id, video_path, file_hash, duration_ms=0):
        stamp = self._now_iso()
        with self._connect() as connection:
            cursor = connection.execute(
                """UPDATE runs SET video_path=?,file_hash=?,duration_ms=?,updated_at=?
                   WHERE id=?""",
                (str(video_path), str(file_hash), int(duration_ms or 0), stamp, run_id),
            )
            if cursor.rowcount != 1:
                raise KeyError(run_id)
            row = connection.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
        return self._run_from_row(row)

    def save_recognition_alignment(self, run_id, alignment):
        import json

        with self._connect() as connection:
            connection.execute(
                """INSERT INTO recognition_alignments(
                   run_id,segment_id,start_ms,end_ms,recommended_text,reliability,
                   sources_json,created_at
                   ) VALUES(?,?,?,?,?,?,?,?)""",
                (
                    run_id, alignment.segment_id, int(alignment.start_ms),
                    int(alignment.end_ms), alignment.recommended_text,
                    alignment.reliability,
                    json.dumps(alignment.sources, ensure_ascii=False), self._now_iso(),
                ),
            )

    def save_ocr_conflicts(self, conflicts):
        import json

        with self._connect() as connection:
            for conflict in conflicts:
                connection.execute(
                    """INSERT INTO ocr_conflicts(
                       segment_id,category,severity,recommended_text,variants_json,created_at
                       ) VALUES(?,?,?,?,?,?)""",
                    (
                        conflict.segment_id, conflict.category, conflict.severity,
                        conflict.recommended_text,
                        json.dumps(conflict.variants, ensure_ascii=False), self._now_iso(),
                    ),
                )

    def recommended_text_for_run(self, run_id):
        with self._connect() as connection:
            rows = connection.execute(
                """SELECT recommended_text FROM recognition_alignments
                   WHERE run_id=? ORDER BY start_ms,id""",
                (run_id,),
            ).fetchall()
        return "\n".join(row[0] for row in rows if row[0])

    def recover_interrupted_runs(self):
        stamp = self._now_iso()
        with self._connect() as connection:
            run_ids = tuple(
                row[0]
                for row in connection.execute(
                    "SELECT id FROM runs WHERE status='running'"
                ).fetchall()
            )
            if not run_ids:
                return 0
            placeholders = ",".join("?" for _ in run_ids)
            connection.execute(
                f"""UPDATE stage_events SET status='interrupted',finished_at=?
                    WHERE status='running' AND run_id IN ({placeholders})""",
                (stamp, *run_ids),
            )
            connection.execute(
                f"""UPDATE runs SET status='interrupted',updated_at=?
                    WHERE id IN ({placeholders})""",
                (stamp, *run_ids),
            )
            return len(run_ids)
