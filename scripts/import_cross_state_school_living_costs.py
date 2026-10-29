#!/usr/bin/env python3
"""Import cross-state school surrounding-area living cost data into AAIE CRM.

Default mode is dry-run. Use --apply to write to database/aaie_crm.db.
Only official median sources that are current enough and machine-readable are
imported. Official sources that are outdated, subscription-only, PDF-only, or
not suburb/dwelling-market medians are recorded in OFFICIAL_SOURCES but are not
used to populate market values.
"""

import argparse
import csv
import json
import re
import sqlite3
import sys
import urllib.parse
import urllib.request
import zipfile
from datetime import date, datetime, timezone
from pathlib import Path

from openpyxl import load_workbook

APP_DIR = Path(__file__).resolve().parents[1]
if str(APP_DIR) not in sys.path:
    sys.path.insert(0, str(APP_DIR))

from app import refresh_school_living_cost_summary  # noqa: E402

DB_PATH = APP_DIR / "database" / "aaie_crm.db"
SOURCE_DIR = APP_DIR / "school_data_sources" / "cross_state_living_costs"
SA_SOURCE_DIR = APP_DIR / "school_data_sources" / "sa_living_costs"

EXISTING_CRM_STATES = ("ACT", "NSW", "QLD", "SA", "TAS", "WA")

SA_CKAN_API = "https://data.sa.gov.au/data/api/3/action/package_show?id="
DATA_GOV_AU_CKAN_API = "https://data.gov.au/data/api/3/action/package_show?id="
SA_RENTAL_PACKAGE = "private-rent-report"
SA_PROPERTY_HOUSE_PACKAGE = "metro-median-house-sales"
TAS_RENTAL_PACKAGE = "rental-bond-and-rental-data-july-2024-to-june-2025"
NSW_RENTAL_BOND_PAGE = "https://www.nsw.gov.au/housing-and-construction/rental-forms-surveys-and-data/rental-bond-data"
QLD_RTA_RENTAL_FILE = "https://www.rta.qld.gov.au/sites/default/files/2023-04/rta-bond-statistics.xlsx"

OFFICIAL_SOURCES = {
    "SA": {
        "rental_market": "https://data.sa.gov.au/data/dataset/private-rent-report",
        "property_house": "https://data.sa.gov.au/data/dataset/metro-median-house-sales",
        "property_unit": "not_found_in_official_machine_readable_sources",
    },
    "NSW": {
        "rental_market": NSW_RENTAL_BOND_PAGE,
        "rental_method": "official Rental Bond Lodgement raw weekly rent data, calculated to postcode median by CRM",
        "property_market": "user_supplied_official_nsw_property_sales_export",
        "property_method": "official NSW property sales export rows, calculated to suburb House/Unit median sale price by CRM",
        "checked_property_market": {
            "source": "NSW data portal - Rent and Sales Tables",
            "url": "https://data.nsw.gov.au/data/dataset/housing-nsw-rent-and-sales-tables",
            "status": "not_imported_outdated_sales_tables_only_confirmed_to_2015",
        },
    },
    "QLD": {
        "rental_market": "https://www.rta.qld.gov.au/forms-resources/rta-quarterly-data/median-rents-quarterly-data",
        "property_market": "not_found_in_official_public_machine_readable_sources",
        "checked_property_market": {
            "source": "Queensland Government data/search portals",
            "url": "https://www.data.qld.gov.au/",
            "status": "not_imported_no_current_suburb_house_unit_median_sale_source_confirmed",
        },
    },
    "WA": {
        "rental_market": "not_found_in_official_public_machine_readable_sources",
        "property_market": "official_landgate_sales_evidence_requires_paid_access_or_user_supplied_export",
        "checked_property_market": {
            "source": "Landgate Sales Evidence data",
            "url": "https://catalogue.data.wa.gov.au/dataset/sales-evidence-data",
            "status": "not_imported_subscription_or_lookup_resources_only",
        },
    },
    "ACT": {
        "rental_market": "not_found_in_official_public_machine_readable_sources",
        "property_market": "not_found_in_official_public_suburb_house_unit_median_sources",
        "checked_property_market": {
            "source": "ACT Land and Property Report",
            "url": "https://www.planning.act.gov.au/__data/assets/pdf_file/0004/2630362/act-land-and-property-report-june-2024.pdf",
            "status": "not_imported_pdf_land_supply_report_not_house_unit_market_median",
        },
    },
    "TAS": {
        "rental_market": f"https://data.gov.au/data/dataset/{TAS_RENTAL_PACKAGE}",
        "rental_method": "official Rental Bond and Rental Data active bonds, calculated to suburb median by CRM",
        "property_market": "not_found_in_official_public_machine_readable_sources",
        "checked_property_market": {
            "source": "Tasmanian Government/data.gov.au/LIST source probes",
            "url": "https://data.gov.au/data/",
            "status": "not_imported_no_current_suburb_house_unit_median_sale_source_confirmed",
        },
    },
}

SA_RENTAL_SOURCE_NAME = "SA Private Rental Report - quarterly median rents"
SA_PROPERTY_SOURCE_NAME = "SA Metropolitan Median House Sales"
NSW_RENTAL_SOURCE_NAME = "NSW Rental Bond Lodgement - postcode-derived median rents"
NSW_PROPERTY_SALES_SOURCE_NAME = "NSW official property sales export - CRM-calculated suburb medians"
QLD_RENTAL_SOURCE_NAME = "QLD RTA Bond Statistics - suburb median rents"
QLD_RENTAL_DETAIL_SOURCE_NAME = "QLD RTA Bond Statistics - official median rents by area"
TAS_RENTAL_SOURCE_NAME = "TAS Rental Bond and Rental Data - active bond median rents"
SCHOOL_COORDINATE_GEO_SOURCE = "existing_school_coordinate_centroid"
DOWNLOAD_HEADERS = {"User-Agent": "Mozilla/5.0"}


class DbAdapter:
    def __init__(self, conn):
        self.conn = conn

    def one(self, sql, params=()):
        return self.conn.execute(sql, params).fetchone()

    def all(self, sql, params=()):
        return self.conn.execute(sql, params).fetchall()

    def execute(self, sql, params=()):
        cur = self.conn.execute(sql, params)
        return cur.lastrowid


def stamp():
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def clean_number(value):
    if value is None:
        return None
    text = re.sub(r"[^0-9.]", "", str(value))
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def clean_int(value):
    number = clean_number(value)
    if number is None:
        return None
    return int(number)


def clean_suburb(value):
    text = re.sub(r"\s+", " ", str(value or "").strip())
    if not text:
        return ""
    return text.title()


def median_value(values):
    clean = sorted(value for value in (clean_number(item) for item in values) if value is not None)
    if not clean:
        return None
    mid = len(clean) // 2
    if len(clean) % 2:
        result = clean[mid]
    else:
        result = (clean[mid - 1] + clean[mid]) / 2
    return int(result) if float(result).is_integer() else round(result, 2)


def rental_bucket_from_dwelling(value):
    text = str(value or "").strip().lower()
    if "townhouse" in text:
        return ""
    if text in {"f", "flat"} or any(token in text for token in ("flat", "unit", "apartment")):
        return "Unit"
    if text in {"h", "house"} or "house" in text:
        return "House"
    return ""


def bedrooms_from_dwelling(value):
    text = str(value or "").strip()
    match = re.search(r"\b(\d+)\b", text)
    return match.group(1) if match else ""


def dry_run_label(apply):
    return "apply" if apply else "dry_run"


def ckan_package(api_base, package_name):
    url = api_base + urllib.parse.quote(package_name)
    request = urllib.request.Request(url, headers=DOWNLOAD_HEADERS)
    with urllib.request.urlopen(request, timeout=30) as response:
        payload = json.loads(response.read().decode("utf-8"))
    if not payload.get("success"):
        raise RuntimeError(f"CKAN package lookup failed: {package_name}")
    return payload["result"]


def resource_sort_key(item):
    name = str(item.get("name") or item.get("url") or "").lower()
    years = [int(value) for value in re.findall(r"20\d{2}", name)]
    year = years[-1] if years else 0
    month_order = {
        "jan": 1, "january": 1, "feb": 2, "february": 2,
        "mar": 3, "march": 3, "apr": 4, "april": 4,
        "may": 5, "jun": 6, "june": 6, "jul": 7, "july": 7,
        "aug": 8, "august": 8, "sep": 9, "sept": 9, "september": 9,
        "oct": 10, "october": 10, "nov": 11, "november": 11,
        "dec": 12, "december": 12,
    }
    month = 0
    for token, value in month_order.items():
        if re.search(rf"\b{token}\b", name):
            month = max(month, value)
    if re.search(r"\b(q1|mar|march|03)\b", name):
        month = max(month, 3)
    elif re.search(r"\b(q2|jun|june|06)\b", name):
        month = max(month, 6)
    elif re.search(r"\b(q3|sep|sept|september|09)\b", name):
        month = max(month, 9)
    elif re.search(r"\b(q4|dec|december|12)\b", name):
        month = max(month, 12)
    return year, month, name


def latest_resource_url(api_base, package_name):
    package = ckan_package(api_base, package_name)
    resources = [
        item for item in package.get("resources", [])
        if item.get("url") and str(item.get("format") or "").upper() in {"XLS", "XLSX"}
    ]
    if not resources:
        raise RuntimeError(f"No supported Excel resource found for {package_name}")
    latest = sorted(resources, key=resource_sort_key)[-1]
    return latest["url"], latest.get("name") or latest["url"]


def download_file(url, target_dir):
    target_dir.mkdir(parents=True, exist_ok=True)
    parsed = urllib.parse.urlparse(url)
    filename = urllib.parse.unquote(Path(parsed.path).name) or "downloaded-resource.xlsx"
    target = target_dir / filename
    request = urllib.request.Request(url, headers=DOWNLOAD_HEADERS)
    with urllib.request.urlopen(request, timeout=90) as response:
        target.write_bytes(response.read())
    return target


def maybe_download_sa_files():
    target = SOURCE_DIR / "sa" / datetime.now().strftime("%Y%m%d_%H%M%S")
    rental_url, rental_name = latest_resource_url(SA_CKAN_API, SA_RENTAL_PACKAGE)
    house_url, house_name = latest_resource_url(SA_CKAN_API, SA_PROPERTY_HOUSE_PACKAGE)
    return {
        "sa_rental_file": {
            "name": rental_name,
            "path": str(download_file(rental_url, target)),
            "url": rental_url,
        },
        "sa_property_house_file": {
            "name": house_name,
            "path": str(download_file(house_url, target)),
            "url": house_url,
        },
    }


def html_links(url, pattern):
    request = urllib.request.Request(url, headers=DOWNLOAD_HEADERS)
    with urllib.request.urlopen(request, timeout=45) as response:
        text = response.read().decode("utf-8", "replace")
    matches = []
    for href in re.findall(r'href=["\']([^"\']+)["\']', text, flags=re.I):
        full_url = urllib.parse.urljoin(url, href)
        if re.search(pattern, full_url, flags=re.I):
            matches.append(full_url)
    return matches


def maybe_download_nsw_rental_file():
    links = html_links(NSW_RENTAL_BOND_PAGE, r"rental.*bond.*lodgement.*\.xlsx|rentalbond_lodgements.*\.xlsx")
    monthly = [link for link in links if "year" not in link.lower()]
    if not monthly:
        raise RuntimeError("No NSW Rental Bond Lodgement monthly XLSX link found.")
    url = monthly[0]
    path = download_file(url, SOURCE_DIR / "nsw" / datetime.now().strftime("%Y%m%d_%H%M%S"))
    return {"name": Path(urllib.parse.urlparse(url).path).name, "path": str(path), "url": url}


def maybe_download_qld_rental_file():
    path = download_file(QLD_RTA_RENTAL_FILE, SOURCE_DIR / "qld" / datetime.now().strftime("%Y%m%d_%H%M%S"))
    return {"name": Path(urllib.parse.urlparse(QLD_RTA_RENTAL_FILE).path).name, "path": str(path), "url": QLD_RTA_RENTAL_FILE}


def maybe_download_tas_rental_file():
    package = ckan_package(DATA_GOV_AU_CKAN_API, TAS_RENTAL_PACKAGE)
    resources = [
        item for item in package.get("resources", [])
        if item.get("url") and str(item.get("format") or "").lower() in {"xlsx", "excel (.xlsx)", ".xlsx"}
    ]
    if not resources:
        raise RuntimeError("No TAS Rental Bond XLSX resource found.")
    latest = sorted(resources, key=resource_sort_key)[-1]
    path = download_file(latest["url"], SOURCE_DIR / "tas" / datetime.now().strftime("%Y%m%d_%H%M%S"))
    return {"name": latest.get("name") or Path(path).name, "path": str(path), "url": latest["url"]}


def quarter_from_sa_rental_workbook(path):
    workbook = load_workbook(path, data_only=True, read_only=True)
    sheet = workbook["Suburb"] if "Suburb" in workbook.sheetnames else workbook.worksheets[0]
    for row in sheet.iter_rows(min_row=1, max_row=20, values_only=True):
        text = " ".join(str(cell or "") for cell in row)
        match = re.search(r"1 January\s*-\s*31 March\s*(20\d{2})", text, flags=re.I)
        if match:
            return f"{match.group(1)} Q1"
        match = re.search(r"1 April\s*-\s*30 June\s*(20\d{2})", text, flags=re.I)
        if match:
            return f"{match.group(1)} Q2"
        match = re.search(r"1 July\s*-\s*30 September\s*(20\d{2})", text, flags=re.I)
        if match:
            return f"{match.group(1)} Q3"
        match = re.search(r"1 October\s*-\s*31 December\s*(20\d{2})", text, flags=re.I)
        if match:
            return f"{match.group(1)} Q4"
    name = Path(path).stem
    match = re.search(r"(20\d{2})[-_ ]?(0[369]|12)", name)
    if not match:
        return name
    year, month = match.groups()
    return f"{year} Q{ {'03': 1, '06': 2, '09': 3, '12': 4}[month] }"


def parse_sa_rental_rows(path, quarter=""):
    workbook = load_workbook(path, data_only=True, read_only=True)
    sheet = workbook["Suburb"] if "Suburb" in workbook.sheetnames else workbook.worksheets[0]
    quarter = quarter or quarter_from_sa_rental_workbook(path)
    rows = []
    for row in sheet.iter_rows(min_row=16, values_only=True):
        suburb = clean_suburb(row[0] if row else "")
        if not suburb or suburb in {"Metro", "Non Metro", "South Australia", "Row Labels"}:
            continue
        unit_median = clean_number(row[10] if len(row) > 10 else None)
        house_median = clean_number(row[20] if len(row) > 20 else None)
        if unit_median is not None:
            rows.append({
                "suburb": suburb,
                "property_type": "Unit",
                "bedrooms": "",
                "median_weekly_rent": int(unit_median) if float(unit_median).is_integer() else unit_median,
                "quarter": quarter,
            })
        if house_median is not None:
            rows.append({
                "suburb": suburb,
                "property_type": "House",
                "bedrooms": "",
                "median_weekly_rent": int(house_median) if float(house_median).is_integer() else house_median,
                "quarter": quarter,
            })
    return rows


def period_from_sa_property_headers(headers):
    periods = []
    for value in headers:
        match = re.search(r"Median\s+([1-4])Q\s+(20\d{2})", str(value or ""), flags=re.I)
        if match:
            quarter, year = match.groups()
            periods.append((int(year), int(quarter), f"{year} Q{quarter}"))
    return sorted(periods)[-1][2] if periods else ""


def parse_sa_property_house_rows(path, period=""):
    workbook = load_workbook(path, data_only=True, read_only=True)
    sheet = workbook.worksheets[0]
    iterator = sheet.iter_rows(values_only=True)
    headers = next(iterator, ())
    period = period or period_from_sa_property_headers(headers) or Path(path).stem
    header_text = [str(value or "").strip().lower() for value in headers]
    suburb_index = header_text.index("suburb") if "suburb" in header_text else 1
    year = period[:4]
    quarter = period[-1:]
    target = f"{quarter}q {year}".lower()
    sales_index = next((idx for idx, value in enumerate(header_text) if value.startswith("sales") and target in value), 4)
    median_index = next((idx for idx, value in enumerate(header_text) if value.startswith("median") and target in value), 5)
    parsed = []
    for row in iterator:
        suburb = clean_suburb(row[suburb_index] if len(row) > suburb_index else "")
        price = clean_number(row[median_index] if len(row) > median_index else None)
        if not suburb or price is None:
            continue
        parsed.append({
            "suburb": suburb,
            "property_type": "House",
            "median_sale_price": int(price) if float(price).is_integer() else price,
            "period": period,
            "sales_count": clean_int(row[sales_index] if len(row) > sales_index else None),
        })
    return parsed


def period_from_filename(path):
    name = Path(path).stem.lower()
    months = {
        "january": "01", "jan": "01", "february": "02", "feb": "02", "march": "03", "mar": "03",
        "april": "04", "apr": "04", "may": "05", "june": "06", "jun": "06", "july": "07", "jul": "07",
        "august": "08", "aug": "08", "september": "09", "sep": "09", "october": "10", "oct": "10",
        "november": "11", "nov": "11", "december": "12", "dec": "12",
    }
    year_match = re.search(r"(20\d{2})", name)
    year = year_match.group(1) if year_match else ""
    for token, month in months.items():
        if re.search(rf"(?:^|[^a-z]){token}(?:[^a-z]|$)", name):
            return f"{year}-{month}" if year else token.title()
    return Path(path).stem


def quarter_label(year, month_name):
    month = str(month_name or "").strip().lower()[:3]
    quarter = {"mar": 1, "jun": 2, "sep": 3, "dec": 4}.get(month)
    if not quarter:
        return f"{year} {str(month_name).title()}".strip()
    return f"{year} Q{quarter}"


def parse_nsw_rental_rows(path, quarter=""):
    workbook = load_workbook(path, data_only=True, read_only=True)
    sheet = next((ws for ws in workbook.worksheets if "lodg" in ws.title.lower()), workbook.worksheets[0])
    rows_by_key = {}
    quarter = quarter or period_from_filename(path)
    header_index = None
    headers = []
    for idx, row in enumerate(sheet.iter_rows(values_only=True), start=1):
        values = [str(cell or "").strip().lower() for cell in row]
        if "postcode" in values and "weekly rent" in values:
            header_index = idx
            headers = values
            break
    if not header_index:
        return []
    index = {name: headers.index(name) for name in ("postcode", "dwelling type", "bedrooms", "weekly rent") if name in headers}
    for row in sheet.iter_rows(min_row=header_index + 1, values_only=True):
        postcode = re.sub(r"[^0-9]", "", str(row[index["postcode"]] if len(row) > index["postcode"] else ""))
        bucket = rental_bucket_from_dwelling(row[index["dwelling type"]] if len(row) > index["dwelling type"] else "")
        bedrooms = str(row[index["bedrooms"]] if len(row) > index["bedrooms"] else "").strip()
        rent = clean_number(row[index["weekly rent"]] if len(row) > index["weekly rent"] else None)
        if not postcode or not bucket or rent is None or rent < 100:
            continue
        rows_by_key.setdefault((postcode, bucket, bedrooms), []).append(rent)
    parsed = []
    for (postcode, bucket, bedrooms), values in sorted(rows_by_key.items()):
        median = median_value(values)
        if median is not None:
            parsed.append({
                "postcode": postcode,
                "property_type": bucket,
                "bedrooms": bedrooms,
                "median_weekly_rent": median,
                "quarter": quarter,
            })
    return parsed


def canonical_header(value):
    return re.sub(r"[^a-z0-9]", "", str(value or "").strip().lower())


def find_header_value(row, aliases):
    aliases = {canonical_header(alias) for alias in aliases}
    for key, value in row.items():
        if canonical_header(key) in aliases:
            return value
    return None


def property_sale_bucket(value):
    text = str(value or "").strip().lower()
    if not text:
        return ""
    if any(token in text for token in ("unit", "apartment", "flat", "home unit", "strata")):
        return "Unit"
    if any(token in text for token in ("house", "residence", "dwelling", "detached", "terrace", "semi")):
        return "House"
    return ""


def parse_sale_date(value):
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value or "").strip()
    if not text:
        return None
    text = re.sub(r"\s+", " ", text)
    for fmt in ("%Y-%m-%d", "%Y%m%d", "%d/%m/%Y", "%d-%m-%Y", "%Y/%m/%d", "%d %b %Y", "%d %B %Y"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            pass
    return None


def sale_quarter_key_and_label(sale_date):
    quarter = ((sale_date.month - 1) // 3) + 1
    labels = {
        1: "Jan-Mar",
        2: "Apr-Jun",
        3: "Jul-Sep",
        4: "Oct-Dec",
    }
    return (sale_date.year, quarter), f"{labels[quarter]} {sale_date.year}"


def csv_records(path):
    with open(path, newline="", encoding="utf-8-sig") as handle:
        return [dict(row) for row in csv.DictReader(handle)]


def xlsx_records(path):
    workbook = load_workbook(path, data_only=True, read_only=True)
    records = []
    required_aliases = {
        "suburb": {"suburb", "locality", "propertysuburb", "addresssuburb", "town"},
        "price": {"saleprice", "purchaseprice", "price", "transferprice", "consideration", "contractprice"},
    }
    for sheet in workbook.worksheets:
        rows = list(sheet.iter_rows(values_only=True))
        header_index = None
        headers = []
        for idx, row in enumerate(rows[:40]):
            candidate = [str(cell or "").strip() for cell in row]
            canonical = {canonical_header(cell) for cell in candidate}
            if canonical & required_aliases["suburb"] and canonical & required_aliases["price"]:
                header_index = idx
                headers = candidate
                break
        if header_index is None:
            continue
        for row in rows[header_index + 1:]:
            records.append({headers[idx]: row[idx] if idx < len(row) else None for idx in range(len(headers))})
    return records


def nsw_psi_bucket(fields):
    unit_number = fields[6].strip() if len(fields) > 6 else ""
    nature = fields[17].strip().upper() if len(fields) > 17 else ""
    strata_lot = fields[19].strip() if len(fields) > 19 else ""
    if nature and nature != "R":
        return ""
    if unit_number or strata_lot:
        return "Unit"
    return "House"


def nsw_psi_dat_records_from_text(text):
    records = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or not line.startswith("B;"):
            continue
        fields = line.split(";")
        percent_interest = fields[22].strip() if len(fields) > 22 else ""
        if percent_interest and clean_number(percent_interest) not in (None, 100):
            continue
        bucket = nsw_psi_bucket(fields)
        if not bucket:
            continue
        records.append({
            "Suburb": fields[9] if len(fields) > 9 else "",
            "Postcode": fields[10] if len(fields) > 10 else "",
            "Property Type": bucket,
            "Contract Date": fields[13] if len(fields) > 13 else "",
            "Purchase Price": fields[15] if len(fields) > 15 else "",
        })
    return records


def nsw_psi_dat_records(path):
    return nsw_psi_dat_records_from_text(Path(path).read_text(encoding="utf-8", errors="replace"))


def nsw_psi_zip_records(path):
    records = []
    with zipfile.ZipFile(path) as archive:
        for name in sorted(archive.namelist()):
            if name.startswith("__MACOSX/") or not name.lower().endswith(".dat"):
                continue
            text = archive.read(name).decode("utf-8", errors="replace")
            records.extend(nsw_psi_dat_records_from_text(text))
    return records


def table_records(path):
    suffix = Path(path).suffix.lower()
    if suffix == ".csv":
        return csv_records(path)
    if suffix == ".dat":
        return nsw_psi_dat_records(path)
    if suffix == ".zip":
        return nsw_psi_zip_records(path)
    if suffix in {".xlsx", ".xlsm", ".xltx", ".xltm"}:
        return xlsx_records(path)
    raise ValueError(f"Unsupported NSW property sales export file type: {suffix or Path(path).name}")


def parse_nsw_property_sales_rows(path, period=""):
    records = table_records(path)
    return property_sales_rows_from_records(records, period=period, fallback_period=Path(path).stem)


def property_sales_rows_from_records(records, period="", fallback_period=""):
    sale_rows = []
    quarter_labels = {}
    for record in records:
        suburb = clean_suburb(find_header_value(record, ("suburb", "locality", "property suburb", "address suburb", "town")))
        bucket = property_sale_bucket(find_header_value(record, (
            "property type", "dwelling type", "property type desc", "property description",
            "building type", "property class", "premises type",
        )))
        price = clean_number(find_header_value(record, (
            "sale price", "purchase price", "price", "transfer price", "consideration", "contract price",
        )))
        sale_date = parse_sale_date(find_header_value(record, (
            "contract date", "sale date", "settlement date", "transfer date", "dealing date", "purchase date",
        )))
        if not suburb or not bucket or price is None:
            continue
        quarter_key = None
        if sale_date:
            quarter_key, quarter_label = sale_quarter_key_and_label(sale_date)
            quarter_labels[quarter_key] = quarter_label
        sale_rows.append({
            "suburb": suburb,
            "property_type": bucket,
            "price": price,
            "quarter_key": quarter_key,
        })
    if not sale_rows:
        return []
    target_quarter = None
    resolved_period = period
    if not resolved_period:
        dated_quarters = [row["quarter_key"] for row in sale_rows if row["quarter_key"]]
        if dated_quarters:
            target_quarter = sorted(dated_quarters)[-1]
            resolved_period = quarter_labels[target_quarter]
        else:
            resolved_period = fallback_period
    grouped = {}
    for row in sale_rows:
        if target_quarter and row["quarter_key"] != target_quarter:
            continue
        grouped.setdefault((row["suburb"], row["property_type"]), []).append(row["price"])
    parsed = []
    for (suburb, bucket), values in sorted(grouped.items()):
        median = median_value(values)
        if median is not None:
            parsed.append({
                "suburb": suburb,
                "property_type": bucket,
                "median_sale_price": median,
                "period": resolved_period,
                "sales_count": len(values),
            })
    return parsed


def parse_official_property_sales_rows(path, period=""):
    records = table_records(path)
    return property_sales_rows_from_records(records, period=period, fallback_period=Path(path).stem)


def official_property_sales_source_name(state):
    state = str(state or "").strip().upper()
    if state == "NSW":
        return NSW_PROPERTY_SALES_SOURCE_NAME
    return f"{state} official property sales export - CRM-calculated suburb medians"


def parse_qld_rental_rows(path):
    workbook = load_workbook(path, data_only=True, read_only=True)
    sheet = workbook["4 sub-rents"] if "4 sub-rents" in workbook.sheetnames else next((workbook[name] for name in workbook.sheetnames if "sub-rents" in name), None)
    if sheet is None:
        return []
    rows = list(sheet.iter_rows(values_only=True))
    month_row = rows[5] if len(rows) > 5 else ()
    year_row = rows[6] if len(rows) > 6 else ()
    period_columns = [
        (idx, clean_int(year_row[idx]) if idx < len(year_row) else None, month_row[idx] if idx < len(month_row) else None)
        for idx in range(4, max(len(month_row), len(year_row)))
        if idx < len(year_row) and clean_int(year_row[idx]) is not None and idx < len(month_row) and month_row[idx]
    ]
    if not period_columns:
        return []
    median_index, year, month_name = sorted(period_columns, key=lambda item: (item[1], {"Mar": 1, "Jun": 2, "Sep": 3, "Dec": 4}.get(str(item[2])[:3], 0)))[-1]
    quarter = quarter_label(year, month_name)
    parsed = []
    for row in rows[8:]:
        suburb = clean_suburb(row[2] if len(row) > 2 else "")
        dwelling = row[3] if len(row) > 3 else ""
        rent = clean_number(row[median_index] if len(row) > median_index else None)
        bucket = rental_bucket_from_dwelling(dwelling)
        if not suburb or not bucket or rent is None:
            continue
        parsed.append({
            "suburb": suburb,
            "property_type": bucket,
            "bedrooms": bedrooms_from_dwelling(dwelling),
            "median_weekly_rent": int(rent) if float(rent).is_integer() else rent,
            "quarter": quarter,
        })
    return parsed


def qld_rental_period_columns(rows):
    month_row = rows[5] if len(rows) > 5 else ()
    year_row = rows[6] if len(rows) > 6 else ()
    return [
        (idx, clean_int(year_row[idx]) if idx < len(year_row) else None, month_row[idx] if idx < len(month_row) else None)
        for idx in range(4, max(len(month_row), len(year_row)))
        if idx < len(year_row) and clean_int(year_row[idx]) is not None and idx < len(month_row) and month_row[idx]
    ]


def latest_qld_rental_period_column(rows):
    period_columns = qld_rental_period_columns(rows)
    if not period_columns:
        return None
    return sorted(period_columns, key=lambda item: (item[1], {"Mar": 1, "Jun": 2, "Sep": 3, "Dec": 4}.get(str(item[2])[:3], 0)))[-1]


def parse_qld_rental_detail_sheet(workbook, sheet_name, area_level, area_column):
    sheet = workbook[sheet_name] if sheet_name in workbook.sheetnames else None
    if sheet is None:
        return []
    rows = list(sheet.iter_rows(values_only=True))
    latest = latest_qld_rental_period_column(rows)
    if not latest:
        return []
    median_index, year, month_name = latest
    quarter = quarter_label(year, month_name)
    parsed = []
    for row in rows[8:]:
        raw_area = row[2] if len(row) > 2 else ""
        dwelling = row[3] if len(row) > 3 else ""
        rent = clean_number(row[median_index] if len(row) > median_index else None)
        bucket = rental_bucket_from_dwelling(dwelling)
        if raw_area in {None, ""} or not bucket or rent is None:
            continue
        area_text = clean_suburb(raw_area) if area_level != "postcode" else re.sub(r"[^0-9]", "", str(raw_area))
        if not area_text:
            continue
        row_payload = {
            "state": "QLD",
            "area_level": area_level,
            "area_name": area_text,
            "suburb": area_text if area_level == "suburb" else "",
            "postcode": area_text if area_level == "postcode" else "",
            "lga_name": area_text if area_level == "lga" else "",
            "property_type": bucket,
            "bedrooms": bedrooms_from_dwelling(dwelling),
            "median_weekly_rent": int(rent) if float(rent).is_integer() else rent,
            "quarter": quarter,
            "year": year,
            "source": QLD_RENTAL_DETAIL_SOURCE_NAME,
            "source_url": OFFICIAL_SOURCES["QLD"]["rental_market"],
            "raw_json": json.dumps({
                "workbook_sheet": sheet_name,
                "source_area_column": area_column,
                "source_area_value": str(raw_area),
                "source_dwelling_value": str(dwelling),
                "source_month": str(month_name),
                "source_year": year,
            }, ensure_ascii=False),
        }
        parsed.append(row_payload)
    return parsed


def parse_qld_rental_detail_rows(path):
    workbook = load_workbook(path, data_only=True, read_only=True)
    sheet_specs = [
        ("1 pc-rents", "postcode", "Postcode"),
        ("4 sub-rents", "suburb", "Suburb"),
        (next((name for name in workbook.sheetnames if name.strip().lower() == "7 lga-rents"), "7 lga-rents "), "lga", "Local Government Area"),
        ("10 qld-rents", "state", "State"),
    ]
    parsed = []
    for sheet_name, area_level, area_column in sheet_specs:
        parsed.extend(parse_qld_rental_detail_sheet(workbook, sheet_name, area_level, area_column))
    return parsed


def ensure_official_rental_market_details_table(conn):
    conn.execute(
        """CREATE TABLE IF NOT EXISTS official_rental_market_details (
           id INTEGER PRIMARY KEY,
           state TEXT NOT NULL,
           area_level TEXT NOT NULL,
           area_name TEXT NOT NULL,
           suburb TEXT,
           postcode TEXT,
           lga_name TEXT,
           property_type TEXT NOT NULL,
           bedrooms TEXT,
           median_weekly_rent REAL,
           quarter TEXT NOT NULL,
           year INTEGER,
           source TEXT NOT NULL,
           source_url TEXT,
           last_updated TEXT NOT NULL,
           raw_json TEXT,
           imported_at TEXT NOT NULL,
           UNIQUE(state,area_level,area_name,property_type,bedrooms,quarter,source)
        )"""
    )
    conn.execute(
        """CREATE INDEX IF NOT EXISTS idx_official_rental_market_details_lookup
           ON official_rental_market_details(state, area_level, area_name, property_type, quarter)"""
    )


def import_qld_rental_details(conn, path, apply=False):
    rows = parse_qld_rental_detail_rows(path)
    now = stamp()
    if apply:
        ensure_official_rental_market_details_table(conn)
    if apply and rows:
        quarters = sorted({row["quarter"] for row in rows if row.get("quarter")})
        for quarter in quarters:
            conn.execute(
                "DELETE FROM official_rental_market_details WHERE state='QLD' AND source=? AND quarter=?",
                (QLD_RENTAL_DETAIL_SOURCE_NAME, quarter),
            )
        for row in rows:
            conn.execute(
                """INSERT INTO official_rental_market_details(
                     state,area_level,area_name,suburb,postcode,lga_name,property_type,bedrooms,
                     median_weekly_rent,quarter,year,source,source_url,last_updated,raw_json,imported_at
                   ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    row["state"], row["area_level"], row["area_name"], row["suburb"], row["postcode"],
                    row["lga_name"], row["property_type"], row["bedrooms"], row["median_weekly_rent"],
                    row["quarter"], row["year"], row["source"], row["source_url"], now, row["raw_json"], now,
                ),
            )
    levels = {}
    for row in rows:
        levels[row["area_level"]] = levels.get(row["area_level"], 0) + 1
    return {
        "source": QLD_RENTAL_DETAIL_SOURCE_NAME,
        "state": "QLD",
        "total": len(rows),
        "changed": len(rows) if apply else 0,
        "area_levels": levels,
    }


def parse_tas_rental_rows(path, quarter=""):
    workbook = load_workbook(path, data_only=True, read_only=True)
    sheet = workbook["Active Bonds"] if "Active Bonds" in workbook.sheetnames else workbook.worksheets[0]
    quarter = quarter or period_from_filename(path)
    rows_by_key = {}
    headers = [str(cell or "").strip().lower() for cell in next(sheet.iter_rows(min_row=2, max_row=2, values_only=True), ())]
    index = {name: headers.index(name) for name in headers}
    required = ("suburb", "postcode", "weekly rent", "number of bedrooms", "dwelling / premises type", "housing type")
    if not all(name in index for name in required):
        return []
    for row in sheet.iter_rows(min_row=3, values_only=True):
        housing_type = str(row[index["housing type"]] if len(row) > index["housing type"] else "").strip().lower()
        if housing_type and housing_type != "private housing":
            continue
        suburb = clean_suburb(row[index["suburb"]] if len(row) > index["suburb"] else "")
        postcode = re.sub(r"[^0-9]", "", str(row[index["postcode"]] if len(row) > index["postcode"] else ""))
        bucket = rental_bucket_from_dwelling(row[index["dwelling / premises type"]] if len(row) > index["dwelling / premises type"] else "")
        bedrooms = str(row[index["number of bedrooms"]] if len(row) > index["number of bedrooms"] else "").strip()
        rent = clean_number(row[index["weekly rent"]] if len(row) > index["weekly rent"] else None)
        if not suburb or not bucket or rent is None or rent < 50:
            continue
        rows_by_key.setdefault((suburb, postcode, bucket, bedrooms), []).append(rent)
    parsed = []
    for (suburb, postcode, bucket, bedrooms), values in sorted(rows_by_key.items()):
        median = median_value(values)
        if median is not None:
            parsed.append({
                "suburb": suburb,
                "postcode": postcode,
                "property_type": bucket,
                "bedrooms": bedrooms,
                "median_weekly_rent": median,
                "quarter": quarter,
            })
    return parsed


def import_sa_rental_market(conn, path, apply=False, quarter=""):
    rows = parse_sa_rental_rows(path, quarter=quarter)
    now = stamp()
    if apply and rows:
        for row_quarter in sorted({row["quarter"] for row in rows}):
            conn.execute(
                "DELETE FROM rental_market WHERE state='SA' AND source=? AND quarter=?",
                (SA_RENTAL_SOURCE_NAME, row_quarter),
            )
        for row in rows:
            conn.execute(
                """INSERT INTO rental_market(state,suburb,postcode,property_type,bedrooms,median_weekly_rent,quarter,source,last_updated)
                   VALUES('SA',?,?,?,?,?,?,?,?)""",
                (
                    row["suburb"], "", row["property_type"], row["bedrooms"],
                    row["median_weekly_rent"], row["quarter"], SA_RENTAL_SOURCE_NAME, now,
                ),
            )
    return {"source": SA_RENTAL_SOURCE_NAME, "state": "SA", "total": len(rows), "changed": len(rows) if apply else 0}


def import_sa_property_house_market(conn, path, apply=False, period=""):
    rows = parse_sa_property_house_rows(path, period=period)
    now = stamp()
    if apply and rows:
        conn.execute(
            "DELETE FROM property_market WHERE state='SA' AND source=? AND property_type='House'",
            (SA_PROPERTY_SOURCE_NAME,),
        )
        for row in rows:
            conn.execute(
                """INSERT INTO property_market(state,suburb,postcode,property_type,median_sale_price,period,sales_count,source,last_updated)
                   VALUES('SA',?,?,?,?,?,?,?,?)""",
                (
                    row["suburb"], "", "House", row["median_sale_price"],
                    row["period"], row["sales_count"], SA_PROPERTY_SOURCE_NAME, now,
                ),
            )
    return {"source": SA_PROPERTY_SOURCE_NAME, "state": "SA", "total": len(rows), "changed": len(rows) if apply else 0}


def import_qld_rental_market(conn, path, apply=False):
    rows = parse_qld_rental_rows(path)
    now = stamp()
    if apply and rows:
        conn.execute("DELETE FROM rental_market WHERE state='QLD' AND source=?", (QLD_RENTAL_SOURCE_NAME,))
        for row in rows:
            conn.execute(
                """INSERT INTO rental_market(state,suburb,postcode,property_type,bedrooms,median_weekly_rent,quarter,source,last_updated)
                   VALUES('QLD',?,?,?,?,?,?,?,?)""",
                (row["suburb"], "", row["property_type"], row["bedrooms"], row["median_weekly_rent"], row["quarter"], QLD_RENTAL_SOURCE_NAME, now),
            )
    return {"source": QLD_RENTAL_SOURCE_NAME, "state": "QLD", "total": len(rows), "changed": len(rows) if apply else 0}


def import_tas_rental_market(conn, path, apply=False, quarter=""):
    rows = parse_tas_rental_rows(path, quarter=quarter)
    now = stamp()
    if apply and rows:
        conn.execute("DELETE FROM rental_market WHERE state='TAS' AND source=?", (TAS_RENTAL_SOURCE_NAME,))
        for row in rows:
            conn.execute(
                """INSERT INTO rental_market(state,suburb,postcode,property_type,bedrooms,median_weekly_rent,quarter,source,last_updated)
                   VALUES('TAS',?,?,?,?,?,?,?,?)""",
                (row["suburb"], row["postcode"], row["property_type"], row["bedrooms"], row["median_weekly_rent"], row["quarter"], TAS_RENTAL_SOURCE_NAME, now),
            )
    return {"source": TAS_RENTAL_SOURCE_NAME, "state": "TAS", "total": len(rows), "changed": len(rows) if apply else 0}


def import_nsw_rental_market(conn, path, apply=False, quarter=""):
    rows = parse_nsw_rental_rows(path, quarter=quarter)
    area_rows = conn.execute(
        """SELECT DISTINCT suburb, postcode FROM geo_areas
           WHERE state='NSW' AND COALESCE(suburb,'')<>'' AND COALESCE(postcode,'')<>''"""
    ).fetchall()
    suburbs_by_postcode = {}
    for row in area_rows:
        suburbs_by_postcode.setdefault(str(row["postcode"]), set()).add(str(row["suburb"]).strip())
    prepared = []
    for row in rows:
        for suburb in sorted(suburbs_by_postcode.get(row["postcode"], [])):
            prepared.append((suburb, row["postcode"], row["property_type"], row["bedrooms"], row["median_weekly_rent"], row["quarter"]))
    now = stamp()
    if apply and prepared:
        conn.execute("DELETE FROM rental_market WHERE state='NSW' AND source=?", (NSW_RENTAL_SOURCE_NAME,))
        for values in prepared:
            conn.execute(
                """INSERT INTO rental_market(state,suburb,postcode,property_type,bedrooms,median_weekly_rent,quarter,source,last_updated)
                   VALUES('NSW',?,?,?,?,?,?,?,?)""",
                (*values, NSW_RENTAL_SOURCE_NAME, now),
            )
    return {"source": NSW_RENTAL_SOURCE_NAME, "state": "NSW", "total": len(prepared), "raw_postcode_rows": len(rows), "changed": len(prepared) if apply else 0}


def import_nsw_property_sales_market(conn, path, apply=False, period=""):
    rows = parse_nsw_property_sales_rows(path, period=period)
    now = stamp()
    if apply and rows:
        conn.execute("DELETE FROM property_market WHERE state='NSW' AND source=?", (NSW_PROPERTY_SALES_SOURCE_NAME,))
        for row in rows:
            conn.execute(
                """INSERT INTO property_market(state,suburb,postcode,property_type,median_sale_price,period,sales_count,source,last_updated)
                   VALUES('NSW',?,?,?,?,?,?,?,?)""",
                (
                    row["suburb"], "", row["property_type"], row["median_sale_price"],
                    row["period"], row["sales_count"], NSW_PROPERTY_SALES_SOURCE_NAME, now,
                ),
            )
    periods = sorted({row["period"] for row in rows if row.get("period")})
    return {
        "source": NSW_PROPERTY_SALES_SOURCE_NAME,
        "state": "NSW",
        "total": len(rows),
        "period": periods[-1] if periods else "",
        "changed": len(rows) if apply else 0,
    }


def import_official_property_sales_market(conn, state, path, apply=False, period=""):
    state = str(state or "").strip().upper()
    if state not in EXISTING_CRM_STATES:
        raise ValueError(f"Unsupported state for official property sales import: {state or '(blank)'}")
    rows = parse_official_property_sales_rows(path, period=period)
    source_name = official_property_sales_source_name(state)
    now = stamp()
    if apply and rows:
        conn.execute("DELETE FROM property_market WHERE state=? AND source=?", (state, source_name))
        for row in rows:
            conn.execute(
                """INSERT INTO property_market(state,suburb,postcode,property_type,median_sale_price,period,sales_count,source,last_updated)
                   VALUES(?,?,?,?,?,?,?,?,?)""",
                (
                    state, row["suburb"], "", row["property_type"], row["median_sale_price"],
                    row["period"], row["sales_count"], source_name, now,
                ),
            )
    periods = sorted({row["period"] for row in rows if row.get("period")})
    return {
        "source": source_name,
        "state": state,
        "total": len(rows),
        "period": periods[-1] if periods else "",
        "changed": len(rows) if apply else 0,
    }


def existing_states(conn):
    rows = conn.execute(
        "SELECT DISTINCT UPPER(COALESCE(state,'')) state FROM schools WHERE COALESCE(state,'')<>'' ORDER BY state"
    ).fetchall()
    return [row["state"] for row in rows]


def seed_geo_areas_from_schools(conn, states, apply=False):
    states = [state.upper() for state in states]
    if not states:
        return {"source": SCHOOL_COORDINATE_GEO_SOURCE, "total": 0, "changed": 0}
    placeholders = ",".join("?" for _ in states)
    rows = conn.execute(
        f"""SELECT UPPER(state) state, suburb, postcode, AVG(latitude) latitude, AVG(longitude) longitude, COUNT(*) schools
            FROM schools
            WHERE UPPER(state) IN ({placeholders})
              AND COALESCE(suburb,'')<>''
              AND latitude IS NOT NULL AND longitude IS NOT NULL
            GROUP BY UPPER(state), suburb, postcode
            ORDER BY UPPER(state), suburb, postcode""",
        states,
    ).fetchall()
    changed = 0
    if apply:
        for row in rows:
            area_name = str(row["suburb"] or "").strip()
            conn.execute(
                """INSERT INTO geo_areas(state,area_type,area_name,suburb,postcode,latitude,longitude,geometry)
                   VALUES(?,'suburb',?,?,?,?,?,?)
                   ON CONFLICT(state,area_type,area_name,postcode) DO UPDATE SET
                     suburb=excluded.suburb,
                     latitude=excluded.latitude,
                     longitude=excluded.longitude,
                     geometry=COALESCE(geo_areas.geometry, excluded.geometry)""",
                (
                    row["state"], area_name, area_name, row["postcode"] or "",
                    row["latitude"], row["longitude"], SCHOOL_COORDINATE_GEO_SOURCE,
                ),
            )
            changed += 1
    return {"source": SCHOOL_COORDINATE_GEO_SOURCE, "total": len(rows), "changed": changed}


def refresh_summaries(conn, states, apply=False, limit=0):
    states = [state.upper() for state in states]
    if not states:
        return {"source": "school_radius_market_summary", "total": 0, "changed": 0}
    placeholders = ",".join("?" for _ in states)
    rows = conn.execute(
        f"""SELECT id FROM schools
            WHERE UPPER(COALESCE(state,'')) IN ({placeholders})
              AND latitude IS NOT NULL AND longitude IS NOT NULL
            ORDER BY UPPER(state), id""",
        states,
    ).fetchall()
    if limit:
        rows = rows[:limit]
    if not apply:
        return {"source": "school_radius_market_summary", "total": len(rows), "changed": 0}
    adapter = DbAdapter(conn)
    for row in rows:
        refresh_school_living_cost_summary(adapter, row["id"])
    return {"source": "school_radius_market_summary", "total": len(rows), "changed": len(rows)}


def resolve_default_sa_file(folder, pattern):
    matches = sorted(path for path in folder.glob(pattern) if not path.name.startswith("._"))
    return matches[-1] if matches else None


def optional_path(value):
    return Path(value) if value else None


def main():
    parser = argparse.ArgumentParser(description="Import official cross-state school surrounding living cost data.")
    parser.add_argument("--db", default=str(DB_PATH))
    parser.add_argument("--states", nargs="*", default=list(EXISTING_CRM_STATES), help="States to refresh. Defaults to ACT NSW QLD SA TAS WA.")
    parser.add_argument("--download-sa", action="store_true", help="Download latest SA official rent and house-sales files.")
    parser.add_argument("--download-rentals", action="store_true", help="Download latest official NSW/QLD/TAS rental resources.")
    parser.add_argument("--sa-rental-file")
    parser.add_argument("--sa-property-house-file")
    parser.add_argument("--nsw-rental-file")
    parser.add_argument("--nsw-property-sales-file")
    parser.add_argument("--qld-rental-file")
    parser.add_argument("--qld-property-sales-file")
    parser.add_argument("--tas-rental-file")
    parser.add_argument("--tas-property-sales-file")
    parser.add_argument("--wa-property-sales-file")
    parser.add_argument("--act-property-sales-file")
    parser.add_argument("--official-property-sales-file", help="User-supplied official CSV/XLSX property sales export.")
    parser.add_argument("--official-property-state", help="State for --official-property-sales-file, for example WA.")
    parser.add_argument("--quarter", default="")
    parser.add_argument("--period", default="")
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--apply", action="store_true", help="Write imported data and summaries. Without this flag the script is dry_run.")
    args = parser.parse_args()

    conn = sqlite3.connect(args.db)
    conn.row_factory = sqlite3.Row
    downloaded = {}
    download_errors = {}
    try:
        if args.download_sa:
            try:
                downloaded = maybe_download_sa_files()
            except Exception as exc:
                download_errors["SA"] = str(exc)
        if args.download_rentals:
            for key, callback in (
                ("nsw_rental_file", maybe_download_nsw_rental_file),
                ("qld_rental_file", maybe_download_qld_rental_file),
                ("tas_rental_file", maybe_download_tas_rental_file),
            ):
                try:
                    downloaded[key] = callback()
                except Exception as exc:
                    download_errors[key] = str(exc)

        states = [state.upper() for state in (args.states or existing_states(conn))]
        rental_file = optional_path(args.sa_rental_file or (downloaded.get("sa_rental_file") or {}).get("path"))
        house_file = optional_path(args.sa_property_house_file or (downloaded.get("sa_property_house_file") or {}).get("path"))
        nsw_rental_file = optional_path(args.nsw_rental_file or (downloaded.get("nsw_rental_file") or {}).get("path"))
        nsw_property_sales_file = optional_path(args.nsw_property_sales_file)
        qld_rental_file = optional_path(args.qld_rental_file or (downloaded.get("qld_rental_file") or {}).get("path"))
        qld_property_sales_file = optional_path(args.qld_property_sales_file)
        tas_rental_file = optional_path(args.tas_rental_file or (downloaded.get("tas_rental_file") or {}).get("path"))
        tas_property_sales_file = optional_path(args.tas_property_sales_file)
        wa_property_sales_file = optional_path(args.wa_property_sales_file)
        act_property_sales_file = optional_path(args.act_property_sales_file)
        official_property_sales_file = optional_path(args.official_property_sales_file)
        if not rental_file:
            rental_file = resolve_default_sa_file(SA_SOURCE_DIR, "private-rental-report-*.xlsx")
        if not house_file:
            house_file = resolve_default_sa_file(SA_SOURCE_DIR, "lsg_stats_*.xlsx")

        results = {
            "mode": dry_run_label(args.apply),
            "states": states,
            "official_sources": OFFICIAL_SOURCES,
            "downloaded": downloaded,
            "download_errors": download_errors,
            "import_errors": {},
            "steps": [],
        }
        if "SA" in states and rental_file and rental_file.exists():
            try:
                results["steps"].append(import_sa_rental_market(conn, rental_file, apply=args.apply, quarter=args.quarter))
            except Exception as exc:
                results["import_errors"]["sa_rental_market"] = str(exc)
        if "SA" in states and house_file and house_file.exists():
            try:
                results["steps"].append(import_sa_property_house_market(conn, house_file, apply=args.apply, period=args.period))
            except Exception as exc:
                results["import_errors"]["sa_property_market_house"] = str(exc)

        results["steps"].append(seed_geo_areas_from_schools(conn, states, apply=args.apply))
        if "NSW" in states and nsw_rental_file and nsw_rental_file.exists():
            try:
                results["steps"].append(import_nsw_rental_market(conn, nsw_rental_file, apply=args.apply, quarter=args.quarter))
            except Exception as exc:
                results["import_errors"]["nsw_rental_market"] = str(exc)
        if "NSW" in states and nsw_property_sales_file and nsw_property_sales_file.exists():
            try:
                results["steps"].append(import_nsw_property_sales_market(conn, nsw_property_sales_file, apply=args.apply, period=args.period))
            except Exception as exc:
                results["import_errors"]["nsw_property_sales_market"] = str(exc)
        state_property_files = {
            "QLD": qld_property_sales_file,
            "TAS": tas_property_sales_file,
            "WA": wa_property_sales_file,
            "ACT": act_property_sales_file,
        }
        if official_property_sales_file:
            state = str(args.official_property_state or "").strip().upper()
            if state:
                state_property_files[state] = official_property_sales_file
            else:
                results["import_errors"]["official_property_sales_market"] = "--official-property-state is required with --official-property-sales-file"
        for state, property_file in state_property_files.items():
            if state in states and property_file and property_file.exists():
                try:
                    results["steps"].append(import_official_property_sales_market(conn, state, property_file, apply=args.apply, period=args.period))
                except Exception as exc:
                    results["import_errors"][f"{state.lower()}_property_sales_market"] = str(exc)
        if "QLD" in states and qld_rental_file and qld_rental_file.exists():
            try:
                results["steps"].append(import_qld_rental_details(conn, qld_rental_file, apply=args.apply))
                results["steps"].append(import_qld_rental_market(conn, qld_rental_file, apply=args.apply))
            except Exception as exc:
                results["import_errors"]["qld_rental_market"] = str(exc)
        if "TAS" in states and tas_rental_file and tas_rental_file.exists():
            try:
                results["steps"].append(import_tas_rental_market(conn, tas_rental_file, apply=args.apply, quarter=args.quarter))
            except Exception as exc:
                results["import_errors"]["tas_rental_market"] = str(exc)
        results["steps"].append(refresh_summaries(conn, states, apply=args.apply, limit=args.limit))

        if args.apply:
            conn.commit()
        else:
            conn.rollback()
        print(json.dumps(results, ensure_ascii=False, indent=2))
    finally:
        conn.close()


if __name__ == "__main__":
    main()
