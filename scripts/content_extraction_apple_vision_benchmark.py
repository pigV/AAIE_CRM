"""Isolated Apple Vision suitability benchmark; never runs existing providers."""

import argparse
import hashlib
import json
import pickle
import resource
import sys
import time
from collections import Counter
from pathlib import Path
from statistics import median

WORKSPACE = Path(__file__).resolve().parents[1]
if str(WORKSPACE) not in sys.path:
    sys.path.insert(0, str(WORKSPACE))

from content_extraction.models import SubtitleSegment
from content_extraction.ocr.apple_vision import (
    AppleVisionOCRAdapter,
    AppleVisionWorkerClient,
    AppleVisionWorkerConfig,
)
from content_extraction.ocr.local_first import critical_token_conflicts
from content_extraction.ocr.normalization import similarity_signals


def confidence_distribution(values):
    values = sorted(float(value) for value in values)
    if not values:
        return {"p25": None, "median": None, "p75": None}

    def nearest_rank(fraction):
        return values[round((len(values) - 1) * fraction)]

    return {"p25": nearest_rank(.25), "median": median(values), "p75": nearest_rank(.75)}


def compare_to_paddle(paddle, candidate, similar_threshold=.9):
    signals = similarity_signals(paddle.text, candidate.text)
    conflicts = critical_token_conflicts((paddle, candidate))
    category = "exact" if signals.exact else (
        "similar" if signals.ratio >= similar_threshold and not conflicts else "conflict"
    )
    conflict_types = {item.token_type for item in conflicts}
    combined = (paddle.text or "") + (candidate.text or "")
    return {
        "category": category,
        "similarity_ratio": signals.ratio,
        "english_acronym_disagreement": "ASCII_ACRONYM" in conflict_types,
        "numeric_disagreement": bool(conflict_types & {"NUMBER", "PERCENTAGE", "AMOUNT", "AGE", "DATE", "VISA_SUBCLASS"}),
        "chinese_disagreement": bool(not signals.exact and any("\u4e00" <= char <= "\u9fff" for char in combined)),
        "critical_conflict_types": sorted(conflict_types),
    }


def audit_easyocr_confidence(results, tolerance=1e-9):
    frame_values = []
    box_values = []
    mismatches = 0
    for result in results:
        retained = [float(item.get("confidence") or 0) for item in result.boxes if isinstance(item, dict)]
        box_values.extend(retained)
        frame_values.append(float(result.confidence))
        expected = sum(retained) / len(retained) if retained else 0.0
        if abs(expected - float(result.confidence)) > tolerance:
            mismatches += 1
    return {
        "aggregation": "arithmetic_mean_of_retained_boxes",
        "mismatch_count": mismatches,
        "box_confidence": confidence_distribution(box_values),
        "frame_confidence": confidence_distribution(frame_values),
    }


def _find_input_path(value):
    if isinstance(value, dict):
        if value.get("input_path"):
            return str(value["input_path"])
        for item in value.values():
            found = _find_input_path(item)
            if found:
                return found
    elif isinstance(value, (list, tuple)):
        for item in value:
            found = _find_input_path(item)
            if found:
                return found
    return ""


def _sha256(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _provider_result(frame, provider):
    return next(item for item in frame.provider_results if item.provider == provider)


def _existing_latency_by_frame(fusions, provider):
    output = {}
    previous = 0.0
    for frame in fusions:
        current = float(frame.metrics.providers.get(provider, {}).get("latency_ms", previous))
        output[frame.frame_id] = max(0.0, current - previous)
        previous = current
    return output


def _provider_summary(results, latencies):
    recognized = [item for item in results if item.status == "completed" and item.text]
    no_text = [item for item in results if item.status == "completed" and not item.text]
    geometry = [item for item in recognized if item.polygons and item.bounding_boxes]
    return {
        "total": len(results),
        "successful_recognition_count": len(recognized),
        "no_text_count": len(no_text),
        "failure_count": len(results) - len(recognized) - len(no_text),
        "confidence_all_frames": confidence_distribution(item.confidence for item in results),
        "confidence_recognized": confidence_distribution(item.confidence for item in recognized),
        "geometry_complete_count": len(geometry),
        "geometry_completeness_rate": round(len(geometry) / max(len(recognized), 1), 6),
        "average_latency_ms": round(sum(latencies) / max(len(latencies), 1), 3),
    }


def _comparison_summary(paddles, candidates):
    categories = Counter()
    details = []
    for paddle, candidate in zip(paddles, candidates):
        if not paddle.text and not candidate.text:
            category = "both_no_text"
            compared = {
                "category": category, "similarity_ratio": 1.0,
                "english_acronym_disagreement": False,
                "numeric_disagreement": False, "chinese_disagreement": False,
                "critical_conflict_types": [],
            }
        elif not paddle.text or not candidate.text:
            category = "conflict"
            compared = {
                "category": category, "similarity_ratio": 0.0,
                "english_acronym_disagreement": False,
                "numeric_disagreement": False,
                "chinese_disagreement": any(
                    "\u4e00" <= char <= "\u9fff" for char in paddle.text + candidate.text
                ),
                "critical_conflict_types": [],
            }
        else:
            compared = compare_to_paddle(paddle, candidate)
            category = compared["category"]
        categories[category] += 1
        details.append(compared)
    comparable = categories["exact"] + categories["similar"] + categories["conflict"]
    return {
        "counts": dict(categories),
        "comparable_count": comparable,
        "exact_rate": round(categories["exact"] / max(comparable, 1), 6),
        "similar_rate": round(categories["similar"] / max(comparable, 1), 6),
        "conflict_rate": round(categories["conflict"] / max(comparable, 1), 6),
        "english_acronym_disagreement_count": sum(item["english_acronym_disagreement"] for item in details),
        "chinese_disagreement_count": sum(item["chinese_disagreement"] for item in details),
        "numeric_disagreement_count": sum(item["numeric_disagreement"] for item in details),
    }


TOKEN_QUERIES = {
    "EAL_ESL": ("EAL", "ESL"), "GPA": ("GPA",), "PR": ("PR",),
    "30_PERCENT": ("30%",), "30_WAN": ("30万",),
    "IELTS_5": ("雅思五分", "雅思5分"), "YEAR_7": ("第7年", "第七年"),
    "GAO_ER": ("高二",), "CHILD_EXAM": ("孩子考砸了",),
    "GRADUATION": ("毕不了业", "毕业"), "XIA_MA_WEI": ("下马威",),
    "POLICY": ("政策",), "WORK_VISA": ("工签",),
    "ENGLISH_TUTORING": ("补英语",), "DRIVER_LICENCE": ("驾照",),
    "CANADA": ("加拿大",), "CHILD_ABROAD": ("带娃", "带我走出国"),
}


def _token_evidence(frames, provider_rows):
    output = {}
    for label, variants in TOKEN_QUERIES.items():
        matches = []
        for frame, row in zip(frames, provider_rows):
            texts = {name: result.text for name, result in row.items()}
            found = sorted({variant for variant in variants if any(variant in text for text in texts.values())})
            if found:
                matches.append({
                    "frame_id": frame.frame_id, "start_ms": frame.start_ms,
                    "end_ms": frame.end_ms, "matched": found, "provider_texts": texts,
                })
        output[label] = matches if matches else "NO OCR EVIDENCE"
    return output


def run_benchmark(result_path, checkpoint_path, config):
    report = json.loads(Path(result_path).read_text(encoding="utf-8"))
    with Path(checkpoint_path).open("rb") as handle:
        fusions = tuple(pickle.load(handle))
    accepted_ids = {
        item["frame_id"] for item in report["ocr_frame_results"]
        if item["post_ocr_evidence"]["accepted"]
    }
    accepted = tuple(frame for frame in fusions if frame.frame_id in accepted_ids)
    if len(accepted) != 103:
        raise RuntimeError(f"expected 103 accepted frames, got {len(accepted)}")

    paddle_results = tuple(_provider_result(frame, "paddle") for frame in accepted)
    easy_results = tuple(_provider_result(frame, "easyocr") for frame in accepted)
    paddle_latency = _existing_latency_by_frame(fusions, "paddle")
    easy_latency = _existing_latency_by_frame(fusions, "easyocr")
    client = AppleVisionWorkerClient(config)
    adapter = AppleVisionOCRAdapter(client)
    apple_results = []
    apple_wall = []
    paths = []
    started = time.perf_counter()
    try:
        for index, (frame, paddle) in enumerate(zip(accepted, paddle_results), 1):
            path = Path(_find_input_path(paddle.raw_response)).resolve()
            if not path.is_file():
                raise FileNotFoundError(f"accepted crop missing for {frame.frame_id}")
            paths.append(str(path))
            segment = SubtitleSegment(
                frame.frame_id, paddle.source_video_id or "benchmark-job-18",
                frame.start_ms, frame.end_ms, str(path), _sha256(path),
                "opencv-subtitle-v2",
            )
            call_started = time.perf_counter()
            apple_results.append(adapter.recognize(segment))
            apple_wall.append((time.perf_counter() - call_started) * 1000.0)
            if index % 20 == 0:
                print(json.dumps({"event": "apple_vision_progress", "processed": index, "total": len(accepted)}), flush=True)
    finally:
        client.close()
    total_ms = (time.perf_counter() - started) * 1000.0
    apple_results = tuple(apple_results)
    rows = tuple({"paddle": p, "easyocr": e, "apple_vision": a} for p, e, a in zip(paddle_results, easy_results, apple_results))
    output = {
        "benchmark": {
            "safebox": 180, "purpose": "provider_suitability",
            "is_ground_truth": False, "accepted_crop_count": len(accepted),
            "reran_video_or_existing_providers": False,
            "vision_languages": list(config.languages), "vision_revision": config.revision,
            "vision_recognition_level": config.recognition_level,
        },
        "providers": {
            "paddle": _provider_summary(paddle_results, [paddle_latency[item.frame_id] for item in accepted]),
            "easyocr": _provider_summary(easy_results, [easy_latency[item.frame_id] for item in accepted]),
            "apple_vision": _provider_summary(apple_results, apple_wall),
        },
        "comparison_to_paddle": {
            "easyocr": _comparison_summary(paddle_results, easy_results),
            "apple_vision": _comparison_summary(paddle_results, apple_results),
        },
        "easyocr_confidence_audit": audit_easyocr_confidence(easy_results),
        "critical_token_evidence": _token_evidence(accepted, rows),
        "apple_vision_runtime": {
            "worker_start_count": client.start_count,
            "inference_count": client.inference_count,
            "worker_exit": client.worker_exit,
            "total_wall_ms": round(total_ms, 3),
            "network_deny_profile": bool(config.sandbox_profile),
            "network_request_count": sum(int((item.raw_response or {}).get("network_request_count") or 0) for item in apple_results),
            "cost": 0,
            "children_peak_rss_raw": resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss,
            "rss_unit": "bytes on macOS",
        },
        "frames": [{
            "frame_id": frame.frame_id, "start_ms": frame.start_ms, "end_ms": frame.end_ms,
            "crop_path": path,
            "providers": {name: {
                "status": item.status, "text": item.text, "confidence": item.confidence,
                "polygon_count": len(item.polygons), "bounding_box_count": len(item.bounding_boxes),
                "inference_ms": (item.raw_response or {}).get("inference_ms"),
                "error": item.error,
            } for name, item in row.items()},
            "easyocr_vs_paddle": compare_to_paddle(row["paddle"], row["easyocr"]) if row["paddle"].text and row["easyocr"].text else None,
            "apple_vision_vs_paddle": compare_to_paddle(row["paddle"], row["apple_vision"]) if row["paddle"].text and row["apple_vision"].text else None,
        } for frame, path, row in zip(accepted, paths, rows)],
    }
    return output


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--result", required=True)
    parser.add_argument("--checkpoint", required=True)
    parser.add_argument("--binary", required=True)
    parser.add_argument("--state-dir", required=True)
    parser.add_argument("--sandbox-profile", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    output = run_benchmark(args.result, args.checkpoint, AppleVisionWorkerConfig(
        binary_path=args.binary, state_dir=args.state_dir,
        sandbox_profile=args.sandbox_profile, timeout_seconds=60,
    ))
    target = Path(args.output)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(".tmp")
    temporary.write_text(json.dumps(output, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(target)
    print(json.dumps({"event": "benchmark_complete", "output": str(target.resolve())}, ensure_ascii=False))


if __name__ == "__main__":
    main()
