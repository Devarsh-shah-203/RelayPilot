"""Provider-independent attachment planning and upload orchestration."""

from .manager import AttachmentManager
from .models import AttachmentRecord, AttachmentStatus, DeliveryMode, UploadReport
from .policy import DeliveryDecision, choose_delivery_mode, classify_file, estimate_tokens

__all__ = [
    "AttachmentManager", "AttachmentRecord", "AttachmentStatus", "DeliveryMode",
    "UploadReport", "DeliveryDecision", "choose_delivery_mode", "classify_file",
    "estimate_tokens",
]
