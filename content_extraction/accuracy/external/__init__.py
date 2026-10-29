"""Fail-closed external candidate providers for Accurate Transcript."""

from .iflytek_spark_lite import SparkLiteAdapter
from .schemas import SparkLiteConfig, SparkProposalAudit, SparkUsage

__all__ = (
    "SparkLiteAdapter", "SparkLiteConfig", "SparkProposalAudit", "SparkUsage",
)
