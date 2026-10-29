from dataclasses import dataclass

from ..shared.recognition_text import normalize_for_comparison


@dataclass(frozen=True)
class OCRFrameEvidenceDecision:
    accepted: bool
    role: str
    rejection_reason: str
    line_count: int
    text_density: float
    box_geometry_valid: bool

    def to_dict(self):
        return {
            "accepted": bool(self.accepted),
            "role": self.role,
            "rejection_reason": self.rejection_reason,
            "line_count": int(self.line_count),
            "text_density": float(self.text_density),
            "box_geometry_valid": bool(self.box_geometry_valid),
        }


def evaluate_frame_evidence(frame, role, crop_evidence=None):
    """Apply the post-OCR gate without changing provider selection or voting."""

    text = str(getattr(frame, "fused_text", "") or "")
    normalized = normalize_for_comparison(text)
    line_count = len(tuple(line for line in text.splitlines() if line.strip()))
    if normalized and line_count == 0:
        line_count = 1
    bounding = dict(getattr(frame, "bounding_region", {}) or {})
    geometry_valid = True
    area = 0.0
    if bounding:
        try:
            box_width = float(bounding["width"])
            box_height = float(bounding["height"])
            geometry_valid = box_width > 0 and box_height > 0
            area = max(1.0, box_width * box_height)
            if crop_evidence is not None:
                crop_width = (
                    crop_evidence.source_width
                    * crop_evidence.region.width_ratio
                )
                crop_height = (
                    crop_evidence.source_height
                    * crop_evidence.region.height_ratio
                )
                geometry_valid = bool(
                    geometry_valid
                    and box_width <= crop_width * 1.05
                    and box_height <= crop_height * 1.05
                )
        except (KeyError, TypeError, ValueError):
            geometry_valid = False
    text_density = round(len(normalized) / area, 6) if area else 0.0

    role = str(role or "unknown")
    if role != "spoken_subtitle":
        reason = f"role_{role}"
        return OCRFrameEvidenceDecision(
            False, role, reason, line_count, text_density, geometry_valid
        )
    if not normalized:
        return OCRFrameEvidenceDecision(
            False, role, "no_ocr_text", line_count, text_density, geometry_valid
        )
    if line_count > 3 or len(normalized) > 100:
        return OCRFrameEvidenceDecision(
            False, role, "dense_text_not_subtitle", line_count,
            text_density, geometry_valid,
        )
    if not geometry_valid:
        return OCRFrameEvidenceDecision(
            False, role, "invalid_box_geometry", line_count,
            text_density, geometry_valid,
        )
    return OCRFrameEvidenceDecision(
        True, role, "", line_count, text_density, geometry_valid
    )
