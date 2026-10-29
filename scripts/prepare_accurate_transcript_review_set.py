#!/usr/bin/env python3
"""Prepare a PENDING human-review sheet; never labels machine text as truth."""

import argparse
import hashlib
import json
from pathlib import Path


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--benchmark-result", type=Path, required=True)
    parser.add_argument("--media", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--count", type=int, default=40)
    args = parser.parse_args()
    source = json.loads(args.benchmark_result.read_text(encoding="utf-8"))
    segments = source["cross_modal_result"]["segments"]
    ranked = sorted(segments, key=lambda item: (
        not bool(item.get("conflicts")),
        -len(item.get("alternatives") or ()),
        item.get("confidence_level") == "HIGH",
        int(item.get("start_ms", 0)),
    ))[:args.count]
    media_hash = hashlib.sha256(args.media.read_bytes()).hexdigest()
    samples = []
    for index, item in enumerate(sorted(ranked, key=lambda value: int(value.get("start_ms", 0))), 1):
        samples.append({
            "sample_id": f"review-job18-{index:03d}",
            "source": {"benchmark": "job_18_final_paddle_apple",
                       "segment_id": item["id"], "media_sha256": media_hash,
                       "start_ms": item["start_ms"], "end_ms": item["end_ms"]},
            "machine_evidence_only": True,
            "is_ground_truth": False,
            "before_text": item.get("clean_text", ""),
            "provider_alternatives": item.get("alternatives", []),
            "conflicts": item.get("conflicts", []),
            "review": {
                "decision": "PENDING_HUMAN_REVIEW",
                "allowed_decisions": ["CORRECTION_NEEDED", "NO_CORRECTION_NEEDED", "AMBIGUOUS"],
                "human_verified_text": None,
                "first_pass_completed": False,
                "second_pass_completed": False,
            },
        })
    output = {
        "schema_version": "accurate-review-candidates-v1",
        "status": "PENDING_HUMAN_VERIFICATION",
        "warning": "This file is not Ground Truth and cannot be benchmarked until two human review passes are complete.",
        "sample_count": len(samples), "samples": samples,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(output, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    args.output.chmod(0o600)
    print(json.dumps({"status": output["status"], "sample_count": len(samples),
                      "output": str(args.output)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
