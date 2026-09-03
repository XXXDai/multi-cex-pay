"""二维码识别、裁剪与聚合。"""

from .compose import ComposeResult, Panel, compose, compose_from_paths
from .detect import (
    CropResult,
    QRHit,
    crop_qr,
    detect_qrcodes,
    guess_brand,
    load_image,
    render_qr,
    verify_readable,
)

__all__ = [
    "ComposeResult",
    "CropResult",
    "Panel",
    "QRHit",
    "compose",
    "compose_from_paths",
    "crop_qr",
    "detect_qrcodes",
    "guess_brand",
    "load_image",
    "render_qr",
    "verify_readable",
]
