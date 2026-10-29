import argparse
import json
import os
from pathlib import Path

from .models import SubtitleSegment
from .ocr.tencent import (
    TencentOCRAdapter,
    TencentOCRConfig,
    TencentSDKGeneralAccurateClient,
)


class SingleRequestCounter:
    def __init__(self, client):
        self.client = client
        self.request_count = 0

    def __call__(self, image_bytes):
        if self.request_count >= 1:
            raise RuntimeError("smoke test 禁止发送第二次腾讯 OCR 请求")
        self.request_count += 1
        return self.client(image_bytes)


def run_smoke(image_path, environ=None, client_factory=None):
    environ = os.environ if environ is None else environ
    config = TencentOCRConfig.from_environ(environ)
    if config is None:
        raise RuntimeError("腾讯云 OCR 环境变量尚未配置")
    path = Path(image_path).expanduser().resolve()
    if not path.is_file():
        raise FileNotFoundError("smoke test 图片不存在")
    factory = client_factory or (lambda value: TencentSDKGeneralAccurateClient(value))
    counted_client = SingleRequestCounter(factory(config))
    adapter = TencentOCRAdapter(
        client=counted_client,
        sensitive_values=(config.secret_id, config.secret_key),
        max_attempts=1,
    )
    segment = SubtitleSegment(
        "tencent-smoke-frame", "tencent-smoke-video", 0, 1000,
        str(path), "smoke-image", "manual-single-image-v1",
    )
    result = adapter.recognize(segment)
    raw_serialized = json.dumps(result.raw_response, ensure_ascii=False)
    raw_payload = result.raw_response.get("Response", result.raw_response)
    detections = (
        raw_payload.get("TextDetections", ())
        if isinstance(raw_payload, dict) else ()
    )
    return {
        "provider": result.provider,
        "status": result.status,
        "text": result.text,
        "confidence": result.confidence,
        "request_id": result.request_id,
        "bounding_box_count": len(result.bounding_boxes),
        "polygon_count": len(result.polygons),
        "request_count": counted_client.request_count,
        "text_detections_count": len(detections),
        "item_polygon": result.bounding_boxes,
        "polygon": result.polygons,
        "raw_response_retained": bool(result.raw_response),
        "raw_response_contains_credentials": any(
            value and value in raw_serialized
            for value in (config.secret_id, config.secret_key)
        ),
        "error": result.error,
    }


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Run one authorized Tencent GeneralAccurateOCR image smoke test."
    )
    parser.add_argument("--image", required=True, help="One local subtitle screenshot")
    args = parser.parse_args(argv)
    try:
        output = run_smoke(args.image)
    except Exception as exc:
        output = {
            "provider": "tencent",
            "status": "failed",
            "error": {"code": "SMOKE_SETUP_FAILED", "message": str(exc)[:300]},
        }
    print(json.dumps(output, ensure_ascii=False))
    return 0 if output.get("status") == "completed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
