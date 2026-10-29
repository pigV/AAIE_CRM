"""Master-data-first institution structure for active Final Acceptances."""

from __future__ import annotations


INSTITUTION_CATEGORIES = (
    "k12_public",
    "k12_private",
    "public_language_school",
    "private_language_school",
    "university",
    "other_education",
    "unmapped",
)
METROPOLITAN_AREAS = (
    "Melbourne", "Sydney", "Adelaide", "Brisbane", "Perth", "Other",
    "unmapped_city",
)


def _normalized(value: object) -> str:
    return " ".join(str(value or "").strip().casefold().split())


def _metro(value: object) -> str:
    city = str(value or "").strip()
    if not city:
        return "unmapped_city"
    for known in METROPOLITAN_AREAS:
        if known.casefold() == city.casefold() and known != "unmapped_city":
            return known
    return "Other"


def _school_category(sector_key: object, school_type: object) -> str:
    sector = str(sector_key or "").strip().casefold()
    if sector not in {"public", "private"}:
        return "unmapped"
    type_text = str(school_type or "").strip().casefold()
    is_language = any(
        marker in type_text
        for marker in ("language", "english", "elicos", "语言")
    )
    if is_language:
        return f"{sector}_language_school"
    return f"k12_{sector}"


def load_dashboard_institution_metrics(
    connection,
    *,
    fallback_mappings: dict[str, dict] | None = None,
) -> dict:
    """Recalculate structure from current master rows on every read.

    Reviewed JSON is consulted only when the active acceptance has no resolvable
    School or Higher Education provider master identity. Every fallback use is
    exposed in both per-event provenance and aggregate audit counts.
    """

    fallback = fallback_mappings or {}
    fallback_by_key = {_normalized(key): value for key, value in fallback.items()}
    rows = connection.execute(
        """SELECT acceptance.id,acceptance.application_cycle_id,
                  acceptance.student_id,acceptance.school_name_snapshot,
                  acceptance.selected_at,
                  COALESCE(acceptance.school_id,offer.school_id) AS school_id,
                  school.school_name AS master_school_name,
                  school.sector_key AS master_sector_key,
                  school.school_type AS master_school_type,
                  city.city_name AS master_school_city,
                  provider.id AS provider_id,
                  provider.provider_name,provider.provider_type,
                  provider.city AS provider_city,
                  student.passport_first_name,student.passport_last_name
             FROM cycle_final_acceptances AS acceptance
             JOIN offer_received_events AS offer
               ON offer.id=acceptance.offer_received_event_id
             LEFT JOIN schools AS school
               ON school.id=COALESCE(acceptance.school_id,offer.school_id)
             LEFT JOIN school_cities AS city ON city.id=school.city_id
             LEFT JOIN higher_education_offers AS he_offer
               ON he_offer.id=offer.higher_education_offer_id
             LEFT JOIN higher_education_applications AS he_application
               ON he_application.id=he_offer.application_id
             LEFT JOIN higher_education_providers AS provider
               ON provider.id=he_application.provider_id
             LEFT JOIN students AS student
               ON student.student_id=acceptance.student_id
            WHERE acceptance.acceptance_status='active'
            ORDER BY acceptance.id"""
    ).fetchall()

    category_counts = {key: 0 for key in INSTITUTION_CATEGORIES}
    metro_counts = {key: 0 for key in METROPOLITAN_AREAS}
    provenance_counts = {
        "canonical_master": 0,
        "reviewed_json_fallback": 0,
        "unresolved": 0,
    }
    events = []
    fallback_events = []
    unmapped_events = []
    student_ids = set()

    for row in rows:
        student_ids.add(str(row["student_id"]))
        snapshot_name = str(row["school_name_snapshot"] or "").strip()
        mapping_entry = None
        if row["provider_id"] is not None:
            school_name = str(row["provider_name"] or snapshot_name)
            category = (
                "university"
                if str(row["provider_type"] or "").strip() == "University"
                else "other_education"
            )
            metro = _metro(row["provider_city"])
            provenance = "canonical_master"
            match_method = "higher_education_provider_id"
        elif row["school_id"] is not None and row["master_school_name"] is not None:
            school_name = str(row["master_school_name"] or snapshot_name)
            category = _school_category(
                row["master_sector_key"], row["master_school_type"]
            )
            metro = _metro(row["master_school_city"])
            provenance = "canonical_master"
            match_method = "school_id"
        else:
            school_name = snapshot_name
            mapping_entry = fallback_by_key.get(_normalized(snapshot_name))
            if mapping_entry is not None:
                category = str(mapping_entry.get("institution_type") or "unmapped")
                metro = str(mapping_entry.get("metropolitan_area") or "unmapped_city")
                provenance = "reviewed_json_fallback"
                match_method = str(mapping_entry.get("match_method") or "reviewed_fallback")
            else:
                category = "unmapped"
                metro = "unmapped_city"
                provenance = "unresolved"
                match_method = "unmapped"

        if category not in category_counts:
            category = "unmapped"
        if metro not in metro_counts:
            metro = "unmapped_city"
        category_counts[category] += 1
        metro_counts[metro] += 1
        provenance_counts[provenance] += 1
        student_name = " ".join(
            part
            for part in (
                str(row["passport_first_name"] or "").strip(),
                str(row["passport_last_name"] or "").strip(),
            )
            if part
        )
        event = {
            "offer_id": int(row["id"]),
            "application_cycle_id": int(row["application_cycle_id"]),
            "student_id": str(row["student_id"]),
            "student_name": student_name or str(row["student_id"]),
            "school_name": school_name,
            "institution_type": category,
            "metropolitan_area": metro,
            "match_method": match_method,
            "provenance": provenance,
            "reviewed": bool(mapping_entry and mapping_entry.get("reviewed")),
            "system_level": bool(mapping_entry and mapping_entry.get("system_level")),
            "offer_date": "",
            "accepted_date": str(row["selected_at"] or ""),
            "original_filename": "",
        }
        events.append(event)
        if provenance == "reviewed_json_fallback":
            fallback_events.append(event)
        if category == "unmapped" or metro == "unmapped_city":
            unmapped_events.append(event)

    total_events = len(events)
    recognized_events = total_events - category_counts["unmapped"]
    city_known_events = total_events - metro_counts["unmapped_city"]
    return {
        "total_events": total_events,
        "involved_student_count": len(student_ids),
        "recognized_events": recognized_events,
        "city_known_events": city_known_events,
        "category_counts": category_counts,
        "metro_counts": metro_counts,
        "unmapped_events": unmapped_events,
        "events": events,
        "fallback_events": fallback_events,
        "fallback_usage_count": len(fallback_events),
        "provenance_counts": provenance_counts,
        "coverage": {
            "type_percent": round(recognized_events / total_events * 100, 2)
            if total_events else 0,
            "city_percent": round(city_known_events / total_events * 100, 2)
            if total_events else 0,
        },
        "empty": total_events == 0,
    }
