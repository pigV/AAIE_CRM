import os
from pathlib import Path

from .base import OCRAdapter, normalized_confidence


ALLOWED_IMAGE_FORMATS = {"jpg", "jpeg", "png", "bmp"}


def _plain(value):
    tolist = getattr(value, "tolist", None)
    return tolist() if callable(tolist) else value


def _polygon(value):
    value = _plain(value)
    if isinstance(value, (list, tuple)) and len(value) == 4 and all(
        isinstance(point, (list, tuple)) and len(point) >= 2 for point in value
    ):
        try:
            points = tuple({"x": int(point[0]), "y": int(point[1])} for point in value)
        except (TypeError, ValueError):
            return ()
    elif isinstance(value, (list, tuple)) and len(value) == 4:
        try:
            left, top, right, bottom = (int(item) for item in value)
        except (TypeError, ValueError):
            return ()
        points = (
            {"x": left, "y": top}, {"x": right, "y": top},
            {"x": right, "y": bottom}, {"x": left, "y": bottom},
        )
    else:
        return ()
    if len({(point["x"], point["y"]) for point in points}) < 3:
        return ()
    return points


def _bounding_box(polygon):
    xs = [point["x"] for point in polygon]
    ys = [point["y"] for point in polygon]
    return {
        "x": min(xs), "y": min(ys),
        "width": max(xs) - min(xs), "height": max(ys) - min(ys),
    }


class PaddleOCRV5Engine:
    detection_model_name = "PP-OCRv5_mobile_det"
    recognition_model_name = "PP-OCRv5_mobile_rec"

    def __init__(self, factory=None):
        options = {
            "text_detection_model_name": self.detection_model_name,
            "text_recognition_model_name": self.recognition_model_name,
            "use_doc_orientation_classify": False,
            "use_doc_unwarping": False,
            "use_textline_orientation": False,
        }
        if factory is None:
            cache_home = Path(str(os.environ.get("PADDLE_PDX_CACHE_HOME") or ""))
            model_root = cache_home / "official_models"
            detection_dir = model_root / self.detection_model_name
            recognition_dir = model_root / self.recognition_model_name
            required = (
                detection_dir / "inference.json",
                detection_dir / "inference.pdiparams",
                recognition_dir / "inference.json",
                recognition_dir / "inference.pdiparams",
            )
            if not cache_home.is_absolute() or not all(path.is_file() for path in required):
                raise RuntimeError("PaddleOCR local PP-OCRv5 models are unavailable")
            os.environ.setdefault("PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK", "True")
            from paddleocr import PaddleOCR

            factory = PaddleOCR
            options.update({
                "text_detection_model_dir": str(detection_dir),
                "text_recognition_model_dir": str(recognition_dir),
            })
        self.pipeline = factory(**options)

    def predict(self, path):
        return self.pipeline.predict(input=str(path))


class PaddleOCRAdapter(OCRAdapter):
    provider = "paddle"
    model_version = "PP-OCRv5"
    request_id_source = "local"
    billing_type = "local"
    cost = 0

    def __init__(self, engine=None, sensitive_values=()):
        super().__init__(sensitive_values)
        self.engine = engine

    @staticmethod
    def _result_payload(item):
        json_payload = getattr(item, "json", None)
        if isinstance(json_payload, dict):
            return json_payload
        return item if isinstance(item, dict) else {}

    @staticmethod
    def _detections(payload):
        result = payload.get("res") if isinstance(payload.get("res"), dict) else payload
        if isinstance(result.get("rec_texts"), (list, tuple)):
            texts = result.get("rec_texts") or ()
            scores = result.get("rec_scores")
            scores = () if scores is None else _plain(scores)
            boxes = result.get("rec_polys")
            if boxes is None or len(boxes) == 0:
                boxes = result.get("rec_boxes") or ()
            boxes = _plain(boxes)
            return tuple(
                (texts[index], scores[index] if index < len(scores) else 0,
                 boxes[index] if index < len(boxes) else ())
                for index in range(len(texts))
            )
        return ((result.get("text"), result.get("score", 0), result.get("box", ())),)

    def recognize(self, segment):
        if self.engine is None:
            return self.failed(
                segment, "dependency_unavailable", "PADDLEOCR_UNAVAILABLE",
                "PaddleOCR PP-OCRv5 本地运行环境尚未配置",
            )
        path = Path(segment.representative_path)
        if path.suffix.lower().lstrip(".") not in ALLOWED_IMAGE_FORMATS or not path.is_file():
            return self.failed(
                segment, "failed", "PADDLEOCR_INVALID_IMAGE", "不支持或不存在的 OCR 图片"
            )
        try:
            raw_results = self.engine.predict(path)
            raw_payloads = []
            detections = []
            for item in raw_results or ():
                payload = self._result_payload(item)
                if not payload:
                    continue
                raw_payloads.append(payload)
                for raw_text, raw_score, raw_box in self._detections(payload):
                    text = str(raw_text or "").strip()
                    polygon = _polygon(raw_box)
                    if not text or not polygon:
                        continue
                    detections.append({
                        "text": text,
                        "confidence": normalized_confidence(raw_score),
                        "polygon": polygon,
                        "bounding_box": _bounding_box(polygon),
                    })
            detections.sort(key=lambda item: (
                item["bounding_box"]["y"], item["bounding_box"]["x"]
            ))
            confidence = (
                sum(item["confidence"] for item in detections) / len(detections)
                if detections else 0.0
            )
            request_id = f"local:paddle:{segment.run_id}:{segment.id}:{segment.image_hash[:12]}"
            return self.completed(
                segment,
                "".join(item["text"] for item in detections),
                confidence,
                boxes=detections,
                request_id=request_id,
                raw_response={
                    "provider": "paddle",
                    "billing_type": "local",
                    "cost": 0,
                    "network_request_count": 0,
                    "results": raw_payloads,
                },
                bounding_boxes=[item["bounding_box"] for item in detections],
                polygons=[item["polygon"] for item in detections],
            )
        except Exception as exc:
            return self.failed(segment, "failed", "PADDLEOCR_FAILED", exc)
