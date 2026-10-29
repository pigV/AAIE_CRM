#!/usr/bin/env python3
"""Exercise the real parent signing POST and final PDF endpoints over HTTP."""

from __future__ import annotations

import argparse
import base64
from http.cookiejar import CookieJar
import io
import json
from pathlib import Path
from urllib import parse, request

from PIL import Image, ImageDraw


def post(opener, url: str, fields: dict[str, str], user_agent: str):
    payload = parse.urlencode(fields).encode("utf-8")
    req = request.Request(url, data=payload, method="POST")
    req.add_header("Content-Type", "application/x-www-form-urlencoded")
    req.add_header("User-Agent", user_agent)
    return opener.open(req, timeout=120)


def get(opener, url: str, user_agent: str):
    req = request.Request(url, method="GET")
    req.add_header("User-Agent", user_agent)
    return opener.open(req, timeout=120)


def signature_data_uri() -> str:
    image = Image.new("RGBA", (420, 150), (255, 255, 255, 0))
    draw = ImageDraw.Draw(image)
    draw.line(
        (18, 102, 90, 38, 160, 112, 248, 44, 398, 92),
        fill=(14, 35, 65, 255),
        width=9,
    )
    output = io.BytesIO()
    image.save(output, format="PNG")
    return "data:image/png;base64," + base64.b64encode(output.getvalue()).decode("ascii")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--token", required=True)
    parser.add_argument("--openid", required=True)
    parser.add_argument("--nickname", required=True)
    parser.add_argument("--user-agent", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    base = args.base_url.rstrip("/")
    portal_url = f"{base}/contract-sign/{parse.quote(args.token)}"
    jar = CookieJar()
    opener = request.build_opener(request.HTTPCookieProcessor(jar))

    login = post(
        opener,
        portal_url + "/login",
        {"wechat_nickname": args.nickname, "wechat_openid": args.openid},
        args.user_agent,
    )
    login_html = login.read().decode("utf-8")
    if "在线签署确认" not in login_html:
        raise AssertionError("login did not reach the signing form")

    signed = post(
        opener,
        portal_url + "/submit",
        {
            "declaration": "yes",
            "parent_name": args.nickname,
            "signature_data": signature_data_uri(),
        },
        args.user_agent,
    )
    signed_status = signed.status
    signed_html = signed.read().decode("utf-8")
    for marker in (
        "合同已完成签署",
        "双方签名及 AAIE 公章已加盖完成",
        "/final/view",
        "/final/download",
    ):
        if marker not in signed_html:
            raise AssertionError(f"completed page is missing {marker!r}")
    if signed_status != 200:
        raise AssertionError(f"signing returned HTTP {signed_status}, expected 200")

    view = get(opener, portal_url + "/final/view", args.user_agent)
    view_bytes = view.read()
    download = get(opener, portal_url + "/final/download", args.user_agent)
    download_bytes = download.read()
    if not view_bytes.startswith(b"%PDF-") or view_bytes != download_bytes:
        raise AssertionError("view/download did not return the same final PDF artifact")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_bytes(download_bytes)

    reopened = get(opener, portal_url, args.user_agent)
    reopened_html = reopened.read().decode("utf-8")
    if "合同已完成签署" not in reopened_html or "确认并签署" in reopened_html:
        raise AssertionError("refresh/reopen did not preserve the completed state")

    print(json.dumps({
        "signing_status": signed_status,
        "view_status": view.status,
        "download_status": download.status,
        "reopen_status": reopened.status,
        "final_pdf_bytes": len(download_bytes),
        "final_pdf": str(args.output.resolve()),
        "completed_page": True,
        "refresh_reopen": True,
    }, ensure_ascii=False))


if __name__ == "__main__":
    main()
