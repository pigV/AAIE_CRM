#!/usr/bin/env python3
"""Search Good Schools Guide with Selenium and import reviewed source data.

This script is intentionally separate from the CRM web server. It writes third-party
Good Schools data as Needs Review and does not overwrite official school data.
"""

import argparse
import json
import os
import re
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote_plus


APP_DIR = Path(__file__).resolve().parents[1]
DEFAULT_DB_PATH = APP_DIR / "database" / "aaie_crm.db"
GOOD_SCHOOLS_URL = "https://www.goodschools.com.au"
GOOD_SCHOOLS_SOURCE_NAME = "Good Schools Guide"
GOOD_SCHOOLS_SOURCE_TYPE = "third_party"
GOOD_SCHOOLS_ACTIVITY_CATEGORIES = [
    "Accelerated learning",
    "Activities",
    "Extra-curricular",
    "Music",
    "Sports",
]
GOOD_SCHOOLS_ACTIVITY_CATEGORY_HINTS = {
    "Accelerated learning": ["academic", "extension", "accelerated learning", "high achievers", "加速课程", "Extension Program", "精英学术项目"],
    "Activities": ["activities", "clubs", "student activities", "社团丰富"],
    "Extra-curricular": ["extra-curricular", "co-curricular", "activities", "社团丰富"],
    "Music": ["music", "choir", "band", "ensemble", "orchestra", "音乐"],
    "Sports": ["sports", "sport", "体育"],
}
GOOD_SCHOOLS_ACTIVITY_ITEM_ALIASES = {
    "basketball": ["basketball", "篮球"],
    "tennis": ["tennis", "网球"],
    "debating": ["debating", "debate", "辩论"],
    "chess": ["chess", "club", "clubs", "社团丰富"],
    "choir": ["choir", "合唱团"],
    "concert band": ["concert band", "band", "管乐团"],
    "band": ["band", "管乐团"],
    "orchestra": ["orchestra", "管弦乐团", "交响乐团"],
    "rowing": ["rowing", "赛艇"],
    "swimming": ["swimming", "游泳"],
    "football": ["football", "足球", "澳式足球"],
    "athletics": ["athletics", "田径"],
    "drama": ["drama", "theatre", "戏剧表演"],
    "dance": ["dance", "舞蹈"],
    "public speaking": ["public speaking", "公众演讲"],
    "duke of edinburgh": ["duke of edinburgh", "Duke of Edinburgh"],
    "cadets": ["cadets", "Cadets"],
    "high achievers": ["high achievers", "Extension Program", "精英学术项目"],
    "extension": ["extension", "Extension Program", "加速课程"],
    "accelerated": ["accelerated", "加速课程"],
}


def now_iso():
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def compact_text(value):
    return re.sub(r"\s+", " ", str(value or "")).strip()


def normalize_name(value):
    return re.sub(r"[^a-z0-9]+", " ", str(value or "").lower()).strip()


def normalized_key(value):
    return re.sub(r"[^a-z0-9]+", " ", str(value or "").lower()).strip()


def row_value(row, key, default=""):
    try:
        return row[key] if key in row.keys() and row[key] is not None else default
    except Exception:
        if isinstance(row, dict):
            return row.get(key, default) or default
        return default


def postcode_from_goodschools_url(url):
    match = re.search(r"/in-[a-z0-9-]*?(\d{4})(?:/|$)", str(url or "").lower())
    return match.group(1) if match else ""


def suburb_from_goodschools_url(url):
    match = re.search(r"/in-([a-z0-9-]+?)(?:-\d{4})?(?:/|$)", str(url or "").lower())
    if not match:
        return ""
    suburb = re.sub(r"-\d{4}$", "", match.group(1))
    return normalize_name(suburb)


def is_goodschools_profile_url(url):
    text = str(url or "").lower()
    return "/compare-schools/in-" in text and "/compare-schools/search" not in text


def selenium_by_css():
    try:
        from selenium.webdriver.common.by import By
        return By.CSS_SELECTOR
    except Exception:
        return "css selector"


def selenium_by_tag():
    try:
        from selenium.webdriver.common.by import By
        return By.TAG_NAME
    except Exception:
        return "tag name"


def selenium_enter_key():
    try:
        from selenium.webdriver.common.keys import Keys
        return Keys.ENTER
    except Exception:
        return "\n"


def create_default_driver(headless=True):
    try:
        from selenium import webdriver
        from selenium.webdriver.chrome.options import Options
    except ImportError as exc:
        raise RuntimeError("Selenium 未安装。请先在独立环境安装 selenium 和可用的 ChromeDriver。") from exc
    options = Options()
    if headless:
        options.add_argument("--headless=new")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--window-size=1440,1100")
    chrome_binary = os.environ.get("AAIE_CHROME_BINARY", "").strip()
    for candidate in (
        chrome_binary,
        "/Applications/Google Chrome for Testing.app/Contents/MacOS/Google Chrome for Testing",
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    ):
        if candidate and Path(candidate).exists():
            options.binary_location = candidate
            break
    return webdriver.Chrome(options=options)


def find_first(driver, selectors):
    by_css = selenium_by_css()
    for selector in selectors:
        try:
            matches = driver.find_elements(by_css, selector)
        except Exception:
            matches = []
        if matches:
            return matches[0]
    return None


def visible_body_text(driver):
    try:
        body = driver.find_element(selenium_by_tag(), "body")
        return compact_text(getattr(body, "text", ""))
    except Exception:
        return ""


def table_columns(conn, table_name):
    try:
        return {row["name"] for row in conn.execute(f"PRAGMA table_info({table_name})")}
    except Exception:
        return set()


def insert_filtered(conn, table_name, payload):
    columns = table_columns(conn, table_name)
    values = {key: value for key, value in payload.items() if key in columns}
    if not values:
        return None
    keys = list(values.keys())
    placeholders = ",".join("?" for _ in keys)
    conn.execute(
        f"INSERT INTO {table_name}({','.join(keys)}) VALUES({placeholders})",
        tuple(values[key] for key in keys),
    )
    return conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]


class GoodSchoolsSeleniumImporter:
    def __init__(self, db_path=DEFAULT_DB_PATH, driver_factory=None, headless=True):
        self.db_path = Path(db_path)
        self.driver_factory = driver_factory
        self.headless = headless

    def connect(self):
        conn = sqlite3.connect(self.db_path, timeout=30)
        conn.execute("PRAGMA busy_timeout=30000")
        conn.row_factory = sqlite3.Row
        return conn

    def load_school(self, conn, school_name, school_id=None):
        if school_id:
            return conn.execute("SELECT * FROM schools WHERE id=?", (school_id,)).fetchone()
        exact = conn.execute("SELECT * FROM schools WHERE lower(school_name)=lower(?)", (school_name,)).fetchone()
        if exact:
            return exact
        normalized = normalize_name(school_name)
        for row in conn.execute("SELECT * FROM schools"):
            if normalize_name(row["school_name"]) == normalized:
                return row
        like = conn.execute("SELECT * FROM schools WHERE school_name LIKE ? LIMIT 1", (f"%{school_name}%",)).fetchone()
        return like

    def load_feature_tags(self, conn):
        rows = conn.execute(
            """SELECT t.id,t.category_id,t.tag_name_cn,t.tag_name_en,t.keywords,
                      c.category_name_cn,c.category_name_en
               FROM school_feature_tags t
               LEFT JOIN school_feature_categories c ON c.id=t.category_id
               WHERE COALESCE(t.is_active,1)=1 AND COALESCE(c.is_active,1)=1
               ORDER BY COALESCE(c.sort_order,0),COALESCE(t.sort_order,0),t.id"""
        ).fetchall()
        tags = []
        for row in rows:
            keywords = row["keywords"] or "[]"
            try:
                parsed = json.loads(keywords)
                keywords = parsed if isinstance(parsed, list) else [keywords]
            except Exception:
                keywords = re.split(r"[,，\n]+", keywords)
            for name in (row["tag_name_cn"], row["tag_name_en"]):
                if name:
                    keywords.append(name)
            clean_keywords = []
            seen = set()
            for keyword in keywords:
                value = str(keyword or "").strip()
                if value and value.lower() not in seen:
                    clean_keywords.append(value)
                    seen.add(value.lower())
            tags.append({**dict(row), "keywords": clean_keywords})
        return tags

    def make_driver(self):
        if self.driver_factory:
            return self.driver_factory()
        return create_default_driver(self.headless)

    def search_good_schools(self, driver, school):
        school_name = row_value(school, "school_name")
        suburb = row_value(school, "suburb")
        postcode = row_value(school, "postcode")
        state = row_value(school, "state")
        driver.get(GOOD_SCHOOLS_URL)
        search_box = find_first(
            driver,
            [
                "input[type='search']",
                "input[name*='search' i]",
                "input[placeholder*='school' i]",
                "input[placeholder*='Search' i]",
                "form input[type='text']",
            ],
        )
        if search_box:
            try:
                search_box.clear()
            except Exception:
                pass
            search_query = " ".join(part for part in [school_name, suburb, postcode, state] if part)
            search_box.send_keys(search_query or school_name)
            search_box.send_keys(selenium_enter_key())

        result = self.find_school_result(driver, school)
        if not result:
            fallback_query = " ".join(part for part in [school_name, suburb, postcode] if part)
            driver.get(f"{GOOD_SCHOOLS_URL}/search?keywords={quote_plus(fallback_query or school_name)}")
            result = self.find_school_result(driver, school)
        if not result:
            return {"status": "not_found", "message": "Good Schools 搜索不到该学校。"}
        if isinstance(result, dict) and result.get("status") == "needs_review":
            return result
        href = result.get_attribute("href") if hasattr(result, "get_attribute") else ""
        if href:
            driver.get(href)
        else:
            result.click()
        current_url = getattr(driver, "current_url", "")
        if not is_goodschools_profile_url(current_url):
            return {
                "status": "needs_review",
                "message": "Good Schools 只打开了搜索结果页，没有进入学校详情页；已停止自动写入，请人工确认。",
                "candidate_url": current_url,
            }
        return {"status": "opened", "url": current_url}

    def score_goodschools_result(self, school, link):
        school_name = row_value(school, "school_name")
        expected = normalize_name(school_name)
        suburb = normalize_name(row_value(school, "suburb"))
        postcode = str(row_value(school, "postcode")).strip()
        state = normalize_name(row_value(school, "state"))
        text_raw = str(getattr(link, "text", "") or "")
        href = str(link.get_attribute("href") if hasattr(link, "get_attribute") else "")
        text = normalize_name(text_raw)
        href_text = normalize_name(href)
        combined = f"{text} {href_text}"
        score = 0
        reasons = []
        if "compare schools" in href_text or "compare-schools" in href:
            score += 10
        if expected and expected in combined:
            score += 55
            reasons.append("school_name")
        else:
            name_tokens = [token for token in expected.split() if len(token) >= 4]
            matched = [token for token in name_tokens if token in combined]
            if name_tokens and len(matched) >= max(1, len(name_tokens) - 1):
                score += 35
                reasons.append("school_name_partial")
        href_postcode = postcode_from_goodschools_url(href)
        href_suburb = suburb_from_goodschools_url(href)
        if postcode:
            if href_postcode == postcode or postcode in combined:
                score += 35
                reasons.append("postcode")
            elif href_postcode:
                score -= 45
                reasons.append("postcode_conflict")
        if suburb:
            if suburb in combined or (href_suburb and (suburb == href_suburb or suburb in href_suburb or href_suburb in suburb)):
                score += 28
                reasons.append("suburb")
            elif href_suburb:
                score -= 18
                reasons.append("suburb_conflict")
        if state and state in combined:
            score += 8
            reasons.append("state")
        return {
            "link": link,
            "score": score,
            "href": href,
            "text": text_raw,
            "reasons": reasons,
        }

    def find_school_result(self, driver, school):
        try:
            links = driver.find_elements(selenium_by_css(), "a")
        except Exception:
            links = []
        scored = []
        for link in links:
            href = str(link.get_attribute("href") if hasattr(link, "get_attribute") else "")
            if not is_goodschools_profile_url(href):
                continue
            scored.append(self.score_goodschools_result(school, link))
        if scored:
            scored.sort(key=lambda row: row["score"], reverse=True)
            best = scored[0]
            if best["score"] >= 75:
                return best["link"]
            return {
                "status": "needs_review",
                "message": "Good Schools 搜索结果学校名相似，但区名或邮编无法和 CRM 学校记录自动对应，请人工确认。",
                "candidate_url": best["href"],
                "candidate_text": best["text"],
                "match_score": best["score"],
                "match_reasons": best["reasons"],
            }
        expected = normalize_name(row_value(school, "school_name"))
        page_text = normalize_name(visible_body_text(driver))
        current_url = str(getattr(driver, "current_url", ""))
        if expected and expected in page_text and "goodschools.com.au" in current_url:
            return None
        return None

    def extract_profile(self, school_name, page_text, source_url, activities=None):
        summary = page_text[:420]
        description = page_text[:1200]
        strengths = []
        for label, keywords in {
            "学术": ["academic", "extension", "naplan", "atar", "scholarship"],
            "体育": ["sport", "rowing", "football", "basketball", "swimming", "athletics"],
            "音乐": ["music", "orchestra", "choir", "band", "instrumental"],
            "STEM": ["stem", "robotics", "coding", "science", "technology"],
            "寄宿": ["boarding", "homestay"],
            "领导力": ["leadership"],
            "户外教育": ["outdoor education", "camp"],
        }.items():
            if any(re.search(rf"\b{re.escape(keyword)}\b", page_text, re.I) for keyword in keywords):
                strengths.append(label)
        return {
            "school_name": school_name,
            "source_url": source_url,
            "profile_url": source_url,
            "third_party_description": description,
            "third_party_summary": summary,
            "strengths_json": json.dumps(strengths, ensure_ascii=False),
            "raw_extract_json": json.dumps(
                {
                    "source": GOOD_SCHOOLS_SOURCE_NAME,
                    "extracted_text_chars": len(page_text),
                    "activity_categories": activities or [],
                },
                ensure_ascii=False,
            ),
            "fetch_status": "success",
            "review_status": "Needs Review",
        }

    def extract_features(self, page_text, tags, source_url):
        lower_text = page_text.lower()
        features = []
        for tag in tags:
            matched = []
            hits = 0
            for keyword in tag["keywords"]:
                count = lower_text.count(str(keyword).lower())
                if count:
                    matched.append(keyword)
                    hits += count
            if not matched:
                continue
            best = str(matched[0])
            index = lower_text.find(best.lower())
            start = max(0, index - 100)
            end = min(len(page_text), index + len(best) + 180)
            features.append(
                {
                    "category_id": tag["category_id"],
                    "tag_id": tag["id"],
                    "tag_name_cn": tag["tag_name_cn"],
                    "tag_name_en": tag["tag_name_en"],
                    "confidence_rate": min(96, 55 + min(hits, 4) * 10),
                    "source_url": source_url,
                    "source_excerpt": page_text[start:end],
                    "matched_keywords": matched,
                }
            )
        return features

    def activity_count_from_text(self, page_text, category):
        match = re.search(rf"{re.escape(category)}\s*\((\d+)\)", page_text or "", re.I)
        return int(match.group(1)) if match else None

    def find_activity_clickable(self, driver, category):
        by_css = selenium_by_css()
        for selector in ("button", "[role='button']", "[role=button]", "a"):
            try:
                elements = driver.find_elements(by_css, selector)
            except Exception:
                elements = []
            for element in elements:
                text = compact_text(getattr(element, "text", ""))
                if text and category.lower() in text.lower():
                    return element
        return None

    def known_activity_items_from_text(self, text):
        normalized = normalized_key(text)
        items = []
        seen = set()
        for canonical, aliases in GOOD_SCHOOLS_ACTIVITY_ITEM_ALIASES.items():
            for alias in aliases:
                alias_key = normalized_key(alias)
                if alias_key and re.search(rf"\b{re.escape(alias_key)}\b", normalized):
                    if canonical not in seen:
                        items.append(alias)
                        seen.add(canonical)
                    break
        return items

    def parse_activity_items(self, category, text):
        clean = compact_text(text)
        if not clean:
            return []
        clean = re.sub(rf"^{re.escape(category)}\s*(?:\(\d+\))?\s*", "", clean, flags=re.I)
        known_items = self.known_activity_items_from_text(clean)
        if known_items:
            return known_items
        pieces = re.split(r"[\n\r,;•·|]+", text or "")
        items = []
        seen = set()
        stop_words = {normalized_key(value) for value in GOOD_SCHOOLS_ACTIVITY_CATEGORIES}
        for piece in pieces:
            value = compact_text(re.sub(r"\(\d+\)", "", piece))
            key = normalized_key(value)
            if not value or key in stop_words or len(value) > 80:
                continue
            if key and key not in seen:
                items.append(value)
                seen.add(key)
        return items[:80]

    def extract_goodschools_activities(self, driver, page_text, source_url):
        activities = []
        for category in GOOD_SCHOOLS_ACTIVITY_CATEGORIES:
            count = self.activity_count_from_text(page_text, category)
            detail_text = ""
            element = self.find_activity_clickable(driver, category)
            if element:
                try:
                    before = visible_body_text(driver)
                    element.click()
                    after = visible_body_text(driver)
                    detail_text = after if after and after != before else after or before
                except Exception:
                    detail_text = ""
            if not detail_text:
                pattern = rf"{re.escape(category)}\s*(?:\(\d+\))?(.{{0,500}})"
                match = re.search(pattern, page_text or "", re.I)
                detail_text = match.group(1) if match else ""
            items = self.parse_activity_items(category, detail_text)
            if count is not None or items:
                activities.append(
                    {
                        "category": category,
                        "count": count if count is not None else len(items),
                        "items": items,
                        "source_url": source_url,
                    }
                )
        return activities

    def tag_search_terms(self, tag):
        terms = [tag.get("tag_name_cn") or "", tag.get("tag_name_en") or ""]
        raw_keywords = tag.get("keywords") or []
        if isinstance(raw_keywords, list):
            terms.extend(raw_keywords)
        else:
            try:
                parsed = json.loads(raw_keywords or "[]")
                if isinstance(parsed, list):
                    terms.extend(parsed)
            except Exception:
                terms.extend(re.split(r"[,，\n]+", raw_keywords or ""))
        return [str(term or "").strip() for term in terms if str(term or "").strip()]

    def activity_terms(self, category, item=""):
        terms = []
        item_key = normalized_key(item)
        has_specific_item = bool(item_key and item_key != normalized_key(category))
        if item:
            terms.insert(0, item)
        for canonical, aliases in GOOD_SCHOOLS_ACTIVITY_ITEM_ALIASES.items():
            canonical_key = normalized_key(canonical)
            alias_keys = {normalized_key(alias) for alias in aliases}
            if item_key and (item_key == canonical_key or item_key in alias_keys or canonical_key in item_key):
                terms = list(aliases) + terms
                break
        if not has_specific_item:
            terms.extend(GOOD_SCHOOLS_ACTIVITY_CATEGORY_HINTS.get(category, []))
        deduped = []
        seen = set()
        for term in terms:
            key = normalized_key(term)
            if key and key not in seen:
                deduped.append(str(term))
                seen.add(key)
        return deduped

    def score_activity_tag(self, tag, category, terms):
        category_text = normalized_key(f"{tag.get('category_name_cn') or ''} {tag.get('category_name_en') or ''}")
        tag_terms = self.tag_search_terms(tag)
        tag_keys = [normalized_key(term) for term in tag_terms]
        tag_blob = " ".join(tag_keys)
        score = 0
        category_hints = " ".join(normalized_key(term) for term in GOOD_SCHOOLS_ACTIVITY_CATEGORY_HINTS.get(category, []))
        if category_hints and any(part and part in category_text for part in ("academic", "sports", "music", "activities", "leadership")):
            if any(part in category_text for part in normalized_key(category_hints).split()):
                score += 16
        for term in terms:
            key = normalized_key(term)
            if not key:
                continue
            if key in tag_keys:
                score += 85
            elif key and re.search(rf"\b{re.escape(key)}\b", tag_blob):
                score += 65
            elif key in tag_blob:
                score += 42
        return score

    def best_activity_tag(self, tags, category, item=""):
        terms = self.activity_terms(category, item)
        best_tag = None
        best_score = 0
        for tag in tags:
            score = self.score_activity_tag(tag, category, terms)
            if score > best_score:
                best_tag = tag
                best_score = score
        if best_tag and best_score >= 50:
            return best_tag, min(96, 62 + min(best_score, 120) // 4), terms
        return None, 0, terms

    def extract_activity_features(self, activities, tags, source_url):
        features = []
        seen = set()
        for activity in activities:
            category = activity.get("category") or ""
            candidates = activity.get("items") or [category]
            if activity.get("items"):
                candidates = [category] + list(activity.get("items") or [])
            for item in candidates:
                tag, confidence, terms = self.best_activity_tag(tags, category, item)
                if not tag or tag["id"] in seen:
                    continue
                seen.add(tag["id"])
                item_label = item if item != category else category
                source_excerpt = f"GoodSchools Activities & Support Staff / {category}: {item_label}"
                features.append(
                    {
                        "category_id": tag["category_id"],
                        "tag_id": tag["id"],
                        "tag_name_cn": tag["tag_name_cn"],
                        "tag_name_en": tag["tag_name_en"],
                        "confidence_rate": confidence,
                        "source_url": source_url,
                        "source_excerpt": source_excerpt,
                        "matched_keywords": [term for term in terms[:8]],
                        "notes_prefix": f"GoodSchools Activities: {category}",
                    }
                )
        return features

    def merge_features(self, features):
        merged = {}
        for feature in features:
            key = feature.get("tag_id")
            existing = merged.get(key)
            if existing is None or int(feature.get("confidence_rate") or 0) > int(existing.get("confidence_rate") or 0):
                merged[key] = dict(feature)
            elif existing:
                existing_keywords = set(existing.get("matched_keywords") or [])
                for keyword in feature.get("matched_keywords") or []:
                    if keyword not in existing_keywords:
                        existing.setdefault("matched_keywords", []).append(keyword)
                        existing_keywords.add(keyword)
        return list(merged.values())

    def extract_academic(self, page_text, source_url):
        indicators = []
        for indicator in ["reading", "writing", "spelling", "grammar", "numeracy", "naplan", "icsea", "atar"]:
            pattern = rf"(.{{0,80}}{indicator}.{{0,120}})"
            for match in re.finditer(pattern, page_text, re.I):
                raw = compact_text(match.group(1))
                value = re.search(r"\b(\d{2,4}(?:\.\d+)?)\b", raw)
                year_level = re.search(r"\bYear\s*(\d{1,2})\b", raw, re.I)
                data_year = re.search(r"\b(20\d{2})\b", raw)
                indicators.append(
                    {
                        "indicator_name": "NAPLAN" if indicator == "naplan" else indicator.upper() if indicator in {"icsea", "atar"} else f"NAPLAN {indicator.title()}",
                        "year_level": f"Year {year_level.group(1)}" if year_level else "",
                        "data_year": data_year.group(1) if data_year else "",
                        "indicator_value": value.group(1) if value else "",
                        "comparison_notes": raw,
                        "raw_text": raw,
                        "source_url": source_url,
                    }
                )
        deduped = []
        seen = set()
        for item in indicators:
            key = (item["indicator_name"], item["year_level"], item["indicator_value"], item["raw_text"])
            if key not in seen:
                seen.add(key)
                deduped.append(item)
        return deduped[:25]

    def persist(self, conn, school, profile, features, academic):
        stamp = now_iso()
        school_id = school["id"]
        source_url = profile["source_url"]
        conn.execute(
            "DELETE FROM school_third_party_profiles WHERE school_id=? AND source_name=? AND source_url=?",
            (school_id, GOOD_SCHOOLS_SOURCE_NAME, source_url),
        )
        conn.execute(
            "DELETE FROM school_feature_tag_assignments WHERE school_id=? AND source_type=? AND source_url=?",
            (school_id, GOOD_SCHOOLS_SOURCE_NAME, source_url),
        )
        conn.execute(
            "DELETE FROM school_academic_indicators WHERE school_id=? AND source_name=? AND source_url=?",
            (school_id, GOOD_SCHOOLS_SOURCE_NAME, source_url),
        )
        insert_filtered(
            conn,
            "school_third_party_profiles",
            {
                **profile,
                "school_id": school_id,
                "source_name": GOOD_SCHOOLS_SOURCE_NAME,
                "source_type": GOOD_SCHOOLS_SOURCE_TYPE,
                "fetched_at": stamp,
                "created_at": stamp,
                "updated_at": stamp,
                "notes": "Selenium Good Schools search import; Needs Review",
            },
        )
        for feature in features:
            insert_filtered(
                conn,
                "school_feature_tag_assignments",
                {
                    "school_id": school_id,
                    "category_id": feature["category_id"],
                    "tag_id": feature["tag_id"],
                    "assignment_type": "AutoSuggested",
                    "status": "Suggested",
                    "confidence_rate": feature["confidence_rate"],
                    "source_url": source_url,
                    "source_title": GOOD_SCHOOLS_SOURCE_NAME,
                    "source_type": GOOD_SCHOOLS_SOURCE_NAME,
                    "source_excerpt": feature["source_excerpt"],
                    "collected_at": stamp,
                    "collected_by_system": 1,
                    "notes": (feature.get("notes_prefix") or "Matched keywords") + ": " + ", ".join(map(str, feature["matched_keywords"])),
                    "created_at": stamp,
                    "updated_at": stamp,
                },
            )
        for row in academic:
            insert_filtered(
                conn,
                "school_academic_indicators",
                {
                    **row,
                    "school_id": school_id,
                    "source_name": GOOD_SCHOOLS_SOURCE_NAME,
                    "source_type": GOOD_SCHOOLS_SOURCE_TYPE,
                    "source_url": source_url,
                    "fetched_at": stamp,
                    "review_status": "Needs Review",
                    "notes": "Selenium Good Schools academic extraction",
                    "created_at": stamp,
                    "updated_at": stamp,
                },
            )
        conn.commit()

    def run(self, school_name, school_id=None):
        school_name = compact_text(school_name)
        if not school_name:
            return {"status": "error", "message": "school_name is required"}
        conn = self.connect()
        try:
            school = self.load_school(conn, school_name, school_id=school_id)
            if not school:
                return {"status": "crm_school_not_found", "message": "CRM 学校库中找不到该学校，请先建立学校记录。"}
            driver = self.make_driver()
            try:
                opened = self.search_good_schools(driver, school)
                if opened["status"] != "opened":
                    return opened
                source_url = opened.get("url") or getattr(driver, "current_url", "") or GOOD_SCHOOLS_URL
                page_text = visible_body_text(driver)
                activities = self.extract_goodschools_activities(driver, page_text, source_url)
            finally:
                try:
                    driver.quit()
                except Exception:
                    pass
            if not page_text:
                return {"status": "empty_page", "message": "Good Schools 页面没有可读取文字。", "school_id": school["id"]}
            tags = self.load_feature_tags(conn)
            profile = self.extract_profile(school_name, page_text, source_url, activities=activities)
            activity_features = self.extract_activity_features(activities, tags, source_url)
            features = self.merge_features(activity_features)
            academic = self.extract_academic(page_text, source_url)
            self.persist(conn, school, profile, features, academic)
            return {
                "status": "success",
                "school_id": school["id"],
                "school_name": school["school_name"],
                "source_url": source_url,
                "profile": profile,
                "activities": activities,
                "features": features,
                "academic": academic,
                "message": f"Good Schools 已导入：特色 {len(features)} 条，学术数据 {len(academic)} 条，均为 Needs Review。",
            }
        except Exception as exc:
            return {"status": "error", "message": str(exc)}
        finally:
            conn.close()


def main(argv=None):
    parser = argparse.ArgumentParser(description="Search Good Schools Guide and import third-party school data into AAIE CRM.")
    parser.add_argument("school_name", help="School name to search")
    parser.add_argument("--school-id", type=int, default=None, help="Optional CRM school id")
    parser.add_argument("--db", default=str(DEFAULT_DB_PATH), help="CRM sqlite database path")
    parser.add_argument("--no-headless", action="store_true", help="Show Chrome window")
    args = parser.parse_args(argv)
    importer = GoodSchoolsSeleniumImporter(db_path=Path(args.db), headless=not args.no_headless)
    result = importer.run(args.school_name, school_id=args.school_id)
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    return 0 if result.get("status") in {"success", "not_found", "crm_school_not_found"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
