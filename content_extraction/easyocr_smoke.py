import argparse
import json
import time
from pathlib import Path

from .models import SubtitleSegment
from .ocr.easyocr import EasyOCRAdapter, EasyOCRWorkerClient, EasyOCRWorkerConfig


def run_smoke(image_path, config, client_factory=EasyOCRWorkerClient):
    path = Path(image_path).expanduser().resolve()
    if not path.is_file():
        raise FileNotFoundError("EasyOCR smoke 图片不存在")
    client = client_factory(config)
    started = time.perf_counter()
    result = None
    try:
        adapter = EasyOCRAdapter(client=client)
        segment = SubtitleSegment(
            "easyocr-smoke-frame", "easyocr-smoke-video", 0, 1000,
            str(path), "easyocr-smoke-image", "manual-single-image-v1",
        )
        result = adapter.recognize(segment)
    finally:
        client.close()
    total_ms = round((time.perf_counter() - started) * 1000, 3)
    raw = result.raw_response if result is not None else {}
    return {
        "provider": result.provider,
        "status": result.status,
        "text": result.text,
        "confidence": result.confidence,
        "request_id": result.request_id,
        "request_id_source": result.request_id_source,
        "billing_type": result.billing_type,
        "cost": result.cost,
        "model_version": result.model_version,
        "polygon_count": len(result.polygons),
        "bounding_box_count": len(result.bounding_boxes),
        "polygon": result.polygons,
        "bounding_box": result.bounding_boxes,
        "image": raw.get("image") or {},
        "reader_init_ms": client.init_ms,
        "inference_ms": raw.get("inference_ms"),
        "total_ms": total_ms,
        "local_inference_count": client.inference_count,
        "worker_start_count": client.start_count,
        "worker_exit": client.worker_exit,
        "network_request_count": raw.get("network_request_count"),
        "raw_response_retained": bool(result.raw_response),
        "error": result.error,
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description="Run one offline EasyOCR image smoke test")
    parser.add_argument("--image", required=True)
    parser.add_argument("--python", required=True)
    parser.add_argument("--worker", required=True)
    parser.add_argument("--model-dir", required=True)
    parser.add_argument("--timeout", type=float, default=180)
    args = parser.parse_args(argv)
    config = EasyOCRWorkerConfig(
        python_path=args.python,
        worker_path=args.worker,
        model_dir=args.model_dir,
        timeout_seconds=args.timeout,
    )
    try:
        output = run_smoke(args.image, config)
    except Exception as exc:
        output = {
            "provider": "easyocr",
            "status": "failed",
            "error": {"code": "EASYOCR_SMOKE_FAILED", "message": str(exc)[:300]},
        }
    print(json.dumps(output, ensure_ascii=False))
    return 0 if output.get("status") == "completed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
