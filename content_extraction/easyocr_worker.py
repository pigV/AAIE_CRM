import argparse
import json
import socket
import sys
import time
from pathlib import Path


class OfflineNetworkGuard:
    def __init__(self):
        self.request_count = 0
        self._socket_connect = None
        self._create_connection = None

    def _blocked(self, *_args, **_kwargs):
        self.request_count += 1
        raise RuntimeError("EasyOCR offline runtime blocked a network request")

    def install(self):
        self._socket_connect = socket.socket.connect
        self._create_connection = socket.create_connection
        socket.socket.connect = self._blocked
        socket.create_connection = self._blocked


def emit(payload):
    print(json.dumps(payload, ensure_ascii=False, separators=(",", ":")), flush=True)


def json_safe(value):
    if isinstance(value, dict):
        return {str(key): json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [json_safe(item) for item in value]
    tolist = getattr(value, "tolist", None)
    if callable(tolist):
        return json_safe(tolist())
    item = getattr(value, "item", None)
    if callable(item):
        return json_safe(item())
    return value


def build_reader(model_dir, languages, download_enabled):
    import easyocr

    return easyocr.Reader(
        list(languages),
        gpu=False,
        model_storage_directory=str(model_dir),
        user_network_directory=str(model_dir),
        download_enabled=bool(download_enabled),
        verbose=False,
    )


def run_setup(model_dir, languages):
    started = time.perf_counter()
    reader = build_reader(model_dir, languages, download_enabled=True)
    emit({
        "event": "setup_complete",
        "languages": list(languages),
        "model_dir": str(model_dir),
        "elapsed_ms": round((time.perf_counter() - started) * 1000, 3),
        "reader": type(reader).__name__,
    })


def run_worker(model_dir, languages, offline):
    guard = OfflineNetworkGuard()
    if offline:
        guard.install()
    started = time.perf_counter()
    try:
        reader = build_reader(model_dir, languages, download_enabled=False)
    except Exception as exc:
        emit({"event": "startup_error", "error": str(exc)[:500]})
        return 2
    init_ms = round((time.perf_counter() - started) * 1000, 3)
    reader_identity = f"reader-{id(reader)}"
    inference_count = 0
    emit({
        "event": "ready",
        "init_ms": init_ms,
        "reader_identity": reader_identity,
        "network_request_count": guard.request_count,
    })
    for line in sys.stdin:
        try:
            request = json.loads(line)
        except (TypeError, ValueError, json.JSONDecodeError):
            emit({"event": "error", "request_id": "", "error": "invalid request JSON"})
            continue
        if request.get("command") == "shutdown":
            emit({"event": "shutdown", "worker_exit": "clean"})
            return 0
        request_id = str(request.get("request_id") or "")
        if request.get("command") != "recognize":
            emit({"event": "error", "request_id": request_id, "error": "unknown command"})
            continue
        try:
            image_path = Path(str(request.get("image_path") or ""))
            if not image_path.is_file():
                raise FileNotFoundError("OCR image does not exist")
            from PIL import Image

            with Image.open(image_path) as image:
                image_size = {"width": image.width, "height": image.height}
            inference_started = time.perf_counter()
            raw = reader.readtext(str(image_path), detail=1, paragraph=False)
            inference_count += 1
            detections = []
            for item in raw or ():
                if not isinstance(item, (list, tuple)) or len(item) < 3:
                    continue
                polygon, text, confidence = item[:3]
                detections.append({
                    "polygon": json_safe(polygon),
                    "text": str(text or ""),
                    "confidence": float(confidence or 0),
                })
            emit({
                "event": "result",
                "request_id": request_id,
                "detections": detections,
                "image": image_size,
                "inference_ms": round((time.perf_counter() - inference_started) * 1000, 3),
                "worker_inference_count": inference_count,
                "reader_identity": reader_identity,
                "network_request_count": guard.request_count,
            })
        except Exception as exc:
            emit({"event": "error", "request_id": request_id, "error": str(exc)[:500]})
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description="Isolated EasyOCR JSONL worker")
    parser.add_argument("--model-dir", required=True)
    parser.add_argument("--languages", default="ch_sim,en")
    parser.add_argument("--setup", action="store_true")
    parser.add_argument("--offline", action="store_true")
    args = parser.parse_args(argv)
    model_dir = Path(args.model_dir).expanduser().resolve()
    model_dir.mkdir(parents=True, exist_ok=True)
    languages = tuple(item.strip() for item in args.languages.split(",") if item.strip())
    if args.setup:
        run_setup(model_dir, languages)
        return 0
    return run_worker(model_dir, languages, args.offline)


if __name__ == "__main__":
    raise SystemExit(main())
