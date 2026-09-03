#!/usr/bin/env python3
"""验证「聚合图上各所只认自己那一格」这件事到底成不成立。

无法真的拿三个 App 去扫，所以把扫码这件事拆成两个可以客观验证的步骤：

  第一步 取景：手机举在聚合图前，取景框里到底进来几个码？
              —— 用一个按真实比例裁出来的窗口模拟，再叠加手持的轻微旋转和
                 摄像头分辨率下采样，然后真的去解码。
  第二步 归属：解出来的内容，某家 App 会不会认？
              —— 各所收款码是自家域名的 URL，App 只处理自己的。
                 这一步用域名判定，和 cexpay.qr.detect.guess_brand 同一套规则。

两步都过 = 该 App 扫该格能识别成付款。任何一步不过 = 不识别。

用法:
    python examples/scan_simulation.py                    # 用内置示例数据
    python examples/scan_simulation.py --layout grid
    python examples/scan_simulation.py --save-frames /tmp/frames
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from PIL import Image

from cexpay.qr import Panel, compose, detect_qrcodes
from cexpay.qr.detect import guess_brand

# 示例收款码内容（假数据，请勿扫描）
DEMO_PAYLOADS = {
    "binance": "https://app.binance.com/uni-qr/EXAMPLE-DO-NOT-SCAN",
    "okx": "https://www.okx.com/pay/receive?uid=100000000000000001",
    "bitget": "https://www.bitget.com/pay/receive?qrAction=pay&uid=1000000165",
}
APPS = ("binance", "okx", "bitget")
APP_NAMES = {"binance": "币安 App", "okx": "OKX App", "bitget": "Bitget App"}

GREEN, RED, DIM, BOLD, RESET = "\033[32m", "\033[31m", "\033[2m", "\033[1m", "\033[0m"


def simulate_camera_frame(
    image: Image.Image,
    center: tuple[int, int],
    frame_side: int,
    *,
    rotation: float = 2.5,
    sensor_px: int = 720,
) -> Image.Image:
    """模拟一次手持取景。

    center      取景框中心（用户对准哪一格）
    frame_side  取景框在原图上覆盖的边长（越小=举得越近）
    rotation    手持的轻微倾斜（度）
    sensor_px   摄像头把这块区域采样成多少像素——这一步会丢细节，是真实约束
    """
    cx, cy = center
    half = frame_side // 2
    # 先取一块比取景框大的区域，旋转后再裁回去，避免旋转产生黑边
    pad = int(frame_side * 0.35)
    box = (cx - half - pad, cy - half - pad, cx + half + pad, cy + half + pad)
    region = image.crop(box)

    if rotation:
        region = region.rotate(rotation, resample=Image.Resampling.BICUBIC, fillcolor="white")

    w, h = region.size
    inner = (w // 2 - half, h // 2 - half, w // 2 + half, h // 2 + half)
    frame = region.crop(inner)

    # 下采样到摄像头分辨率
    return frame.resize((sensor_px, sensor_px), Image.Resampling.LANCZOS)


def app_would_accept(app: str, payload: str) -> bool:
    """某家 App 会不会把这个内容当成自家的收款码。"""
    return guess_brand(payload) == app


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--layout", default="row", choices=("row", "column", "grid"))
    parser.add_argument("--qr-size", type=int, default=520)
    parser.add_argument("--frame-ratio", type=float, default=1.6,
                        help="取景框边长 / 二维码边长。1.6 约等于普通扫码距离")
    parser.add_argument("--rotation", type=float, default=2.5, help="手持倾斜角度")
    parser.add_argument("--sensor-px", type=int, default=720, help="摄像头采样分辨率")
    parser.add_argument("--save-frames", help="把每次模拟取景的画面存到这个目录")
    args = parser.parse_args()

    panels = [Panel(name, payload=payload, subtitle="示例账号")
              for name, payload in DEMO_PAYLOADS.items()]
    result = compose(panels, layout=args.layout, qr_size=args.qr_size)
    canvas = result.image

    print(f"{BOLD}聚合图{RESET}  {canvas.size[0]}x{canvas.size[1]}  排版={args.layout}  "
          f"单码={args.qr_size}px")
    print(f"{DIM}逐格回读校验: " +
          "  ".join(f"{k}={'✓' if v else '✗'}" for k, v in result.verified.items()) + RESET)

    # 重新算出每格二维码在成图里的位置（compose 内部用的是同一套排版逻辑）
    rects = _panel_rects(panels, canvas, args)

    frame_side = int(args.qr_size * args.frame_ratio)
    out_dir = Path(args.save_frames) if args.save_frames else None
    if out_dir:
        out_dir.mkdir(parents=True, exist_ok=True)

    print(f"\n{BOLD}第一步：取景{RESET}  "
          f"（取景框 {frame_side}px = 码宽的 {args.frame_ratio}倍，倾斜 {args.rotation}°，"
          f"采样到 {args.sensor_px}px）")

    frames: dict[str, list[str]] = {}
    for name, (x0, y0, x1, y1) in rects.items():
        center = ((x0 + x1) // 2, (y0 + y1) // 2)
        frame = simulate_camera_frame(canvas, center, frame_side,
                                      rotation=args.rotation, sensor_px=args.sensor_px)
        if out_dir:
            frame.save(out_dir / f"frame-{name}.png")
        found = [h.payload for h in detect_qrcodes(frame)]
        frames[name] = found
        brands = [guess_brand(p) or "未知" for p in found]
        status = f"{GREEN}只框到 1 个{RESET}" if len(found) == 1 else f"{RED}框到 {len(found)} 个{RESET}"
        print(f"  对准 {name:8} → {status}  解出: {', '.join(brands) if brands else '无'}")

    print(f"\n{BOLD}第二步：各所 App 是否认账{RESET}")
    header = "  " + " " * 12 + "".join(f"{APP_NAMES[a]:>12}" for a in APPS)
    print(header)
    print("  " + "-" * (12 + 12 * len(APPS)))
    for name in rects:
        row = f"  对准 {name:8}"
        for app in APPS:
            ok = any(app_would_accept(app, p) for p in frames[name])
            row += f"{(GREEN + '✓ 识别' + RESET) if ok else (DIM + '— 不识别' + RESET):>21}"
        print(row)

    print(f"\n{BOLD}对照：整张图丢给相册识别{RESET}")
    whole = [h.payload for h in detect_qrcodes(canvas)]
    print(f"  一次性解出 {len(whole)} 个码 —— App 通常只取其中一个，取哪个不可控。")
    for app in APPS:
        hits = [p for p in whole if app_would_accept(app, p)]
        print(f"  {APP_NAMES[app]:10} 图中有 {len(hits)} 个它认得的码，"
              f"但{'可能被其它码抢先' if len(whole) > 1 else '唯一'}")

    # 找出"举多远才会同时框进两个完整的码"这个阈值
    print(f"\n{BOLD}串格阈值{RESET}  （二维码被裁掉一部分就解不出来，所以只算完整落入的）")
    center_name = list(rects)[len(rects) // 2]
    cx0, cy0, cx1, cy1 = rects[center_name]
    ccx, ccy = (cx0 + cx1) // 2, (cy0 + cy1) // 2
    threshold = None
    for ratio in [x / 10 for x in range(12, 61, 1)]:
        side = int(args.qr_size * ratio)
        fx0, fy0, fx1, fy1 = ccx - side // 2, ccy - side // 2, ccx + side // 2, ccy + side // 2
        full = [m for m, (a, b, c, d) in rects.items()
                if a >= fx0 and c <= fx1 and b >= fy0 and d <= fy1]
        if len(full) > 1:
            threshold = ratio
            break
    if threshold:
        print(f"  取景框放大到码宽的 {GREEN}{threshold}倍{RESET} 才会同时框进 2 个完整的码。")
        print(f"  {DIM}正常扫码时取景框约为码宽的 1.2~2.5 倍，安全余量约 {threshold / 2.5:.1f}倍。{RESET}")
    else:
        print(f"  {DIM}在 6 倍以内都不会同时框进两个完整的码。{RESET}")

    ok_diag = all(
        len(frames[name]) == 1 and app_would_accept(name, frames[name][0])
        for name in rects
    )
    cross = [
        (name, app) for name in rects for app in APPS
        if app != name and any(app_would_accept(app, p) for p in frames[name])
    ]
    print(f"\n{BOLD}结论{RESET}")
    print(f"  对准某一格时只有该格进入取景框: {GREEN + '成立' + RESET if ok_diag else RED + '不成立' + RESET}")
    print(f"  出现跨所误识别: {RED + str(cross) + RESET if cross else GREEN + '无' + RESET}")
    print(f"  {DIM}→ 相机对准单格可靠；相册识别多码图不可靠，别引导用户走相册。{RESET}")
    return 0 if ok_diag and not cross else 1


def _panel_rects(panels, canvas, args) -> dict[str, tuple[int, int, int, int]]:
    """复算每格二维码的矩形，参数与 compose 的默认值保持一致。"""
    from cexpay.qr.compose import _grid_shape

    qr_size = args.qr_size
    padding = 64
    gutter = int(qr_size * 0.45)
    header_h = int(qr_size * 0.15)
    sub_h = int(qr_size * 0.10)
    cell_h = header_h + qr_size + sub_h
    cols, _rows = _grid_shape(len(panels), args.layout)

    # 标题占的高度 = 画布高 - 内容高 - 底部小字，直接反推更稳
    content_h = _rows_of(len(panels), cols) * cell_h + (_rows_of(len(panels), cols) - 1) * gutter
    top = padding + (canvas.height - padding * 2 - content_h) // 3  # 标题在上、小字在下

    rects = {}
    for index, panel in enumerate(panels):
        col, row = index % cols, index // cols
        x0 = padding + col * (qr_size + gutter)
        y0 = top + row * (cell_h + gutter) + header_h + 18
        rects[panel.exchange] = (x0, y0, x0 + qr_size, y0 + qr_size)
    return rects


def _rows_of(count: int, cols: int) -> int:
    return (count + cols - 1) // cols


if __name__ == "__main__":
    raise SystemExit(main())
