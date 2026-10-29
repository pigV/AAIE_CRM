#!/usr/bin/env python3
"""Isolated HTTP acceptance for the Revenue permission authority split."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import urllib.error
import urllib.parse
import urllib.request


ROOT = Path(__file__).resolve().parents[1]
SENSITIVE_TERMS = ("revenue", "commission", "收入与佣金", "佣金", "master agent")
WORKSPACES = (
    "/revenue", "/revenue/cases", "/revenue/billing",
    "/revenue/direct-commission", "/revenue/master-agent",
    "/revenue/contracts", "/revenue/reconciliation",
    "/mobile/revenue", "/mobile/revenue/cases", "/api/revenue/summary",
)
IDENTITIES = {
    "Admin": ("revenue-e2e-admin", "csrf-900001", True),
    "TongYu": ("revenue-e2e-tong-yu", "csrf-tong-yu", True),
    "Manager": ("revenue-e2e-manager", "csrf-900002", True),
    "Staff": ("revenue-e2e-staff", "csrf-900003", True),
    "Consultant": ("revenue-e2e-consultant", "csrf-900004", False),
    "Finance": ("revenue-e2e-finance", "csrf-900005", False),
}


def request(base_url: str, path: str, token: str, *, method: str = "GET", csrf: str = "") -> dict:
    payload = urllib.parse.urlencode({"csrf": csrf}).encode() if method == "POST" else None
    request_object = urllib.request.Request(
        base_url.rstrip("/") + path,
        data=payload,
        method=method,
        headers={
            "Cookie": f"aaie_session={token}",
            "User-Agent": "AAIE-Revenue-Permission-Isolated-Acceptance/1.0",
            "Content-Type": "application/x-www-form-urlencoded",
        },
    )
    try:
        response = urllib.request.urlopen(request_object, timeout=10)
    except urllib.error.HTTPError as error:
        response = error
    with response:
        body = response.read().decode("utf-8", errors="replace")
        return {"status": int(response.status), "body": body}


def leakage(body: str) -> list[str]:
    lowered = body.lower()
    return [term for term in SENSITIVE_TERMS if term.lower() in lowered]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://127.0.0.1:8908")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    matrix = {}

    for name, (token, csrf, authorized) in IDENTITIES.items():
        for path in WORKSPACES:
            result = request(args.base_url, path, token)
            expected = 200 if authorized else 403
            if result["status"] != expected:
                raise AssertionError(f"{name} {path}: {result['status']} != {expected}")
            matches = [] if authorized else leakage(result["body"])
            if matches:
                raise AssertionError(f"{name} {path} leaked {matches}")
            if path == "/api/revenue/summary" and authorized:
                document = json.loads(result["body"])
                if document.get("ok") is not True or document.get("data", {}).get("schema_version") != "revenue.v1":
                    raise AssertionError(f"{name} API envelope is invalid")
            matrix[f"{name} GET {path}"] = {"status": result["status"], "leakage": matches}

        normal = request(
            args.base_url, "/revenue/billing/payment", token,
            method="POST", csrf=csrf,
        )
        expected_normal = 400 if authorized else 403
        if normal["status"] != expected_normal:
            raise AssertionError(
                f"{name} normal mutation gate: {normal['status']} != {expected_normal}"
            )
        matrix[f"{name} POST normal"] = {"status": normal["status"]}

        high_risk = request(
            args.base_url, "/revenue/contracts/activate", token,
            method="POST", csrf=csrf,
        )
        expected_high_risk = 400 if name == "Admin" else 403
        if high_risk["status"] != expected_high_risk:
            raise AssertionError(
                f"{name} high-risk mutation gate: {high_risk['status']} != {expected_high_risk}"
            )
        if not authorized and leakage(high_risk["body"]):
            raise AssertionError(f"{name} high-risk denial leaked Revenue terminology")
        matrix[f"{name} POST high-risk"] = {"status": high_risk["status"]}

    admin_initialization = request(
        args.base_url, "/revenue/initialization", IDENTITIES["Admin"][0]
    )
    if admin_initialization["status"] != 200:
        raise AssertionError("Admin initialization authority is unavailable")
    for name in ("TongYu", "Manager", "Staff"):
        token = IDENTITIES[name][0]
        denied = request(args.base_url, "/revenue/initialization", token)
        if denied["status"] != 403:
            raise AssertionError(f"{name} initialization authority was not safely denied")

    tong_context = request(
        args.base_url, "/students/20260139/tab/revenue", IDENTITIES["TongYu"][0]
    )
    if tong_context["status"] != 200 or "/revenue/cases/" not in tong_context["body"]:
        raise AssertionError("TONG YU Student/Application Cycle context is unavailable")

    report = {
        "status": "PASS",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "base_url": args.base_url,
        "matrix": matrix,
        "normal_operation_authority": "Admin OR explicit revenue_commission permission",
        "high_risk_authority": "exact Admin",
        "production_service_used": False,
        "production_database_used": False,
    }
    output = args.output.resolve()
    if not output.is_relative_to(ROOT / "SAFEBOX"):
        raise AssertionError("acceptance report must stay inside candidate SAFEBOX")
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({
        "status": report["status"],
        "checks": len(matrix),
        "normal_operation_authority": report["normal_operation_authority"],
        "high_risk_authority": report["high_risk_authority"],
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
