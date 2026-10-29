from .apple_vision import (
    AppleVisionOCRAdapter,
    AppleVisionWorkerClient,
    AppleVisionWorkerConfig,
)
from .iflytek import (
    IFlytekGeneralOCRAdapter,
    IFlytekOCRAdapter,
    IFlytekOCRConfig,
    IFlytekUniversalOCRClient,
)
from .easyocr import EasyOCRAdapter, EasyOCRWorkerClient, EasyOCRWorkerConfig
from .evidence import OCRCrossModalEvidence, OCRCrossModalEvidenceResolver
from .local_first import FrameOCRFusion, LocalFirstOCRFusionCoordinator
from .temporal import OCRFusedTranscript, TemporalOCRSegment, merge_fused_frames
from .evidence_gate import OCRFrameEvidenceDecision, evaluate_frame_evidence
from .paddle import PaddleOCRAdapter, PaddleOCRV5Engine
from .tencent import (
    TencentGeneralAccurateOCRAdapter,
    TencentOCRAdapter,
    TencentOCRConfig,
    TencentSDKGeneralAccurateClient,
)

__all__ = [
    "AppleVisionOCRAdapter",
    "AppleVisionWorkerClient",
    "AppleVisionWorkerConfig",
    "IFlytekGeneralOCRAdapter",
    "IFlytekOCRAdapter",
    "IFlytekOCRConfig",
    "IFlytekUniversalOCRClient",
    "EasyOCRAdapter",
    "EasyOCRWorkerClient",
    "EasyOCRWorkerConfig",
    "OCRCrossModalEvidence",
    "OCRCrossModalEvidenceResolver",
    "FrameOCRFusion",
    "LocalFirstOCRFusionCoordinator",
    "OCRFusedTranscript",
    "OCRFrameEvidenceDecision",
    "TemporalOCRSegment",
    "merge_fused_frames",
    "evaluate_frame_evidence",
    "PaddleOCRAdapter",
    "PaddleOCRV5Engine",
    "TencentGeneralAccurateOCRAdapter",
    "TencentOCRAdapter",
    "TencentOCRConfig",
    "TencentSDKGeneralAccurateClient",
]
