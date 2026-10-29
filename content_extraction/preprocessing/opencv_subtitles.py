import hashlib
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class CropRegion:
    left: float
    top: float
    right: float
    bottom: float

    def validate(self):
        values = (self.left, self.top, self.right, self.bottom)
        if any(value < 0 or value > 1 for value in values):
            raise ValueError("crop region must use 0..1 ratios")
        if self.left >= self.right or self.top >= self.bottom:
            raise ValueError("crop region has no area")
        return self

    @property
    def width_ratio(self):
        return self.right - self.left

    @property
    def height_ratio(self):
        return self.bottom - self.top


@dataclass(frozen=True)
class CandidateEvidence:
    accepted: bool
    classification: str
    rejection_reason: str
    region: CropRegion
    line_band_count: int
    edge_density: float
    foreground_density: float
    compactness: float
    center_offset: float
    document_line_count: int
    source_width: int
    source_height: int

    @classmethod
    def for_test(cls, *, top=0.4, height=0.08, classification="candidate"):
        return cls(
            classification == "candidate", classification,
            "" if classification == "candidate" else classification,
            CropRegion(0.2, top, 0.8, min(1.0, top + height)),
            1, 0.1, 0.1, 0.8, 0.0,
            8 if classification == "screen_document" else 0,
            720, 1280,
        )

    def to_dict(self):
        return {
            "accepted": bool(self.accepted),
            "classification": self.classification,
            "rejection_reason": self.rejection_reason,
            "region": {
                "left": self.region.left,
                "top": self.region.top,
                "right": self.region.right,
                "bottom": self.region.bottom,
                "width_ratio": self.region.width_ratio,
                "height_ratio": self.region.height_ratio,
            },
            "line_band_count": int(self.line_band_count),
            "edge_density": float(self.edge_density),
            "foreground_density": float(self.foreground_density),
            "compactness": float(self.compactness),
            "center_offset": float(self.center_offset),
            "document_line_count": int(self.document_line_count),
            "source_width": int(self.source_width),
            "source_height": int(self.source_height),
        }


@dataclass(frozen=True)
class PreprocessingProfile:
    version: str
    minimum_score: float
    candidate_regions: tuple = (
        CropRegion(0.02, 0.55, 0.98, 0.98),
        CropRegion(0.02, 0.35, 0.98, 0.75),
        CropRegion(0.02, 0.10, 0.98, 0.50),
    )

    def __post_init__(self):
        if not self.version or self.minimum_score < 0:
            raise ValueError("invalid preprocessing profile")
        for region in self.candidate_regions:
            region.validate()


@dataclass(frozen=True)
class DetectedSubtitleCrop:
    path: str
    image_hash: str
    perceptual_signature: int
    content_score: float
    region: CropRegion
    width: int
    height: int
    evidence: CandidateEvidence | None = None


@dataclass(frozen=True)
class SubtitleCandidateDecision:
    crop: DetectedSubtitleCrop | None
    evidence: CandidateEvidence


class OpenCVSubtitleDetector:
    def __init__(self, profile, cv2_module=None):
        self.profile = profile
        if cv2_module is None:
            import cv2 as cv2_module
        self.cv2 = cv2_module

    @staticmethod
    def _bounds(region, width, height):
        region.validate()
        return (
            max(0, round(region.left * width)),
            max(0, round(region.top * height)),
            min(width, round(region.right * width)),
            min(height, round(region.bottom * height)),
        )

    def _score(self, crop):
        gray = self.cv2.cvtColor(crop, self.cv2.COLOR_BGR2GRAY)
        edges = self.cv2.Canny(gray, 60, 180)
        contrast = float(gray.std()) / 255.0
        edge_density = float((edges > 0).mean())
        return contrast * 0.75 + edge_density * 0.25

    def _signature(self, crop):
        gray = self.cv2.cvtColor(crop, self.cv2.COLOR_BGR2GRAY)
        resized = self.cv2.resize(gray, (9, 8), interpolation=self.cv2.INTER_AREA)
        comparisons = resized[:, 1:] > resized[:, :-1]
        signature = 0
        for value in comparisons.flatten():
            signature = (signature << 1) | int(value)
        return signature

    @staticmethod
    def _groups(values):
        groups = []
        for value in sorted(values):
            if not groups or value > groups[-1][-1] + 1:
                groups.append([value])
            else:
                groups[-1].append(value)
        return groups

    def _localize(self, image):
        height, width = image.shape[:2]
        gray = self.cv2.cvtColor(image, self.cv2.COLOR_BGR2GRAY)
        edges = self.cv2.Canny(gray, 60, 180)
        kernel = self.cv2.getStructuringElement(
            self.cv2.MORPH_RECT,
            (max(9, round(width * 0.04)), max(2, round(height * 0.002))),
        )
        joined = self.cv2.morphologyEx(edges, self.cv2.MORPH_CLOSE, kernel)
        contours, _hierarchy = self.cv2.findContours(
            joined, self.cv2.RETR_EXTERNAL, self.cv2.CHAIN_APPROX_SIMPLE
        )
        raw_boxes = []
        large_boxes = []
        document_lines = 0
        for contour in contours:
            x, y, box_width, box_height = self.cv2.boundingRect(contour)
            width_ratio = box_width / width
            height_ratio = box_height / height
            if width_ratio >= 0.14 and 0.006 <= height_ratio <= 0.14:
                raw_boxes.append((x, y, x + box_width, y + box_height))
                if height_ratio < 0.022:
                    document_lines += 1
                elif width_ratio >= 0.18:
                    large_boxes.append((x, y, x + box_width, y + box_height))

        candidates = []
        ordered = sorted(large_boxes, key=lambda item: (item[1], item[0]))
        for index, first in enumerate(ordered):
            group = [first]
            for second in ordered[index + 1:]:
                gap = second[1] - group[-1][3]
                first_center = (group[-1][0] + group[-1][2]) / 2
                second_center = (second[0] + second[2]) / 2
                if (
                    len(group) < 2
                    and -height * 0.01 <= gap <= height * 0.055
                    and abs(first_center - second_center) <= width * 0.22
                ):
                    group.append(second)
                    break
            left = min(item[0] for item in group)
            top = min(item[1] for item in group)
            right = max(item[2] for item in group)
            bottom = max(item[3] for item in group)
            region_width = (right - left) / width
            region_height = (bottom - top) / height
            center_offset = abs(((left + right) / 2 / width) - 0.5)
            if region_height > 0.22 or center_offset > 0.32:
                continue
            local_edges = edges[top:bottom, left:right]
            edge_density = float((local_edges > 0).mean()) if local_edges.size else 0.0
            foreground_density = float((gray[top:bottom, left:right] > 180).mean())
            compactness = min(1.0, region_width / max(region_height * 5.0, 0.01))
            score = (
                region_width * 1.8
                + min(edge_density, 0.35)
                + compactness * 0.25
                - center_offset * 0.8
                - max(0, len(group) - 2)
            )
            candidates.append((score, group, (left, top, right, bottom), (
                edge_density, foreground_density, compactness, center_offset
            )))

        if not candidates:
            classification = "screen_document" if document_lines >= 5 else (
                "ui" if contours else "unknown"
            )
            reason = (
                "dense_multiline_document" if classification == "screen_document"
                else "fragmented_or_off_center_ui" if classification == "ui"
                else "no_compact_text_band"
            )
            evidence = CandidateEvidence(
                False, classification, reason, CropRegion(0.0, 0.0, 1.0, 1.0),
                0, float((edges > 0).mean()), float((gray > 180).mean()),
                0.0, 0.5, document_lines, width, height,
            )
            return None, evidence

        _score, group, bounds, metrics = max(candidates, key=lambda item: item[0])
        left, top, right, bottom = bounds
        x_pad = round(width * 0.025)
        y_pad = round(height * 0.012)
        left = max(0, left - x_pad)
        right = min(width, right + x_pad)
        top = max(0, top - y_pad)
        bottom = min(height, bottom + y_pad)
        region = CropRegion(left / width, top / height, right / width, bottom / height)
        edge_density, foreground_density, compactness, center_offset = metrics
        evidence = CandidateEvidence(
            True, "candidate", "", region, len(group), edge_density,
            foreground_density, compactness, center_offset,
            document_lines, width, height,
        )
        return image[top:bottom, left:right], evidence

    def inspect(self, frame_path, output_path, region_override=None):
        image = self.cv2.imread(str(frame_path))
        if image is None:
            raise ValueError("unable to read video frame")
        height, width = image.shape[:2]
        if region_override is not None:
            region = region_override.validate()
            left, top, right, bottom = self._bounds(region, width, height)
            crop = image[top:bottom, left:right]
            score = self._score(crop) if crop.size else 0.0
            accepted = bool(crop.size and score >= self.profile.minimum_score)
            evidence = CandidateEvidence(
                accepted, "candidate" if accepted else "unknown",
                "" if accepted else "minimum_content_score",
                region, 1 if accepted else 0, score, 0.0, 1.0, 0.0,
                0, width, height,
            )
        else:
            crop, evidence = self._localize(image)
            score = self._score(crop) if crop is not None and crop.size else 0.0
            if crop is not None and score < self.profile.minimum_score:
                evidence = CandidateEvidence(
                    False, "unknown", "minimum_content_score", evidence.region,
                    evidence.line_band_count, score, evidence.foreground_density,
                    evidence.compactness, evidence.center_offset,
                    evidence.document_line_count, width, height,
                )
                crop = None
        if crop is None or not evidence.accepted:
            return SubtitleCandidateDecision(None, evidence)

        output_path = Path(output_path)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        if not self.cv2.imwrite(str(output_path), crop):
            raise OSError("unable to write subtitle crop")
        image_hash = hashlib.sha256(crop.tobytes()).hexdigest()
        crop_height, crop_width = crop.shape[:2]
        result = DetectedSubtitleCrop(
            str(output_path), image_hash, self._signature(crop), score,
            evidence.region, crop_width, crop_height, evidence,
        )
        return SubtitleCandidateDecision(result, evidence)

    def detect(self, frame_path, output_path, region_override=None):
        return self.inspect(frame_path, output_path, region_override).crop
