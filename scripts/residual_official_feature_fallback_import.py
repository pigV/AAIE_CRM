#!/usr/bin/env python3
"""Conservative fallback feature extraction for schools still without tags."""

import argparse
import csv
import json
import re
import sys
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

APP_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP_DIR))

import app as app_module
from scripts import batch_official_school_feature_import as official_batch
from scripts.deep_official_school_feature_import import normalize_start_url


DEFAULT_DB_PATH = APP_DIR / "database" / "aaie_crm.db"
DEFAULT_REPORT_DIR = APP_DIR / "reports"
DEFAULT_BACKUP_DIR = APP_DIR / "backups" / "data-fixes"


FALLBACK_RULES = (
    {
        "tag_id": 325,
        "keywords": (
            "student wellbeing",
            "social and emotional wellbeing",
            "wellbeing and resilience",
            "well-being and resilience",
            "wellbeing framework",
            "wellbeing program",
            "wellbeing programs",
        ),
        "confidence": 72,
    },
    {
        "tag_id": 326,
        "keywords": (
            "pastoral care",
            "pastoral support",
        ),
        "confidence": 78,
    },
    {
        "tag_id": 336,
        "keywords": (
            "counselling",
            "counseling",
            "school counsellor",
            "school counselor",
            "student counsellor",
            "student counselor",
        ),
        "confidence": 76,
    },
    {
        "tag_id": 122,
        "keywords": (
            "learning support",
            "academic support",
            "support for learning",
            "individual learning needs",
        ),
        "confidence": 72,
    },
    {
        "tag_id": 335,
        "keywords": (
            "special learning needs",
            "additional learning needs",
            "students with disability",
            "diverse learning needs",
        ),
        "confidence": 74,
    },
    {
        "tag_id": 206,
        "keywords": (
            "innovative teaching practices",
            "creativity and innovation",
            "curiosity, creativity and innovation",
            "innovation in learning",
            "innovative learning",
        ),
        "confidence": 70,
    },
)


def stamp():
    return datetime.now().strftime("%Y%m%d_%H%M%S")


def compact_text(value):
    return re.sub(r"\s+", " ", str(value or "")).strip()


def excerpt_for_keyword(text, keyword, limit=260):
    clean = compact_text(text)
    lowered = clean.lower()
    needle = keyword.lower()
    index = lowered.find(needle)
    if index < 0:
        return clean[:limit]
    start = max(0, index - 100)
    end = min(len(clean), index + len(needle) + 160)
    return ("..." if start else "") + clean[start:end] + ("..." if end < len(clean) else "")


def load_missing_schools(conn, args):
    where = [
        "COALESCE(s.deleted_at,'')=''",
        "s.website_url IS NOT NULL",
        "TRIM(s.website_url)<>''",
        "LOWER(TRIM(s.website_url)) NOT IN ('待补充','n/a','na','none','unknown')",
        "a.id IS NULL",
    ]
    params = []
    if args.state:
        where.append("UPPER(COALESCE(s.state,''))=UPPER(?)")
        params.append(args.state)
    if args.school_id:
        placeholders = ",".join("?" for _ in args.school_id)
        where.append(f"s.id IN ({placeholders})")
        params.extend(args.school_id)
    query = f"""
        SELECT s.id,s.school_name,s.state,s.suburb,s.postcode,s.website_url
        FROM schools s
        LEFT JOIN school_feature_tag_assignments a ON a.school_id=s.id
        WHERE {' AND '.join(where)}
        GROUP BY s.id
        ORDER BY s.state,s.school_name,s.id
    """
    if args.limit:
        query += " LIMIT ?"
        params.append(args.limit)
    return [dict(row) for row in conn.execute(query, params).fetchall()]


def tag_lookup(tag_rows):
    return {int(row["id"]): dict(row) for row in tag_rows if row.get("id") is not None}


def fetch_page_text(url, timeout):
    import requests

    session = requests.Session()
    response = session.get(
        url,
        timeout=timeout,
        headers={"User-Agent": "AAIE-CRM-SchoolFeatureFallback/1.0"},
    )
    response.raise_for_status()
    title, text, _pdf_links = app_module.parse_school_website_html(response.text, response.url)
    return response.url, title, text


def extract_fallback_features(text, source_url, source_title, tags_by_id):
    lower_text = compact_text(text).lower()
    features = []
    seen = set()
    for rule in FALLBACK_RULES:
        tag = tags_by_id.get(rule["tag_id"])
        if not tag:
            continue
        for keyword in rule["keywords"]:
            if keyword.lower() not in lower_text:
                continue
            if rule["tag_id"] in seen:
                break
            excerpt = excerpt_for_keyword(text, keyword)
            tag_name = tag.get("tag_name_cn") or tag.get("tag_name_zh") or tag.get("tag_name_en") or tag.get("tag_key")
            features.append(
                {
                    "tag_id": tag.get("id"),
                    "category_id": tag.get("category_id"),
                    "category": tag.get("category_name_cn") or "",
                    "category_name_cn": tag.get("category_name_cn") or "",
                    "category_name_en": tag.get("category_name_en") or "",
                    "tag_name": tag_name,
                    "tag_name_cn": tag_name,
                    "tag_name_en": tag.get("tag_name_en") or tag_name,
                    "description": excerpt,
                    "confidence_rate": rule["confidence"],
                    "source_url": source_url,
                    "source_title": source_title or urlparse(source_url).netloc,
                    "source_type": "official_website",
                    "source_excerpt": excerpt,
                    "matched_keywords": [keyword],
                }
            )
            seen.add(rule["tag_id"])
            break
    return features


def write_report(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = [
        "school_id",
        "school_name",
        "state",
        "suburb",
        "postcode",
        "website_url",
        "status",
        "message",
        "features_found",
        "features_inserted",
        "feature_names",
        "source_url",
        "error",
    ]
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def main(argv=None):
    parser = argparse.ArgumentParser(description="Conservative fallback official website feature extraction.")
    parser.add_argument("--db", default=str(DEFAULT_DB_PATH))
    parser.add_argument("--state", default="")
    parser.add_argument("--school-id", action="append", type=int, default=[])
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--timeout", type=int, default=12)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--no-backup", action="store_true")
    parser.add_argument("--report", default="")
    args = parser.parse_args(argv)

    db_path = Path(args.db)
    report_path = Path(args.report) if args.report else DEFAULT_REPORT_DIR / f"residual_official_feature_fallback_{stamp()}.csv"

    with official_batch.connect(db_path) as conn:
        schools = load_missing_schools(conn, args)
        tags_by_id = tag_lookup(official_batch.load_feature_tags(conn))

    backup_path = ""
    if schools and not args.dry_run and not args.no_backup:
        backup_path = str(official_batch.make_backup(db_path, DEFAULT_BACKUP_DIR))
    print(json.dumps({"status": "starting", "schools": len(schools), "dry_run": args.dry_run, "backup": backup_path, "report": str(report_path)}, ensure_ascii=False), flush=True)

    rows = []
    for index, school in enumerate(schools, start=1):
        start_url = normalize_start_url(school.get("website_url") or "")
        result = {
            "status": "error",
            "source_url": start_url,
            "features": [],
            "message": "",
            "errors": [],
        }
        source_url = start_url
        error = ""
        try:
            if not start_url:
                raise RuntimeError("学校官网链接必须是 http 或 https。")
            source_url, source_title, text = fetch_page_text(start_url, args.timeout)
            result["source_url"] = source_url
            result["features"] = extract_fallback_features(text, source_url, source_title, tags_by_id)
            if result["features"]:
                result["status"] = "matched"
                result["message"] = f"兜底官网规则匹配 {len(result['features'])} 个学校特色标签"
            else:
                result["status"] = "no_feature"
                result["message"] = "无保守兜底特征"
        except Exception as exc:
            error = str(exc)
            result["status"] = "error"
            result["message"] = f"兜底官网特色提取失败：{exc}"
            result["errors"] = [{"url": start_url, "error": error}]

        inserted = 0
        if not args.dry_run and result["features"]:
            with official_batch.connect(db_path) as conn:
                inserted = official_batch.insert_feature_suggestions(conn, school["id"], result)
                official_batch.record_collection_profile(conn, school, result, inserted)
        row = {
            "school_id": school["id"],
            "school_name": school["school_name"],
            "state": school.get("state") or "",
            "suburb": school.get("suburb") or "",
            "postcode": school.get("postcode") or "",
            "website_url": school.get("website_url") or "",
            "status": result["status"],
            "message": result["message"],
            "features_found": len(result["features"]),
            "features_inserted": inserted,
            "feature_names": "; ".join(feature.get("tag_name_cn") or "" for feature in result["features"]),
            "source_url": source_url,
            "error": error,
        }
        rows.append(row)
        write_report(report_path, rows)
        print(json.dumps({"index": index, "total": len(schools), **row}, ensure_ascii=False), flush=True)

    summary = {}
    inserted_total = 0
    for row in rows:
        summary[row["status"]] = summary.get(row["status"], 0) + 1
        inserted_total += int(row["features_inserted"] or 0)
    print(json.dumps({"status": "finished", "summary": summary, "features_inserted": inserted_total, "report": str(report_path), "backup": backup_path}, ensure_ascii=False), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
