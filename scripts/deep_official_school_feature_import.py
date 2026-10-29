#!/usr/bin/env python3
"""Deeper official website feature extraction for schools without tags.

This script starts from each school's official website, follows a small set of
high-signal internal pages, and writes only Suggested CRM feature tags.
"""

import argparse
import csv
import json
import re
import sqlite3
import sys
from datetime import datetime
from pathlib import Path
from urllib.parse import urljoin, urlparse, urlunparse

APP_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP_DIR))

import app as app_module
from scripts import batch_official_school_feature_import as official_batch


DEFAULT_DB_PATH = APP_DIR / "database" / "aaie_crm.db"
DEFAULT_REPORT_DIR = APP_DIR / "reports"
DEFAULT_BACKUP_DIR = APP_DIR / "backups" / "data-fixes"

POSITIVE_LINK_KEYWORDS = (
    "about",
    "academic",
    "academics",
    "accelerated",
    "activities",
    "arts",
    "boarding",
    "co-curricular",
    "cocurricular",
    "curriculum",
    "duke",
    "enrichment",
    "extension",
    "extracurricular",
    "gifted",
    "house",
    "ib",
    "international",
    "learning",
    "music",
    "outdoor",
    "pastoral",
    "pathways",
    "performing",
    "program",
    "programs",
    "seal",
    "sport",
    "stem",
    "student-support",
    "support",
    "vce",
    "wellbeing",
)

NEGATIVE_LINK_KEYWORDS = (
    "calendar",
    "canteen",
    "careers",
    "contact",
    "employment",
    "facebook",
    "fees",
    "form",
    "instagram",
    "login",
    "newsletter",
    "news",
    "policy",
    "policies",
    "privacy",
    "shop",
    "staff",
    "term-date",
    "uniform",
    "youtube",
)


def stamp():
    return datetime.now().strftime("%Y%m%d_%H%M%S")


def normalize_start_url(raw_url):
    value = str(raw_url or "").strip()
    parsed = urlparse(value)
    if not parsed.scheme and value and "." in value.split("/", 1)[0]:
        value = f"https://{value}"
        parsed = urlparse(value)
    return value if parsed.scheme in {"http", "https"} else ""


def canonicalize_url(url):
    parsed = urlparse(url)
    path = re.sub(r"/+", "/", parsed.path or "/")
    if path != "/" and path.endswith("/"):
        path = path[:-1]
    return urlunparse((parsed.scheme.lower(), parsed.netloc.lower(), path, "", "", ""))


def same_site(url, base_url):
    parsed = urlparse(url)
    base = urlparse(base_url)
    host = parsed.netloc.lower().removeprefix("www.")
    base_host = base.netloc.lower().removeprefix("www.")
    return host == base_host


def score_internal_link(url, text):
    haystack = f"{url} {text}".lower()
    if any(keyword in haystack for keyword in NEGATIVE_LINK_KEYWORDS):
        return -100
    score = 0
    for keyword in POSITIVE_LINK_KEYWORDS:
        if keyword in haystack:
            score += 10
    path_depth = len([part for part in urlparse(url).path.split("/") if part])
    score -= min(path_depth, 5)
    return score


def discover_candidate_pages(html_text, base_url, max_pages):
    try:
        from bs4 import BeautifulSoup
    except ImportError as exc:
        raise RuntimeError("缺少依赖 beautifulsoup4，无法解析官网二级页面链接。") from exc

    soup = BeautifulSoup(html_text or "", "html.parser")
    candidates = {}
    for link in soup.find_all("a", href=True):
        href = str(link.get("href") or "").strip()
        if not href or href.startswith(("#", "mailto:", "tel:", "javascript:")):
            continue
        absolute = canonicalize_url(urljoin(base_url, href))
        if not same_site(absolute, base_url):
            continue
        if absolute.lower().split("?", 1)[0].endswith((".jpg", ".jpeg", ".png", ".gif", ".webp", ".zip", ".doc", ".docx")):
            continue
        text = link.get_text(" ", strip=True)
        score = score_internal_link(absolute, text)
        if score <= 0:
            continue
        candidates[absolute] = max(score, candidates.get(absolute, -999))
    ranked = sorted(candidates.items(), key=lambda item: (-item[1], item[0]))
    return [url for url, _score in ranked[:max_pages]]


def fetch_text_and_links(session, url, timeout):
    headers = {"User-Agent": "AAIE-CRM-SchoolFeatureExtractor/1.0"}
    response = session.get(url, timeout=timeout, headers=headers)
    if hasattr(response, "raise_for_status"):
        response.raise_for_status()
    html_text = getattr(response, "text", "")
    if not html_text:
        content = getattr(response, "content", b"")
        html_text = content.decode("utf-8", errors="ignore") if isinstance(content, bytes) else str(content or "")
    source_title, page_text, _pdf_links = app_module.parse_school_website_html(html_text, url)
    return html_text, source_title, page_text


def deep_extract_school_features(start_url, tag_rows, max_pages, timeout, requests_session=None):
    start_url = normalize_start_url(start_url)
    result = {
        "status": "error",
        "source_url": start_url,
        "features": [],
        "pages_checked": [],
        "message": "",
        "errors": [],
    }
    if not start_url:
        result["message"] = "学校官网链接必须是 http 或 https。"
        result["error"] = result["message"]
        return result
    try:
        if requests_session is None:
            import requests
            requests_session = requests.Session()
        tag_definitions = app_module.normalize_school_feature_tags(tag_rows)

        html_text, title, page_text = fetch_text_and_links(requests_session, start_url, timeout)
        pages = [start_url] + discover_candidate_pages(html_text, start_url, max_pages)
        matches = []
        seen_pages = set()
        for page_url in pages:
            if page_url in seen_pages:
                continue
            seen_pages.add(page_url)
            try:
                if page_url == start_url:
                    page_title, text = title, page_text
                else:
                    _html, page_title, text = fetch_text_and_links(requests_session, page_url, timeout)
                page_matches = app_module.match_school_feature_tags_from_text(
                    text,
                    tag_definitions,
                    page_url,
                    "official_website",
                    source_title=page_title,
                )
                result["pages_checked"].append({"url": page_url, "status": "ok", "matched_count": len(page_matches)})
                matches.extend(page_matches)
            except Exception as exc:
                result["errors"].append({"url": page_url, "error": str(exc)})
                result["pages_checked"].append({"url": page_url, "status": "error", "matched_count": 0})

        result["features"] = app_module.merge_school_feature_matches(matches)
        if result["features"]:
            result["status"] = "matched"
            result["message"] = f"深度官网抓取匹配 {len(result['features'])} 个学校特色标签"
        else:
            result["status"] = "no_feature"
            result["message"] = "无特征"
    except Exception as exc:
        result["status"] = "error"
        result["message"] = f"深度官网特色提取失败：{exc}"
        result["error"] = result["message"]
    return result


def load_target_schools(conn, args):
    where = [
        "COALESCE(s.deleted_at,'')=''",
        "s.website_url IS NOT NULL",
        "TRIM(s.website_url)<>''",
        "LOWER(TRIM(s.website_url)) NOT IN ('待补充','n/a','na','none','unknown')",
    ]
    params = []
    if args.state:
        where.append("UPPER(COALESCE(s.state,''))=UPPER(?)")
        params.append(args.state)
    if args.school_id:
        placeholders = ",".join("?" for _ in args.school_id)
        where.append(f"s.id IN ({placeholders})")
        params.extend(args.school_id)
    if args.only_missing_tags:
        where.append("a.id IS NULL")
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


def write_report(report_path, rows):
    report_path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = [
        "school_id",
        "school_name",
        "state",
        "suburb",
        "postcode",
        "website_url",
        "status",
        "message",
        "pages_checked",
        "features_found",
        "features_inserted",
        "errors",
    ]
    with report_path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def main(argv=None):
    parser = argparse.ArgumentParser(description="Deep crawl official school websites for CRM feature tags.")
    parser.add_argument("--db", default=str(DEFAULT_DB_PATH))
    parser.add_argument("--state", default="")
    parser.add_argument("--school-id", action="append", type=int, default=[])
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--include-existing-tags", action="store_true")
    parser.add_argument("--no-backup", action="store_true")
    parser.add_argument("--timeout", type=int, default=12)
    parser.add_argument("--max-pages", type=int, default=8)
    parser.add_argument("--report", default="")
    args = parser.parse_args(argv)
    args.only_missing_tags = not args.include_existing_tags

    db_path = Path(args.db)
    report_path = Path(args.report) if args.report else DEFAULT_REPORT_DIR / f"deep_official_school_feature_batch_{stamp()}.csv"

    with official_batch.connect(db_path) as conn:
        schools = load_target_schools(conn, args)
        tag_rows = official_batch.load_feature_tags(conn)

    backup_path = ""
    if schools and not args.no_backup:
        backup_path = str(official_batch.make_backup(db_path, DEFAULT_BACKUP_DIR))
    print(json.dumps({"status": "starting", "schools": len(schools), "backup": backup_path, "report": str(report_path)}, ensure_ascii=False), flush=True)

    report_rows = []
    for index, school in enumerate(schools, start=1):
        result = deep_extract_school_features(
            school["website_url"],
            tag_rows=tag_rows,
            max_pages=max(0, args.max_pages),
            timeout=args.timeout,
        )
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
            "status": result.get("status") or "",
            "message": result.get("message") or result.get("error") or "",
            "pages_checked": len(result.get("pages_checked") or []),
            "features_found": len(result.get("features") or []),
            "features_inserted": inserted,
            "errors": json.dumps(result.get("errors") or [], ensure_ascii=False),
        }
        report_rows.append(row)
        write_report(report_path, report_rows)
        print(json.dumps({"index": index, "total": len(schools), **row}, ensure_ascii=False), flush=True)

    summary = {}
    inserted_total = 0
    for row in report_rows:
        summary[row["status"]] = summary.get(row["status"], 0) + 1
        inserted_total += int(row["features_inserted"] or 0)
    print(json.dumps({"status": "finished", "summary": summary, "features_inserted": inserted_total, "report": str(report_path), "backup": backup_path}, ensure_ascii=False), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
