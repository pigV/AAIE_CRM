import argparse
import json
import os
from pathlib import Path

from .models import SubtitleSegment
from .ocr.base import redact_error
from .ocr.iflytek import IFlytekOCRAdapter, IFlytekOCRConfig, IFlytekUniversalOCRClient


class SingleRequestCounter:
    def __init__(self, client):
        self.client = client
        self.request_count = 0

    def __call__(self, image_bytes, image_format):
        if self.request_count >= 1:
            raise RuntimeError("smoke test 禁止发送第二次科大讯飞 OCR 请求")
        self.request_count += 1
        return self.client(image_bytes, image_format)


def masked_identifier(value):
    value = str(value or "")
    if not value:
        return ""
    if len(value) <= 8:
        return value[:2] + "..." + value[-2:]
    return value[:8] + "..." + value[-4:]


def run_smoke(image_path, environ=None, client_factory=None):
    environ = os.environ if environ is None else environ
    config = IFlytekOCRConfig.from_environ(environ)
    if config is None:
        raise RuntimeError("科大讯飞 OCR 环境变量尚未配置")
    path = Path(image_path).expanduser().resolve()
    if not path.is_file():
        raise FileNotFoundError("smoke test 图片不存在")
    factory = client_factory or (lambda value: IFlytekUniversalOCRClient(value))
    counted_client = SingleRequestCounter(factory(config))
    adapter = IFlytekOCRAdapter(
        client=counted_client,
        sensitive_values=(config.app_id, config.api_key, config.api_secret),
        max_attempts=1,
    )
    segment = SubtitleSegment(
        "iflytek-smoke-frame", "iflytek-smoke-video", 0, 1000,
        str(path), "smoke-image", "manual-single-image-v1",
    )
    result = adapter.recognize(segment)
    raw_serialized = json.dumps(result.raw_response, ensure_ascii=False)
    decoded = result.raw_response.get("decoded_result", {})
    pages = decoded.get("pages", ()) if isinstance(decoded, dict) else ()
    header = result.raw_response.get("header", {})
    line_details = []
    page_details = []
    for page in pages:
        if not isinstance(page, dict):
            continue
        page_details.append({
            "width": page.get("width"),
            "height": page.get("height"),
            "angle": page.get("angle"),
        })
        for line in page.get("lines", ()):
            if not isinstance(line, dict):
                continue
            line_details.append({
                "text": "".join(
                    str(word.get("content") or "")
                    for word in line.get("words", ()) if isinstance(word, dict)
                ),
                "confidence": line.get("conf"),
                "coord": line.get("coord", ()),
            })
    line_count = sum(
        len(page.get("lines", ())) for page in pages if isinstance(page, dict)
    )
    return {
        "provider": result.provider,
        "status": result.status,
        "text": result.text,
        "confidence": result.confidence,
        "header_code": header.get("code") if isinstance(header, dict) else None,
        "request_id": masked_identifier(result.request_id),
        "bounding_box_count": len(result.bounding_boxes),
        "polygon_count": len(result.polygons),
        "request_count": counted_client.request_count,
        "line_count": line_count,
        "lines": line_details,
        "pages": page_details,
        "bounding_box": result.bounding_boxes,
        "polygon": result.polygons,
        "raw_response_retained": bool(result.raw_response),
        "raw_response_contains_credentials": any(
            value and value in raw_serialized
            for value in (config.app_id, config.api_key, config.api_secret)
        ),
        "error": result.error,
    }


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Run one authorized iFlytek Universal OCR image smoke test."
    )
    parser.add_argument("--image", required=True, help="One local subtitle screenshot")
    args = parser.parse_args(argv)
    try:
        output = run_smoke(args.image)
    except Exception as exc:
        output = {
            "provider": "iflytek",
            "status": "failed",
            "error": {
                "code": "SMOKE_SETUP_FAILED",
                "message": redact_error(exc, (
                    os.environ.get("IFLYTEK_OCR_APP_ID", ""),
                    os.environ.get("IFLYTEK_OCR_API_KEY", ""),
                    os.environ.get("IFLYTEK_OCR_API_SECRET", ""),
                )),
            },
        }
    print(json.dumps(output, ensure_ascii=False))
    return 0 if output.get("status") == "completed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
