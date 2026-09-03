"""把多个交易所的收款码拼成一张聚合收款图。

二维码本身没法"通用"。
Binance 的码是 ``https://app.binance.com/uni-qr/...``，
Bitget 的是 ``https://www.bitget.com/pay/receive?...``，
各家 App 只认自己的域名。所以这里做的是视觉聚合：
一张图上并排放三个码 + 醒目的品牌标识，用户用哪家 App 就扫哪一格，
而不是（也不可能是）一个能被三家同时解析的"万能码"。

为了让"扫哪一格"可靠，排版上做了几件事：
  1. 每格之间留出 ≥ 码宽 45% 的白色间距，手机取景框自然只框得住一个码；
  2. 每个码正上方压一条品牌色标题栏，肉眼一眼分辨；
  3. 合成后回读校验，确认三个码都还能被扫出来、内容没变。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw, ImageFont

from ..errors import QRError
from .detect import ImageSource, detect_qrcodes, load_image, render_qr

# 常见的中文字体位置，找不到就退回 PIL 内置点阵字体
CJK_FONT_CANDIDATES = (
    "/System/Library/Fonts/PingFang.ttc",
    "/System/Library/Fonts/STHeiti Medium.ttc",
    "/System/Library/Fonts/Hiragino Sans GB.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJKsc-Regular.otf",
    "/usr/share/fonts/truetype/wqy/wqy-microhei.ttc",
    "C:/Windows/Fonts/msyh.ttc",
    "C:/Windows/Fonts/simhei.ttf",
)

LATIN_FONT_CANDIDATES = (
    "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
    "/System/Library/Fonts/Helvetica.ttc",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "C:/Windows/Fonts/arialbd.ttf",
)

BRAND_STYLE: dict[str, dict[str, str]] = {
    "binance": {"title": "Binance Pay", "bg": "#F0B90B", "fg": "#1E2026"},
    "okx":     {"title": "OKX",         "bg": "#000000", "fg": "#FFFFFF"},
    "bitget":  {"title": "Bitget",      "bg": "#00C4CC", "fg": "#04202B"},
}

DEFAULT_STYLE = {"title": "收款码", "bg": "#4B5563", "fg": "#FFFFFF"}


def _load_font(size: int, *, bold: bool = False) -> ImageFont.ImageFont:
    """优先中文字体，其次拉丁字体，最后退回内置字体。"""
    for path in CJK_FONT_CANDIDATES + LATIN_FONT_CANDIDATES:
        if Path(path).exists():
            try:
                return ImageFont.truetype(path, size)
            except OSError:
                continue
    return ImageFont.load_default()


def _text_size(draw: ImageDraw.ImageDraw, text: str, font) -> tuple[int, int]:
    if not text:
        return 0, 0
    left, top, right, bottom = draw.textbbox((0, 0), text, font=font)
    return right - left, bottom - top


@dataclass
class Panel:
    """聚合图里的一格。"""

    exchange: str
    image: Image.Image | None = None
    payload: str | None = None
    title: str | None = None
    subtitle: str = ""          # 例如 "UID 1000000165"

    def resolve_image(self, size: int) -> Image.Image:
        """拿到该格要画的二维码。

        只要知道内容就按内容重绘，而不是把已有位图重采样到目标尺寸。
        重采样会让模块边界错位，扫不出来（见 render_qr 的说明）。
        尺寸刚好相等时直接用原图，避免无谓的重绘。
        """
        if self.image is not None and self.image.size == (size, size):
            return self.image.convert("RGB")
        if self.payload:
            return render_qr(self.payload, size=size)
        if self.image is not None:
            # 内容没解出来，只能best-effort 重采样；compose 的回读校验会如实报告结果
            return self.image.convert("RGB").resize((size, size), Image.Resampling.LANCZOS)
        raise QRError(f"{self.exchange} 这一格既没有图片也没有二维码内容")

    @property
    def style(self) -> dict[str, str]:
        return BRAND_STYLE.get(self.exchange.lower(), DEFAULT_STYLE)

    @property
    def display_title(self) -> str:
        return self.title or self.style["title"]

    @classmethod
    def from_source(
        cls,
        exchange: str,
        source: ImageSource,
        *,
        subtitle: str = "",
        title: str | None = None,
    ) -> Panel:
        """从图片文件/字节创建一格，并顺便解出它的内容用于后续校验。"""
        image = load_image(source)
        hits = detect_qrcodes(image)
        payload = hits[0].payload if hits else None
        return cls(
            exchange=exchange,
            image=image,
            payload=payload,
            title=title,
            subtitle=subtitle,
        )


@dataclass
class ComposeResult:
    image: Image.Image
    layout: str
    verified: dict[str, bool] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)

    @property
    def all_verified(self) -> bool:
        return bool(self.verified) and all(self.verified.values())

    def save(self, path: str | Path) -> Path:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self.image.save(path)
        return path

    def to_dict(self) -> dict[str, Any]:
        return {
            "layout": self.layout,
            "size": list(self.image.size),
            "verified": self.verified,
            "all_verified": self.all_verified,
            "warnings": self.warnings,
        }


def _grid_shape(count: int, layout: str) -> tuple[int, int]:
    """返回 (列数, 行数)。"""
    if count <= 0:
        raise QRError("至少需要一个收款码")
    if layout == "column":
        return 1, count
    if layout == "row":
        return count, 1
    if layout == "grid":
        if count <= 2:
            return count, 1
        if count <= 4:
            return 2, (count + 1) // 2
        cols = 3
        return cols, (count + cols - 1) // cols
    raise QRError(f"未知排版：{layout}（可选 row / column / grid）")


def compose(
    panels: Sequence[Panel],
    *,
    layout: str = "row",
    qr_size: int = 520,
    gutter_ratio: float = 0.45,
    padding: int = 64,
    title: str = "扫码支付 · 任选一家",
    footnote: str = "请使用对应交易所 App 扫描该品牌下方的二维码",
    background: str = "#FFFFFF",
    verify: bool = True,
) -> ComposeResult:
    """把若干收款码排成一张图。

    参数
    ----
    layout:        row（横排）/ column（竖排）/ grid（网格）
    qr_size:       每个二维码的边长（像素）
    gutter_ratio:  格与格之间的留白，按 qr_size 的比例算。
                   ≥0.45 时手机取景框基本只会框住一个码，别调太小。
    verify:        合成后回读校验每个码是否仍可扫（默认开）
    """
    if not panels:
        raise QRError("没有可用的收款码，请先在后台配置各交易所的二维码")

    warnings: list[str] = []
    if gutter_ratio < 0.3 and len(panels) > 1:
        warnings.append(
            f"gutter_ratio={gutter_ratio} 偏小，多个二维码挨太近时扫码可能串码，建议 ≥0.45。"
        )

    cols, rows = _grid_shape(len(panels), layout)
    gutter = int(qr_size * gutter_ratio)

    font_title = _load_font(max(28, qr_size // 13), bold=True)
    font_brand = _load_font(max(24, qr_size // 15), bold=True)
    font_sub = _load_font(max(18, qr_size // 22))
    font_foot = _load_font(max(18, qr_size // 24))

    probe = ImageDraw.Draw(Image.new("RGB", (10, 10)))
    header_h = int(qr_size * 0.15)
    sub_h = int(qr_size * 0.10)
    cell_w = qr_size
    cell_h = header_h + qr_size + sub_h

    title_h = (_text_size(probe, title, font_title)[1] + int(padding * 0.9)) if title else 0
    foot_h = (_text_size(probe, footnote, font_foot)[1] + int(padding * 0.7)) if footnote else 0

    canvas_w = padding * 2 + cols * cell_w + (cols - 1) * gutter
    canvas_h = padding * 2 + title_h + rows * cell_h + (rows - 1) * gutter + foot_h

    canvas = Image.new("RGB", (canvas_w, canvas_h), background)
    draw = ImageDraw.Draw(canvas)
    # 记录每格二维码在成图里的确切矩形，回读校验时按格裁出来单独解
    panel_rects: list[tuple[Panel, tuple[int, int, int, int]]] = []

    y_cursor = padding
    if title:
        tw, th = _text_size(draw, title, font_title)
        draw.text(((canvas_w - tw) // 2, y_cursor), title, font=font_title, fill="#111827")
        y_cursor += th + int(padding * 0.9)

    for index, panel in enumerate(panels):
        col = index % cols
        row = index // cols
        x0 = padding + col * (cell_w + gutter)
        y0 = y_cursor + row * (cell_h + gutter)
        style = panel.style

        # 品牌色标题栏
        draw.rounded_rectangle(
            [x0, y0, x0 + cell_w, y0 + header_h + 18],
            radius=16,
            fill=style["bg"],
        )
        draw.rectangle([x0, y0 + header_h - 6, x0 + cell_w, y0 + header_h + 18], fill=style["bg"])
        brand_text = panel.display_title
        bw, bh = _text_size(draw, brand_text, font_brand)
        draw.text(
            (x0 + (cell_w - bw) // 2, y0 + (header_h - bh) // 2),
            brand_text,
            font=font_brand,
            fill=style["fg"],
        )

        # 二维码（白底，四周本身自带静默区）
        qr_img = panel.resolve_image(qr_size)
        qr_y = y0 + header_h + 18
        canvas.paste(qr_img, (x0, qr_y))
        panel_rects.append((panel, (x0, qr_y, x0 + qr_size, qr_y + qr_size)))
        draw.rectangle(
            [x0, qr_y, x0 + qr_size - 1, qr_y + qr_size - 1],
            outline="#E5E7EB",
            width=2,
        )

        # 副标题（UID / 备注）
        if panel.subtitle:
            sw, sh = _text_size(draw, panel.subtitle, font_sub)
            draw.text(
                (x0 + (cell_w - sw) // 2, qr_y + qr_size + (sub_h - sh) // 2),
                panel.subtitle,
                font=font_sub,
                fill="#6B7280",
            )

    if footnote:
        fw, fh = _text_size(draw, footnote, font_foot)
        draw.text(
            ((canvas_w - fw) // 2, canvas_h - padding - fh),
            footnote,
            font=font_foot,
            fill="#9CA3AF",
        )

    verified: dict[str, bool] = {}
    if verify:
        # 逐格裁出来单独解，而不是把整张大图丢给多码识别器。
        #
        # OpenCV 的 detectAndDecodeMulti 在超大或超长画布上不可靠：
        # column 排版下画布能到 588x2417，同一份代码在 macOS 上能解出三个码、
        # 在 Linux 上就漏一个。而用户实际的扫码方式恰恰是"对准一格去扫"，
        # 所以按格校验既更稳定，也更贴近真实使用场景。
        for panel, rect in panel_rects:
            if not panel.payload:
                verified[panel.exchange] = False
                warnings.append(
                    f"{panel.exchange} 的二维码内容未知（原图没解出来），无法回读校验。"
                )
                continue
            cell = canvas.crop(rect)
            ok = any(hit.payload == panel.payload for hit in detect_qrcodes(cell, expected=1))
            verified[panel.exchange] = ok
            if not ok:
                warnings.append(
                    f"{panel.exchange} 的二维码在聚合图里回读失败，"
                    f"建议调大 qr_size 或改用单码模式。"
                )

        # 再整张扫一遍，纯粹为了如实告知"从相册一次性识别能不能拿到全部"。
        # 扫不全不代表成图有问题，但确实说明不该引导用户走相册识别。
        if len(panel_rects) > 1:
            whole = {hit.payload for hit in detect_qrcodes(canvas, expected=len(panel_rects))}
            missed = [p.exchange for p, _ in panel_rects if p.payload and p.payload not in whole]
            if missed:
                warnings.append(
                    "整图一次性识别只认出了部分二维码（"
                    + "、".join(missed)
                    + " 未被认出）。所以不要让用户从相册识别，引导他用相机对准单格扫。"
                )

    if len(panels) > 1:
        warnings.append(
            "聚合图含多个二维码：从相册识别时部分 App 只会取其中一个，"
            "建议引导用户用相机对准所需品牌那一格扫描，或改用单码模式。"
        )

    return ComposeResult(canvas, layout, verified, warnings)


def compose_from_paths(
    sources: dict[str, ImageSource],
    *,
    subtitles: dict[str, str] | None = None,
    **kwargs: Any,
) -> ComposeResult:
    """便捷入口：``{"binance": "bn.png", "okx": "okx.png"}`` → 一张聚合图。"""
    subtitles = subtitles or {}
    panels = [
        Panel.from_source(exchange, source, subtitle=subtitles.get(exchange, ""))
        for exchange, source in sources.items()
    ]
    return compose(panels, **kwargs)
