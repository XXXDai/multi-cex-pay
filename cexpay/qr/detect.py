"""二维码识别与裁剪。

商家通常是直接截图交易所 App 的收款页，图里除了二维码还有一堆 UI。
这个模块负责：

  1. 在整张截图里定位二维码（支持多码、支持倾斜拍照）
  2. 透视校正后裁出干净的码区，自动补足静默区(quiet zone)
  3. 只要能解出内容，就按原文重新生成一张高容错的标准二维码
     （比像素裁剪更清晰，也便于后面拼合成聚合图）

识别引擎按可用性依次尝试 OpenCV → pyzbar，并对图像做多种预处理重试，
截图、照片、带 logo 的码都能覆盖。
"""

from __future__ import annotations

import io
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np
from PIL import Image

from ..errors import QRError

ImageSource = str | Path | bytes | Image.Image | np.ndarray


@dataclass
class QRHit:
    """一次二维码识别结果。"""

    payload: str
    # 四个角点，顺序为左上/右上/右下/左下
    quad: list[tuple[float, float]]
    engine: str = ""

    @property
    def bbox(self) -> tuple[int, int, int, int]:
        xs = [p[0] for p in self.quad]
        ys = [p[1] for p in self.quad]
        return int(min(xs)), int(min(ys)), int(max(xs)), int(max(ys))

    @property
    def area(self) -> float:
        x0, y0, x1, y1 = self.bbox
        return max(0, x1 - x0) * max(0, y1 - y0)

    @property
    def side(self) -> float:
        x0, y0, x1, y1 = self.bbox
        return max(x1 - x0, y1 - y0)

    def to_dict(self) -> dict[str, Any]:
        x0, y0, x1, y1 = self.bbox
        return {
            "payload": self.payload,
            "bbox": {"x": x0, "y": y0, "w": x1 - x0, "h": y1 - y0},
            "quad": [[round(x, 1), round(y, 1)] for x, y in self.quad],
            "engine": self.engine,
            "brand": guess_brand(self.payload),
        }


@dataclass
class CropResult:
    """裁剪 / 重绘的产物。"""

    image: Image.Image
    hit: QRHit | None
    regenerated: bool
    warnings: list[str] = field(default_factory=list)
    all_hits: list[QRHit] = field(default_factory=list)

    @property
    def payload(self) -> str | None:
        return self.hit.payload if self.hit else None

    def save(self, path: str | Path) -> Path:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self.image.save(path)
        return path

    def to_dict(self) -> dict[str, Any]:
        return {
            "payload": self.payload,
            "regenerated": self.regenerated,
            "warnings": self.warnings,
            "brand": guess_brand(self.payload or ""),
            "hits": [h.to_dict() for h in self.all_hits],
        }


# --------------------------------------------------------------------------
# 品牌识别：从二维码内容反推它属于哪个交易所
# --------------------------------------------------------------------------
BRAND_PATTERNS: list[tuple[str, tuple[str, ...]]] = [
    ("binance", ("binance.com", "app.binance", "b.binance", "binance.me")),
    ("okx", ("okx.com", "okex.com", "www.okx", "okx.me")),
    ("bitget", ("bitget.com", "bitget.me", "bitgetapps.com")),
]


def guess_brand(payload: str) -> str | None:
    """根据二维码内容猜测所属交易所，猜不出返回 None。"""
    if not payload:
        return None
    lowered = payload.lower()
    for brand, needles in BRAND_PATTERNS:
        if any(needle in lowered for needle in needles):
            return brand
    return None


# --------------------------------------------------------------------------
# 图像装载
# --------------------------------------------------------------------------
def load_image(source: ImageSource) -> Image.Image:
    """把各种输入统一成 RGB 的 PIL Image。"""
    if isinstance(source, Image.Image):
        return source.convert("RGB")
    if isinstance(source, np.ndarray):
        array = source
        if array.ndim == 2:
            return Image.fromarray(array).convert("RGB")
        # OpenCV 是 BGR
        return Image.fromarray(array[:, :, ::-1]).convert("RGB")
    if isinstance(source, bytes):
        try:
            return Image.open(io.BytesIO(source)).convert("RGB")
        except Exception as exc:
            raise QRError(f"无法解析图片数据：{exc}") from exc
    path = Path(source)
    if not path.exists():
        raise QRError(f"图片不存在：{path}")
    try:
        return Image.open(path).convert("RGB")
    except Exception as exc:
        raise QRError(f"无法打开图片 {path}：{exc}") from exc


def _to_cv(image: Image.Image) -> np.ndarray:
    return np.array(image)[:, :, ::-1].copy()  # RGB -> BGR


# --------------------------------------------------------------------------
# 识别
# --------------------------------------------------------------------------
def _detect_opencv(image: Image.Image, expected: int | None = None) -> list[QRHit]:
    """多种预处理轮流上，结果累加后去重。

    不在"第一个有结果的变体"就收手：一张图里码的密度可能差很多，
    原图能解出两个、第三个更密的码却要放大才解得出。早退会静默漏码，
    所以只有在拿到 ``expected`` 个不同内容之后才提前结束。
    """
    try:
        import cv2
    except ImportError:
        return []

    frame = _to_cv(image)
    detector = cv2.QRCodeDetector()
    hits: list[QRHit] = []

    gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
    variants: list[tuple[str, np.ndarray, float]] = [
        ("raw", frame, 1.0),
        ("gray", gray, 1.0),
        (
            "adaptive",
            cv2.adaptiveThreshold(
                gray, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C, cv2.THRESH_BINARY, 31, 5
            ),
            1.0,
        ),
        ("upscale2x", cv2.resize(gray, None, fx=2, fy=2, interpolation=cv2.INTER_CUBIC), 2.0),
        # 深色模式截图里二维码是反相的，取反再试一次
        ("invert", cv2.bitwise_not(gray), 1.0),
    ]

    for label, variant, scale in variants:
        try:
            ok, decoded, points, _ = detector.detectAndDecodeMulti(variant)
        except cv2.error:
            continue
        if ok and points is not None:
            # strict=False：真出现 OpenCV 返回长度不一致的怪情况，宁可少认一个码也别抛异常
            for text, quad in zip(decoded, points, strict=False):
                if not text:
                    continue
                pts = [(float(x) / scale, float(y) / scale) for x, y in quad]
                hits.append(QRHit(payload=text, quad=pts, engine=f"opencv:{label}"))

        if expected is not None and len({h.payload for h in hits}) >= expected:
            break

    return _dedupe(hits)


def _detect_pyzbar(image: Image.Image) -> list[QRHit]:
    try:
        from pyzbar import pyzbar
    except Exception:
        return []

    hits: list[QRHit] = []
    for symbol in pyzbar.decode(image):
        if symbol.type != "QRCODE":
            continue
        try:
            payload = symbol.data.decode("utf-8")
        except UnicodeDecodeError:
            payload = symbol.data.decode("latin-1", errors="replace")
        if symbol.polygon and len(symbol.polygon) >= 4:
            quad = [(float(p.x), float(p.y)) for p in symbol.polygon[:4]]
        else:
            rect = symbol.rect
            quad = [
                (float(rect.left), float(rect.top)),
                (float(rect.left + rect.width), float(rect.top)),
                (float(rect.left + rect.width), float(rect.top + rect.height)),
                (float(rect.left), float(rect.top + rect.height)),
            ]
        hits.append(QRHit(payload=payload, quad=quad, engine="pyzbar"))
    return _dedupe(hits)


def _dedupe(hits: Sequence[QRHit]) -> list[QRHit]:
    seen: dict[str, QRHit] = {}
    for hit in hits:
        existing = seen.get(hit.payload)
        # 同样内容保留面积更大的那个框
        if existing is None or hit.area > existing.area:
            seen[hit.payload] = hit
    return sorted(seen.values(), key=lambda h: h.area, reverse=True)


def detect_qrcodes(source: ImageSource, *, expected: int | None = None) -> list[QRHit]:
    """识别图中所有二维码，按面积从大到小返回。

    ``expected`` 是"我知道这张图里应该有几个码"的提示：给了就能在凑够数量后
    提前收手，省掉多余的预处理；不给则把所有预处理都跑一遍，尽量不漏。
    """
    image = load_image(source)
    hits = _detect_opencv(image, expected=expected)
    if not hits:
        hits = _detect_pyzbar(image)
    return hits


# --------------------------------------------------------------------------
# 裁剪 / 重绘
# --------------------------------------------------------------------------
def _order_quad(quad: Sequence[tuple[float, float]]) -> np.ndarray:
    """把四个角点排成 左上→右上→右下→左下。"""
    pts = np.array(quad, dtype="float32")
    total = pts.sum(axis=1)
    diff = np.diff(pts, axis=1).ravel()
    return np.array(
        [
            pts[np.argmin(total)],  # 左上：x+y 最小
            pts[np.argmin(diff)],   # 右上：x-y 最大 => y-x 最小
            pts[np.argmax(total)],  # 右下
            pts[np.argmax(diff)],   # 左下
        ],
        dtype="float32",
    )


def warp_crop(
    image: Image.Image,
    hit: QRHit,
    *,
    size: int = 640,
    margin_ratio: float = 0.10,
) -> Image.Image:
    """透视校正裁剪：拍歪的照片也能拉正成方方正正的码。"""
    try:
        import cv2
    except ImportError:
        return _bbox_crop(image, hit, margin_ratio=margin_ratio).resize(
            (size, size), Image.Resampling.LANCZOS
        )

    src = _order_quad(hit.quad)
    inner = max(64, int(size * (1 - 2 * margin_ratio)))
    offset = (size - inner) / 2
    dst = np.array(
        [
            [offset, offset],
            [offset + inner, offset],
            [offset + inner, offset + inner],
            [offset, offset + inner],
        ],
        dtype="float32",
    )

    matrix = cv2.getPerspectiveTransform(src, dst)
    warped = cv2.warpPerspective(
        _to_cv(image),
        matrix,
        (size, size),
        flags=cv2.INTER_CUBIC,
        borderMode=cv2.BORDER_CONSTANT,
        borderValue=(255, 255, 255),  # 静默区补白
    )
    return Image.fromarray(warped[:, :, ::-1])


def _bbox_crop(image: Image.Image, hit: QRHit, *, margin_ratio: float = 0.10) -> Image.Image:
    x0, y0, x1, y1 = hit.bbox
    margin = int(max(x1 - x0, y1 - y0) * margin_ratio)
    box = (
        max(0, x0 - margin),
        max(0, y0 - margin),
        min(image.width, x1 + margin),
        min(image.height, y1 + margin),
    )
    cropped = image.crop(box)
    # 补成正方形并填白，保证静默区完整
    side = max(cropped.size)
    canvas = Image.new("RGB", (side, side), "white")
    canvas.paste(cropped, ((side - cropped.width) // 2, (side - cropped.height) // 2))
    return canvas


def render_qr(payload: str, *, size: int = 640, border: int = 4) -> Image.Image:
    """按内容重新生成一张标准二维码（高容错等级，便于中心贴 logo）。

    不做缩放。二维码是模块化图形，缩放到任意像素尺寸会让模块边界落在
    像素中间：一部分模块 8px 宽、另一部分 9px 宽，解码器有一定概率直接失败。
    而且它不随尺寸单调变化：440 和 520 都正常，偏偏 460 挂掉。

    所以这里反过来做：先算出每个模块占几个整数像素（向下取整），
    生成 ``box_size * modules`` 的图，再居中贴到 ``size × size`` 的白底上。
    补的白边等于加宽静默区，对扫码只有好处。代价是实际码面可能比 size 略小。
    """
    try:
        import qrcode
        from qrcode.constants import ERROR_CORRECT_H
    except ImportError as exc:
        raise QRError(
            "重绘二维码需要 qrcode 库，请执行 pip install 'qrcode[pil]'"
        ) from exc

    qr = qrcode.QRCode(error_correction=ERROR_CORRECT_H, border=border)
    qr.add_data(payload)
    qr.make(fit=True)

    # modules_count 不含静默区，border 是单边的模块数
    total_modules = qr.modules_count + border * 2
    qr.box_size = max(1, size // total_modules)

    image = qr.make_image(fill_color="black", back_color="white").convert("RGB")

    if image.size == (size, size):
        return image
    if image.width > size:
        # box_size 已经最小为 1 还是超了，说明 size 给得太小，只能缩（并告知调用方）
        return image.resize((size, size), Image.Resampling.NEAREST)

    canvas = Image.new("RGB", (size, size), "white")
    canvas.paste(image, ((size - image.width) // 2, (size - image.height) // 2))
    return canvas


def crop_qr(
    source: ImageSource,
    *,
    index: int = 0,
    payload_filter: str | None = None,
    size: int = 640,
    margin_ratio: float = 0.10,
    regenerate: bool = True,
) -> CropResult:
    """从截图里抠出一张干净的收款码。

    参数
    ----
    index:          图里有多个码时选第几个（按面积从大到小）
    payload_filter: 只要内容包含该子串的码，例如传 ``binance.com``
    regenerate:     解出内容后按原文重绘（默认开）。关掉则做像素级透视裁剪。
    """
    image = load_image(source)
    hits = detect_qrcodes(image)
    warnings: list[str] = []

    if not hits:
        raise QRError(
            "没有在图片里识别到二维码。请确认截图完整、二维码没有被遮挡，"
            "或换一张分辨率更高的图。"
        )

    if payload_filter:
        filtered = [h for h in hits if payload_filter.lower() in h.payload.lower()]
        if not filtered:
            raise QRError(
                f"图片里识别到 {len(hits)} 个二维码，但没有内容包含 {payload_filter!r} 的。"
            )
        hits_for_pick = filtered
    else:
        hits_for_pick = hits

    if len(hits) > 1:
        warnings.append(
            f"图片里有 {len(hits)} 个二维码，已选用第 {index + 1} 个（面积最大优先）。"
        )

    if index >= len(hits_for_pick):
        raise QRError(f"index={index} 越界，图里只识别到 {len(hits_for_pick)} 个二维码。")

    hit = hits_for_pick[index]

    if hit.side < 80:
        warnings.append("二维码在原图里过小，重绘后清晰度可能受影响，建议换高分辨率截图。")

    if regenerate:
        try:
            image_out = render_qr(hit.payload, size=size)
            return CropResult(image_out, hit, True, warnings, hits)
        except QRError as exc:
            warnings.append(f"{exc}；已回退为像素裁剪。")

    cropped = warp_crop(image, hit, size=size, margin_ratio=margin_ratio)
    return CropResult(cropped, hit, False, warnings, hits)


def verify_readable(image: Image.Image, expected_payload: str | None = None) -> bool:
    """回读校验：确认产出的图还能被扫出来（且内容没变）。"""
    hits = detect_qrcodes(image, expected=1)
    if not hits:
        return False
    if expected_payload is None:
        return True
    return any(h.payload == expected_payload for h in hits)
