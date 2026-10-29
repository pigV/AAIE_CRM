#!/usr/bin/env python3
import argparse
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

try:
    from scripts import geocode_school_coordinates as geocoder
except ImportError:
    import geocode_school_coordinates as geocoder


ROOT = Path(__file__).resolve().parents[1]
DB_PATH = ROOT / "database" / "aaie_crm.db"
LOG_DIR = ROOT / "logs"
BACKUP_DIR = ROOT / "backups" / "data-fixes"
STATE_PATH = LOG_DIR / "school_geocode_auto_state.json"
LOG_PATH = LOG_DIR / "school_geocode_auto.log"
ERROR_PATH = LOG_DIR / "school_geocode_auto_errors.jsonl"
DEFAULT_STATES = ("NSW", "VIC", "QLD", "SA", "WA", "ACT", "TAS", "NT")


def now_iso():
    return datetime.now(timezone.utc).astimezone().replace(microsecond=0).isoformat()


def load_state(path=STATE_PATH):
    if not Path(path).is_file():
        return {
            "created_at": now_iso(),
            "updated_at": now_iso(),
            "backup_path": "",
            "failed_school_ids": [],
            "states": {},
        }
    with Path(path).open("r", encoding="utf-8") as handle:
        state = json.load(handle)
    state.setdefault("created_at", now_iso())
    state.setdefault("updated_at", now_iso())
    state.setdefault("backup_path", "")
    state.setdefault("failed_school_ids", [])
    state.setdefault("states", {})
    return state


def save_state(state, path=STATE_PATH):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    state["updated_at"] = now_iso()
    state["failed_school_ids"] = sorted({int(school_id) for school_id in state.get("failed_school_ids", [])})
    with path.open("w", encoding="utf-8") as handle:
        json.dump(state, handle, ensure_ascii=False, indent=2)


def append_json_line(path, payload):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n")


def log_line(path, message):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(f"{now_iso()} {message}\n")


def backup_database(db_path=DB_PATH, backup_dir=BACKUP_DIR):
    db_path = Path(db_path)
    if not db_path.is_file():
        raise FileNotFoundError(f"Database not found: {db_path}")
    backup_dir = Path(backup_dir)
    backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    target = backup_dir / f"aaie_crm_before_school_geocode_auto_{stamp}.db"
    source = sqlite3.connect(db_path)
    try:
        dest = sqlite3.connect(target)
        try:
            source.backup(dest)
        finally:
            dest.close()
    finally:
        source.close()
    return target


def state_missing_count(conn, state_code, failed_school_ids):
    return len(
        geocoder.missing_school_rows(
            conn,
            state=state_code,
            limit=0,
            exclude_school_ids=failed_school_ids,
        )
    )


def run_auto(
    db_path=DB_PATH,
    states=DEFAULT_STATES,
    batch_size=25,
    delay=1.1,
    provider="osm_nominatim",
    apply=True,
    geocode=True,
    state_path=STATE_PATH,
    log_path=LOG_PATH,
    error_path=ERROR_PATH,
    backup_dir=BACKUP_DIR,
    retry_failed=False,
):
    state = load_state(state_path)
    if retry_failed:
        state["failed_school_ids"] = []
    failed_school_ids = set(int(school_id) for school_id in state.get("failed_school_ids", []))
    summary = {
        "started_at": now_iso(),
        "provider": provider,
        "batch_size": batch_size,
        "delay": delay,
        "apply": bool(apply),
        "geocode": bool(geocode),
        "backup_path": state.get("backup_path", ""),
        "total_checked": 0,
        "total_updated": 0,
        "total_api_attempts": 0,
        "total_api_hits": 0,
        "total_cache_hits": 0,
        "total_suburb_fallback_hits": 0,
        "total_errors": 0,
        "states": {},
    }
    if apply and not state.get("backup_path"):
        backup_path = backup_database(db_path=db_path, backup_dir=backup_dir)
        state["backup_path"] = str(backup_path)
        summary["backup_path"] = str(backup_path)
        log_line(log_path, f"backup_created path={backup_path}")

    conn = geocoder.connect(db_path)
    try:
        for state_code in states:
            state_code = str(state_code or "").strip().upper()
            if not state_code:
                continue
            state_summary = {
                "checked": 0,
                "updated": 0,
                "api_attempts": 0,
                "api_hits": 0,
                "cache_hits": 0,
                "suburb_fallback_hits": 0,
                "errors": 0,
                "remaining_untried": 0,
            }
            while True:
                remaining_before = state_missing_count(conn, state_code, failed_school_ids)
                if remaining_before == 0:
                    break
                result = geocoder.run_backfill(
                    conn,
                    apply=apply,
                    state=state_code,
                    limit=batch_size,
                    geocode=geocode,
                    delay=delay,
                    provider=provider,
                    exclude_school_ids=failed_school_ids,
                )
                new_failed_ids = []
                for error in result.get("errors", []):
                    school_id = int(error.get("school_id") or 0)
                    if school_id:
                        failed_school_ids.add(school_id)
                        new_failed_ids.append(school_id)
                        append_json_line(error_path, {"at": now_iso(), "state": state_code, **error})

                state_summary["checked"] += result["checked"]
                state_summary["updated"] += result["updated"]
                state_summary["api_attempts"] += result["api_attempts"]
                state_summary["api_hits"] += result["api_hits"]
                state_summary["cache_hits"] += result["cache_hits"]
                state_summary["suburb_fallback_hits"] += result["suburb_fallback_hits"]
                state_summary["errors"] += len(result.get("errors", []))

                summary["total_checked"] += result["checked"]
                summary["total_updated"] += result["updated"]
                summary["total_api_attempts"] += result["api_attempts"]
                summary["total_api_hits"] += result["api_hits"]
                summary["total_cache_hits"] += result["cache_hits"]
                summary["total_suburb_fallback_hits"] += result["suburb_fallback_hits"]
                summary["total_errors"] += len(result.get("errors", []))

                state["failed_school_ids"] = sorted(failed_school_ids)
                state["states"][state_code] = state_summary
                save_state(state, state_path)
                append_json_line(log_path, {"at": now_iso(), "state": state_code, "result": result})

                if result["checked"] == 0:
                    break
                if result["updated"] == 0 and result["api_hits"] == 0 and not new_failed_ids:
                    log_line(log_path, f"stopped_without_progress state={state_code} remaining_before={remaining_before}")
                    break
            state_summary["remaining_untried"] = state_missing_count(conn, state_code, failed_school_ids)
            state["states"][state_code] = state_summary
            save_state(state, state_path)
            summary["states"][state_code] = state_summary
    finally:
        conn.close()

    summary["failed_school_ids"] = sorted(failed_school_ids)
    summary["finished_at"] = now_iso()
    append_json_line(log_path, {"at": now_iso(), "summary": summary})
    return summary


def build_parser():
    parser = argparse.ArgumentParser(description="Automatically backfill exact school coordinates with free OpenStreetMap Nominatim.")
    parser.add_argument("--db", default=str(DB_PATH), help="SQLite database path.")
    parser.add_argument("--state", action="append", dest="states", help="State to process. Can be used more than once. Defaults to all states.")
    parser.add_argument("--batch-size", type=int, default=25, help="Schools per batch.")
    parser.add_argument("--delay", type=float, default=1.1, help="Delay between Nominatim requests.")
    parser.add_argument("--provider", choices=("osm_nominatim", "google"), default="osm_nominatim", help="Geocoding provider.")
    parser.add_argument("--apply", action="store_true", help="Write coordinates into the CRM database.")
    parser.add_argument("--dry-run", action="store_true", help="Check the workflow without writing school coordinates.")
    parser.add_argument("--retry-failed", action="store_true", help="Retry schools that previous auto runs marked as failed.")
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    states = tuple(args.states) if args.states else DEFAULT_STATES
    apply = bool(args.apply and not args.dry_run)
    summary = run_auto(
        db_path=Path(args.db),
        states=states,
        batch_size=args.batch_size,
        delay=args.delay,
        provider=args.provider,
        apply=apply,
        geocode=not args.dry_run,
        retry_failed=args.retry_failed,
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
