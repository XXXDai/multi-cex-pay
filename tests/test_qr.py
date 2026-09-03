"""二维码识别 / 裁剪 / 聚合。"""

import io

import pytest
from PIL import Image

from cexpay.errors import QRError
from cexpay.qr import Panel, compose, crop_qr, detect_qrcodes, render_qr
from cexpay.qr.detect import guess_brand, verify_readable

BINANCE_URL = "https://app.binance.com/uni-qr/DEMO1234"
OKX_URL = "https://www.okx.com/pay/receive?uid=100000000000000001"
BITGET_URL = "https://www.bitget.com/pay/receive?qrAction=pay&uid=1000000165"


def screenshot(payload: str, *, size=360, canvas=(720, 1200), pos=None) -> Image.Image:
    """伪造一张"App 收款页截图"：灰底 + 居中的二维码 + 一些干扰色块。"""
    page = Image.new("RGB", canvas, "#f2f2f2")
    qr = render_qr(payload, size=size)
    x = pos[0] if pos else (canvas[0] - size) // 2
    y = pos[1] if pos else (canvas[1] - size) // 3
    page.paste(qr, (x, y))
    # 顶部导航栏 / 底部按钮，模拟真实 UI
    for box, color in (
        ((0, 0, canvas[0], 80), "#1b1b1b"),
        ((40, canvas[1] - 140, canvas[0] - 40, canvas[1] - 70), "#3355ff"),
    ):
        page.paste(Image.new("RGB", (box[2] - box[0], box[3] - box[1]), color), box[:2])
    return page


def to_bytes(image: Image.Image) -> bytes:
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


# ---------------------------------------------------------------- 识别
def test_detect_finds_qr_in_screenshot():
    hits = detect_qrcodes(screenshot(BINANCE_URL))
    assert len(hits) == 1
    assert hits[0].payload == BINANCE_URL


def test_detect_accepts_bytes_and_pil():
    page = screenshot(OKX_URL)
    assert detect_qrcodes(to_bytes(page))[0].payload == OKX_URL
    assert detect_qrcodes(page)[0].payload == OKX_URL


def test_detect_empty_image_returns_nothing():
    assert detect_qrcodes(Image.new("RGB", (400, 400), "white")) == []


@pytest.mark.parametrize(
    "payload,brand",
    [
        (BINANCE_URL, "binance"),
        (OKX_URL, "okx"),
        (BITGET_URL, "bitget"),
        ("0x0000000000000000000000000000000000000001", None),
        ("", None),
    ],
)
def test_guess_brand(payload, brand):
    assert guess_brand(payload) == brand


# ---------------------------------------------------------------- 裁剪
def test_crop_regenerates_clean_qr():
    result = crop_qr(screenshot(BINANCE_URL), size=520)
    assert result.payload == BINANCE_URL
    assert result.regenerated is True
    assert result.image.size == (520, 520)
    assert verify_readable(result.image, BINANCE_URL)


def test_crop_pixel_mode_still_readable():
    result = crop_qr(screenshot(BITGET_URL), size=520, regenerate=False)
    assert result.regenerated is False
    assert verify_readable(result.image, BITGET_URL)


def test_crop_without_qr_raises():
    with pytest.raises(QRError):
        crop_qr(Image.new("RGB", (300, 300), "white"))


def test_crop_picks_by_payload_filter():
    """一张图里有两个码时，可以按内容挑。"""
    page = Image.new("RGB", (1400, 700), "white")
    page.paste(render_qr(BINANCE_URL, size=400), (60, 150))
    page.paste(render_qr(BITGET_URL, size=400), (900, 150))

    result = crop_qr(page, payload_filter="bitget.com")
    assert result.payload == BITGET_URL
    assert "2 个二维码" in " ".join(result.warnings)


def test_crop_filter_no_match_raises():
    with pytest.raises(QRError):
        crop_qr(screenshot(BINANCE_URL), payload_filter="okx.com")


def test_crop_index_out_of_range_raises():
    with pytest.raises(QRError):
        crop_qr(screenshot(BINANCE_URL), index=5)


# ---------------------------------------------------------------- 聚合
@pytest.mark.parametrize("layout", ["row", "column", "grid"])
def test_compose_all_three_stay_scannable(layout):
    panels = [
        Panel("binance", payload=BINANCE_URL, subtitle="Pay ID 123456"),
        Panel("okx", payload=OKX_URL, subtitle="UID 1000****0165"),
        Panel("bitget", payload=BITGET_URL, subtitle="UID 1000****0165"),
    ]
    result = compose(panels, layout=layout, qr_size=460)
    assert result.all_verified, result.warnings
    assert set(result.verified) == {"binance", "okx", "bitget"}


def test_compose_from_screenshots_end_to_end():
    """完整链路：三张截图 → 自动裁剪 → 聚合 → 三个码都还能扫。"""
    panels = []
    for exchange, url in (
        ("binance", BINANCE_URL), ("okx", OKX_URL), ("bitget", BITGET_URL)
    ):
        cropped = crop_qr(screenshot(url), size=460)
        panels.append(Panel(exchange, image=cropped.image, payload=cropped.payload))
    result = compose(panels, qr_size=460)
    assert result.all_verified, result.warnings


def test_compose_warns_about_multiple_codes():
    panels = [
        Panel("binance", payload=BINANCE_URL),
        Panel("okx", payload=OKX_URL),
    ]
    result = compose(panels)
    assert any("多个二维码" in w for w in result.warnings)


def test_compose_single_panel_has_no_multi_warning():
    result = compose([Panel("binance", payload=BINANCE_URL)])
    assert not any("多个二维码" in w for w in result.warnings)
    assert result.all_verified


def test_compose_warns_on_tight_gutter():
    panels = [Panel("binance", payload=BINANCE_URL), Panel("okx", payload=OKX_URL)]
    result = compose(panels, gutter_ratio=0.15)
    assert any("gutter_ratio" in w for w in result.warnings)


def test_compose_rejects_empty():
    with pytest.raises(QRError):
        compose([])


def test_compose_rejects_unknown_layout():
    with pytest.raises(QRError):
        compose([Panel("binance", payload=BINANCE_URL)], layout="spiral")


def test_panel_without_image_or_payload_raises():
    with pytest.raises(QRError):
        compose([Panel("binance")])


def test_panel_from_source_extracts_payload():
    panel = Panel.from_source("binance", to_bytes(screenshot(BINANCE_URL)))
    assert panel.payload == BINANCE_URL


# ---------------------------------------------------------------- 回归
def test_detection_accumulates_across_preprocessing_variants():
    """回归：三个码密度不同时，不能因为原图先解出两个就提前收手漏掉第三个。

    历史上 _detect_opencv 一发现任何结果就 break，导致更密的那个码被静默漏掉，
    compose 的回读校验会给出 verified=False。
    """
    dense = "https://www.bitget.com/pay/receive?qrAction=pay&uid=0000000000&extra=padding-to-make-this-denser"
    panels = [
        Panel("binance", payload="https://app.binance.com/uni-qr/S"),
        Panel("okx", payload="https://www.okx.com/pay/receive?uid=1"),
        Panel("bitget", payload=dense),
    ]
    result = compose(panels, layout="row", qr_size=440)
    assert result.all_verified, result.verified


@pytest.mark.parametrize("qr_size", [400, 460, 520, 600])
def test_compose_verifies_at_common_sizes(qr_size):
    panels = [
        Panel("binance", payload=BINANCE_URL),
        Panel("okx", payload=OKX_URL),
        Panel("bitget", payload=BITGET_URL),
    ]
    assert compose(panels, layout="row", qr_size=qr_size).all_verified


def test_detects_inverted_dark_mode_screenshot():
    """深色模式截图里二维码是反相的，也要能识别出来。"""
    from PIL import ImageOps

    inverted = ImageOps.invert(render_qr(BINANCE_URL, size=420))
    hits = detect_qrcodes(inverted)
    assert [h.payload for h in hits] == [BINANCE_URL]


def test_expected_hint_does_not_lose_codes():
    page = Image.new("RGB", (1500, 700), "white")
    page.paste(render_qr(BINANCE_URL, size=420), (60, 140))
    page.paste(render_qr(BITGET_URL, size=420), (1000, 140))
    assert len(detect_qrcodes(page, expected=2)) == 2
    assert len(detect_qrcodes(page)) == 2


def test_render_qr_uses_integer_module_size():
    """回归：重绘不能把二维码缩放到任意像素尺寸。

    早期实现是"先按 box_size=10 生成、再 resize 到 size"，模块边界会落在像素中间
    （一部分模块 8px、一部分 9px），解码成功率随尺寸抖动——440/520 正常，
    偏偏 460 解不出来。现在改成"按整数模块尺寸生成 + 居中补白"。
    """
    import qrcode
    from qrcode.constants import ERROR_CORRECT_H

    payload = "https://www.bitget.com/pay/receive?qrAction=pay&uid=1000000165"
    probe = qrcode.QRCode(error_correction=ERROR_CORRECT_H, border=4)
    probe.add_data(payload)
    probe.make(fit=True)
    total_modules = probe.modules_count + 8

    for size in (400, 440, 460, 520, 640):
        image = render_qr(payload, size=size)
        assert image.size == (size, size)
        assert verify_readable(image, payload), f"size={size} 解不出来"
        # 码面必须是模块数的整数倍（其余是补白），否则模块边界就没对齐
        box = size // total_modules
        assert box * total_modules <= size


@pytest.mark.parametrize("size", [220, 300, 380, 460, 540, 620, 700, 780])
def test_compose_verifies_across_the_size_range(size):
    """不同长度的 payload（=不同模块密度）在各尺寸下都要能回读。"""
    panels = [
        Panel("binance", payload="https://app.binance.com/uni-qr/DEMO1234"),
        Panel("okx", payload="https://www.okx.com/pay/receive?uid=100000000000000001"),
        Panel("bitget", payload=BITGET_URL),
    ]
    result = compose(panels, layout="row", qr_size=size)
    assert result.all_verified, f"size={size}: {result.verified}"


def test_panel_regenerates_rather_than_resampling():
    """给了 payload 时，即使 Panel 里存的是别的尺寸的位图，也应按内容重绘。"""
    small = render_qr(BINANCE_URL, size=240)
    panel = Panel("binance", image=small, payload=BINANCE_URL)
    out = panel.resolve_image(560)
    assert out.size == (560, 560)
    assert verify_readable(out, BINANCE_URL)


def test_panel_without_payload_falls_back_to_resample():
    """解不出内容时只能重采样，但不能崩。"""
    panel = Panel("binance", image=render_qr(BINANCE_URL, size=400), payload=None)
    assert panel.resolve_image(300).size == (300, 300)


@pytest.mark.parametrize("layout", ["row", "column", "grid"])
@pytest.mark.parametrize("qr_size", [240, 340, 460, 580])
def test_compose_verifies_every_layout_and_size(layout, qr_size):
    """回归：column 排版会产出很长的画布（如 588x2417），
    OpenCV 的多码识别在这种画布上跨平台表现不一致——macOS 能解出三个、
    Linux 漏一个。校验改成逐格裁剪后单独解，才是稳定且贴近真实扫码方式的做法。
    """
    panels = [
        Panel("binance", payload="https://app.binance.com/uni-qr/DEMO1234"),
        Panel("okx", payload="https://www.okx.com/pay/receive?uid=100000000000000001"),
        Panel("bitget", payload=BITGET_URL),
    ]
    result = compose(panels, layout=layout, qr_size=qr_size)
    assert result.all_verified, f"{layout}/{qr_size}: {result.verified} {result.warnings}"


# ---------------------------------------------------------------- 扫码隔离
def _camera_frame(canvas, center, side, *, rotation=2.5, sensor_px=720):
    """模拟一次手持取景：裁一块区域、轻微倾斜、再降到摄像头分辨率。"""
    cx, cy = center
    half = side // 2
    pad = int(side * 0.35)
    region = canvas.crop((cx - half - pad, cy - half - pad, cx + half + pad, cy + half + pad))
    if rotation:
        region = region.rotate(rotation, resample=Image.Resampling.BICUBIC, fillcolor="white")
    w, h = region.size
    frame = region.crop((w // 2 - half, h // 2 - half, w // 2 + half, h // 2 + half))
    return frame.resize((sensor_px, sensor_px), Image.Resampling.LANCZOS)


def _row_panel_rects(canvas, qr_size, count):
    """复算横排时每格二维码的矩形（参数与 compose 的默认值一致）。"""
    gutter = int(qr_size * 0.45)
    header_h = int(qr_size * 0.15)
    cell_h = header_h + qr_size + int(qr_size * 0.10)
    top = 64 + (canvas.height - 128 - cell_h) // 3
    return [
        (64 + i * (qr_size + gutter), top + header_h + 18,
         64 + i * (qr_size + gutter) + qr_size, top + header_h + 18 + qr_size)
        for i in range(count)
    ]


@pytest.mark.parametrize("frame_ratio", [1.2, 1.6, 2.0, 2.5])
def test_camera_pointed_at_one_panel_frames_only_that_panel(frame_ratio):
    """聚合图的核心承诺：相机对准某一格时，只有那一格进得了取景框。

    这里分两级断言，因为两件事的确定性完全不同：

      几何（强断言）—— 目标格完整落入取景框，邻格不完整落入。这是纯算术，
                      跨平台完全确定。**被裁掉一部分的二维码在物理上就解不出来**，
                      所以这一条成立就等于"别家的码不可能被扫到"。
      解码（弱断言）—— 如果解码器真解出了东西，那必须是目标格的内容。
                      不强求目标格一定能解出：合成的取景画面叠加了旋转和
                      下采样，不同 OpenCV 版本的解码能力差异很大（macOS 能解、
                      Linux 解不出是实测过的），而真实手机摄像头远好于这个模拟。
                      "码本身清不清晰"由 compose 的逐格回读校验负责。
    """
    payloads = {
        "binance": BINANCE_URL,
        "okx": "https://www.okx.com/pay/receive?uid=100000000000000001",
        "bitget": BITGET_URL,
    }
    qr_size = 520
    panels = [Panel(name, payload=p) for name, p in payloads.items()]
    result = compose(panels, layout="row", qr_size=qr_size)
    assert result.all_verified, result.warnings

    canvas = result.image
    rects = _row_panel_rects(canvas, qr_size, len(panels))
    side = int(qr_size * frame_ratio)
    names = list(payloads)

    for index, name in enumerate(names):
        x0, y0, x1, y1 = rects[index]
        cx, cy = (x0 + x1) // 2, (y0 + y1) // 2
        fx0, fy0 = cx - side // 2, cy - side // 2
        fx1, fy1 = cx + side // 2, cy + side // 2

        # --- 几何：目标格完整在内，其余格不完整 ---
        fully_inside = [
            names[j] for j, (a, b, c, d) in enumerate(rects)
            if a >= fx0 and c <= fx1 and b >= fy0 and d <= fy1
        ]
        assert fully_inside == [name], (
            f"对准 {name}（ratio={frame_ratio}）时完整落入取景框的是 {fully_inside}，"
            f"应该只有 {name}"
        )

        # --- 解码：解出来的只能是自己 ---
        frame = _camera_frame(canvas, (cx, cy), side)
        found = {h.payload for h in detect_qrcodes(frame)}
        foreign = found & {p for n, p in payloads.items() if n != name}
        assert not foreign, (
            f"对准 {name}（ratio={frame_ratio}）却解出了别家的码: {foreign}"
        )


def test_camera_simulation_can_decode_the_targeted_panel():
    """温和条件下（不旋转、采样充足）目标格必须能解出来——
    否则上面那个测试的"解码"部分就是空转，永远不会失败。"""
    payloads = {
        "binance": BINANCE_URL,
        "okx": "https://www.okx.com/pay/receive?uid=100000000000000001",
        "bitget": BITGET_URL,
    }
    qr_size = 520
    panels = [Panel(name, payload=p) for name, p in payloads.items()]
    canvas = compose(panels, layout="row", qr_size=qr_size).image
    rects = _row_panel_rects(canvas, qr_size, len(panels))

    for index, (name, payload) in enumerate(payloads.items()):
        x0, y0, x1, y1 = rects[index]
        cx, cy = (x0 + x1) // 2, (y0 + y1) // 2
        # 不旋转、采样分辨率 = 取景框边长（等于不重采样）：
        # 只考察"取景范围对不对"，把缩放/旋转带来的解码损失完全排除掉
        side = int(qr_size * 1.3)
        frame = _camera_frame(canvas, (cx, cy), side, rotation=0, sensor_px=side)
        found = {h.payload for h in detect_qrcodes(frame)}
        assert payload in found, f"对准 {name} 却没解出它自己"
        assert found == {payload}, f"对准 {name} 时还解出了别的: {found - {payload}}"


def test_each_exchange_only_claims_its_own_payload():
    """域名归属判定：各所只认自己的收款链接，不会把别家的当成自家的。"""
    payloads = {
        "binance": BINANCE_URL,
        "okx": "https://www.okx.com/pay/receive?uid=100000000000000001",
        "bitget": BITGET_URL,
    }
    for app in payloads:
        for owner, payload in payloads.items():
            assert (guess_brand(payload) == app) is (owner == app), (
                f"{app} 对 {owner} 的码判定错误"
            )
