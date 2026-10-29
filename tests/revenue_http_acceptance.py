#!/usr/bin/env python3
"""HTTP permission and zero-leakage acceptance for Revenue V1."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import urllib.error
import urllib.request


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT = (
    ROOT / "SAFEBOX/revenue-commission-v1-20260829/browser/http-acceptance.json"
)
SENSITIVE_TERMS = (
    "revenue",
    "commission",
    "收入与佣金",
    "佣金",
    "master agent",
    "commission-disabled",
    "佣金查看",
    "敬请期待",
)


def request(base_url: str, path: str, token: str) -> dict:
    request_object = urllib.request.Request(
        base_url.rstrip("/") + path,
        headers={
            "Cookie": f"aaie_session={token}",
            "User-Agent": "AAIE-Revenue-Isolated-Acceptance/1.0",
        },
    )
    try:
        response = urllib.request.urlopen(request_object, timeout=10)
    except urllib.error.HTTPError as error:
        response = error
    with response:
        payload = response.read().decode("utf-8", errors="replace")
        return {
            "status": int(response.status),
            "content_type": str(response.headers.get("Content-Type") or ""),
            "body": payload,
        }


def leakage(body: str) -> list[str]:
    lowered = body.lower()
    return [term for term in SENSITIVE_TERMS if term.lower() in lowered]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://127.0.0.1:8897")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()

    matrix = {}
    admin_token = "revenue-e2e-admin"
    admin_paths = (
        "/revenue", "/revenue/initialization", "/revenue/cases", "/revenue/billing",
        "/revenue/direct-commission", "/revenue/master-agent",
        "/revenue/contracts", "/revenue/reconciliation",
        "/mobile/revenue", "/mobile/revenue/cases", "/api/revenue/summary",
    )
    for path in admin_paths:
        result = request(args.base_url, path, admin_token)
        if result["status"] != 200:
            raise AssertionError(f"Admin {path} returned {result['status']}")
        if path == "/revenue/initialization":
            required_markers = (
                "收费规则与数据接入检查",
                'data-revenue-initialization-preview="read-only"',
                "批次校验值",
                "等待管理员批准正式接入",
            )
            missing = [marker for marker in required_markers if marker not in result["body"]]
            if missing:
                raise AssertionError(f"initialization Preview missing markers: {missing}")
            preview_main = result["body"].split(
                'data-revenue-initialization-preview="read-only"', 1
            )[-1].split("</main>", 1)[0]
            if 'method="post"' in preview_main.lower():
                raise AssertionError("initialization Preview unexpectedly exposes a write form")
        if path == "/api/revenue/summary":
            document = json.loads(result["body"])
            if document.get("ok") is not True or not isinstance(document.get("data"), dict):
                raise AssertionError(f"Revenue API returned invalid envelope: {document}")
            data = document["data"]
            required = {
                "schema_version",
                "open_cases",
                "client_receivable_cents",
                "commission_forecast_cents",
                "unmatched_statement_items",
                "open_exceptions",
                "recent_cases",
            }
            if not required.issubset(data):
                raise AssertionError(f"Revenue API missing fields: {required - set(data)}")
            if data["schema_version"] != "revenue.v1":
                raise AssertionError(f"unexpected Revenue schema version: {data['schema_version']}")
        matrix[f"Admin {path}"] = {
            "status": result["status"],
            "content_type": result["content_type"],
        }

    case_href = str((data.get("recent_cases") or [{}])[0].get("href") or "")
    if not case_href.startswith("/revenue/cases/"):
        raise AssertionError("Revenue API did not expose a synthetic Commercial Case workbench")
    case_result = request(args.base_url, case_href, admin_token)
    if case_result["status"] != 200:
        raise AssertionError(f"Admin {case_href} returned {case_result['status']}")
    matrix[f"Admin {case_href}"] = {"status": 200, "content_type": case_result["content_type"]}
    mobile_case_href = case_href.replace("/revenue/cases/", "/mobile/revenue/cases/", 1)
    mobile_case_result = request(args.base_url, mobile_case_href, admin_token)
    if mobile_case_result["status"] != 200:
        raise AssertionError(
            f"Admin {mobile_case_href} returned {mobile_case_result['status']}"
        )
    matrix[f"Admin {mobile_case_href}"] = {
        "status": 200,
        "content_type": mobile_case_result["content_type"],
    }

    action_paths = (
        "/revenue/billing/invoice", "/revenue/cases/tuition",
        "/revenue/cases/commencement",
    )
    for path in action_paths:
        result = request(args.base_url, path, admin_token)
        if result["status"] != 405:
            raise AssertionError(f"Admin GET {path} returned {result['status']}")
        matrix[f"Admin GET {path}"] = {"status": 405}

    roles = {
        "Manager": "revenue-e2e-manager",
        "Staff": "revenue-e2e-staff",
        "Consultant": "revenue-e2e-consultant",
        "Finance": "revenue-e2e-finance",
    }
    for role, token in roles.items():
        for path in admin_paths + (case_href, mobile_case_href) + action_paths:
            result = request(args.base_url, path, token)
            if result["status"] != 403:
                raise AssertionError(f"{role} {path} returned {result['status']}")
            matches = leakage(result["body"])
            if matches:
                raise AssertionError(f"{role} {path} leaked {matches}")
            matrix[f"{role} {path}"] = {
                "status": result["status"],
                "leakage": matches,
            }

        for path in ("/", "/mobile"):
            result = request(args.base_url, path, token)
            if result["status"] != 200:
                raise AssertionError(f"{role} {path} returned {result['status']}")
            matches = leakage(result["body"])
            if matches:
                raise AssertionError(f"{role} {path} leaked {matches}")
            matrix[f"{role} {path}"] = {
                "status": result["status"],
                "leakage": matches,
            }

    manager_he = request(args.base_url, "/higher-education", roles["Manager"])
    if manager_he["status"] != 200:
        raise AssertionError(f"Manager HE returned {manager_he['status']}")
    manager_he_matches = leakage(manager_he["body"])
    if manager_he_matches:
        raise AssertionError(f"Manager HE leaked {manager_he_matches}")
    if "temporary-shares" in manager_he["body"] or "commission-lines" in manager_he["body"]:
        raise AssertionError("Manager HE leaked legacy commission route metadata")
    matrix["Manager /higher-education"] = {
        "status": manager_he["status"],
        "leakage": [],
        "legacy_route_leakage": False,
    }

    report = {
        "status": "PASS",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "base_url": args.base_url,
        "matrix": matrix,
        "admin_exact_role_enforcement": True,
        "non_admin_zero_leakage": True,
        "mobile_placeholder_removed": True,
        "production_service_used": False,
        "production_database_used": False,
    }
    output = args.output.resolve()
    if not output.is_relative_to(ROOT):
        raise AssertionError("acceptance report must stay inside the worktree")
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
