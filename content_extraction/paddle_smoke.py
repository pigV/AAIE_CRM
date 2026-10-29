import argparse
import json
import os
import socket
import time
from pathlib import Path
from unittest.mock import patch

from PIL import Image

from .models import SubtitleSegment
from .ocr.paddle import PaddleOCRAdapter, PaddleOCRV5Engine


class SingleInferenceCounter:
    def __init__(self, engine):
        self.engine = engine
        self.inference_count = 0

    def predict(self, path):
        if self.inference_count >= 1:
            raise RuntimeError("smoke test 禁止执行第二次 PaddleOCR 推理")
        self.inference_count += 1
        return self.engine.predict(path)


def _blocked_network(*_args, **_kwargs):
    raise RuntimeError("PaddleOCR local smoke 禁止网络连接")


def run_smoke(image_path, engine_factory=None):
    path = Path(image_path).expanduser().resolve()
    if not path.is_file():
        raise FileNotFoundError("PaddleOCR smoke 图片不存在")
    with Image.open(path) as image:
        image_size = {"width": image.width, "height": image.height}
    started = time.perf_counter()
    with patch.object(socket.socket, "connect", _blocked_network), patch(
        "socket.create_connection", _blocked_network
    ):
        engine = (engine_factory or PaddleOCRV5Engine)()
        counted_engine = SingleInferenceCounter(engine)
        adapter = PaddleOCRAdapter(engine=counted_engine)
        segment = SubtitleSegment(
            "paddle-smoke-frame", "paddle-smoke-video", 0, 1000,
            str(path), "paddle-smoke-image", "manual-single-image-v1",
        )
        result = adapter.recognize(segment)
    elapsed_ms = round((time.perf_counter() - started) * 1000, 3)
    return {
        "provider": result.provider,
        "status": result.status,
        "text": result.text,
        "confidence": result.confidence,
        "request_id": result.request_id,
        "request_id_source": result.request_id_source,
        "billing_type": result.billing_type,
        "cost": result.cost,
        "polygon_count": len(result.polygons),
        "bounding_box_count": len(result.bounding_boxes),
        "polygon": result.polygons,
        "bounding_box": result.bounding_boxes,
        "image": image_size,
        "local_inference_count": counted_engine.inference_count,
        "network_request_count": 0,
        "elapsed_ms": elapsed_ms,
        "raw_response_retained": bool(result.raw_response),
        "error": result.error,
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description="Run one offline PaddleOCR image smoke test.")
    parser.add_argument("--image", required=True, help="One local subtitle screenshot")
    args = parser.parse_args(argv)
    try:
        output = run_smoke(args.image)
    except Exception as exc:
        output = {
            "provider": "paddle",
            "status": "failed",
            "error": {"code": "PADDLE_SMOKE_FAILED", "message": str(exc)[:300]},
        }
    print(json.dumps(output, ensure_ascii=False))
    return 0 if output.get("status") == "completed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
