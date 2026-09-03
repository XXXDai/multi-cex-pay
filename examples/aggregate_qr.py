#!/usr/bin/env python3
"""二维码流水线单独跑：截图 → 抠码 → 拼聚合图 → 回读校验。

这一段完全不碰交易所接口，也不需要任何 API Key，是最容易先验证的一步。

运行：
    .venv/bin/python examples/aggregate_qr.py bn.png okx.png bitget.png
    .venv/bin/python examples/aggregate_qr.py --self-test        # 自己造图，不需要素材

品牌归属默认由二维码内容自动推断（cexpay.qr.guess_brand 认 binance.com /
okx.com / bitget.com 等域名）。推不出来时按 `--exchange` 的顺序回填，
再不行就用 panel-N 占位。也可以显式指定：

    .venv/bin/python examples/aggregate_qr.py binance=bn.png okx=o.png

关于「聚合」的含义，别对外夸大：
二维码内容是各交易所自己的 URL（app.binance.com/uni-qr/... 之类），
没有任何一张码能被三个 App 同时看懂。compose() 做的是视觉聚合：
三个带品牌色标题的格子并排，格间留出 ≥ 码宽 45% 的白边，让手机取景框
一次只框得住一个格子。从相册里识别多码图时，部分 App 会随机取一个码，
所以要引导用户用相机对准目标品牌那一格扫，或者干脆走单码模式。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cexpay.errors import QRError
from cexpay.qr import Panel, compose, crop_qr, guess_brand, render_qr

# --self-test 用的假收款码内容。格式上模仿真实收款码的域名，
# 但里面的 ID 是编的，扫出来不会指向任何真实账号。
SELF_TEST_PAYLOADS = {
    "binance": "https://app.binance.com/uni-qr/cart/EXAMPLE-BINANCE-0001?l=zh-CN",
    "okx": "https://www.okx.com/pay/receive?uid=EXAMPLE-OKX-0002&ccy=USDT",
    "bitget": "https://www.bitget.com/pay/receive?uid=EXAMPLE-BITGET-0003&coin=USDT",
}

# 三家 App 都只认自己的域名，这里用来展示 guess_brand 的自动归属
FALLBACK_ORDER = ("binance", "okx", "bitget")


def make_self_test_screenshots(out_dir: Path) -> list[str]:
    """造几张「手机截图」：二维码 + 周围一堆干扰内容。

    真实素材是用户从 App 里截的图，码只占画面一小块，周围有导航栏、
    文字、边距。所以这里也把码嵌到一张更大的灰底画布里，让 crop_qr
    真的有活干（检测 → 定位 → 按内容重绘），而不是拿到一张纯二维码。
    """
    from PIL import Image, ImageDraw

    out_dir.mkdir(parents=True, exist_ok=True)
    paths: list[str] = []
    for exchange, payload in SELF_TEST_PAYLOADS.items():
        qr = render_qr(payload, size=460)
        shot = Image.new("RGB", (720, 1180), "#EEF1F4")
        draw = ImageDraw.Draw(shot)
        draw.rectangle([0, 0, 720, 120], fill="#20242B")
        draw.text((28, 52), f"{exchange.upper()}  -  Receive USDT", fill="#FFFFFF")
        draw.rounded_rectangle([70, 300, 650, 980], radius=28, fill="#FFFFFF")
        shot.paste(qr, (130, 380))
        draw.text((150, 900), "Scan to pay  (synthetic test image)", fill="#6B7280")
        path = out_dir / f"{exchange}-screenshot.png"
        shot.save(path)
        paths.append(str(path))
        print(f"  已生成测试截图 {path}  ({shot.width}x{shot.height})")
    return paths


def parse_arg(raw: str) -> tuple[str | None, str]:
    """支持 `binance=path.png` 和裸 `path.png` 两种写法。"""
    if "=" in raw:
        name, _, path = raw.partition("=")
        name = name.strip().lower()
        if name and not Path(raw).exists():
            return name, path.strip()
    return None, raw


def main() -> int:
    parser = argparse.ArgumentParser(
        description="从截图抠出收款码并合成聚合收款图",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "sources", nargs="*",
        help="截图路径，可写成 path.png 或 binance=path.png",
    )
    parser.add_argument("--out", default="aggregate.png", help="聚合图输出路径")
    parser.add_argument("--crop-dir", default=None, help="顺便把每个单码另存到该目录")
    parser.add_argument(
        "--layout", default="row", choices=("row", "column", "grid"),
        help="排版方式",
    )
    parser.add_argument("--qr-size", type=int, default=520, help="单个码的边长（像素）")
    parser.add_argument(
        "--gutter-ratio", type=float, default=0.45,
        help="格间留白 / 码宽；低于 0.3 会被警告，别乱调小",
    )
    parser.add_argument("--title", default="扫码支付 · 任选一家", help="顶部标题")
    parser.add_argument(
        "--footnote",
        default="请使用对应交易所 App 扫描该品牌下方的二维码",
        help="底部说明",
    )
    parser.add_argument(
        "--self-test", action="store_true",
        help="不用素材：自己生成三张合成截图再跑一遍完整流程",
    )
    parser.add_argument(
        "--tmp-dir", default="/tmp/cexpay-qr-selftest",
        help="--self-test 生成的截图放哪",
    )
    args = parser.parse_args()

    sources = list(args.sources)
    if args.self_test:
        print("[自测] 生成合成截图（不需要任何交易所凭据）：")
        sources = make_self_test_screenshots(Path(args.tmp_dir)) + sources
    if not sources:
        parser.error("请至少给一张截图，或加 --self-test")

    crop_dir = Path(args.crop_dir) if args.crop_dir else None

    # ---- 第一步：逐张抠码 ----
    print()
    print("[1/3] 识别并抠出每张截图里的收款码")
    panels: list[Panel] = []
    used_names: set[str] = set()
    for index, raw in enumerate(sources):
        forced, path = parse_arg(raw)
        try:
            crop = crop_qr(path, size=args.qr_size)
        except QRError as exc:
            print(f"  [跳过] {path}: {exc}")
            continue

        # 品牌归属：显式指定 > 内容推断 > 位置回填 > 占位
        brand = forced or guess_brand(crop.payload or "")
        if not brand:
            for candidate in FALLBACK_ORDER:
                if candidate not in used_names:
                    brand = candidate
                    print(
                        f"  [提示] {path} 的二维码内容认不出品牌，按顺序归到 {brand}；"
                        f"想指定就写成 {brand}={path}"
                    )
                    break
        brand = brand or f"panel-{index + 1}"
        if brand in used_names:
            brand = f"{brand}-{index + 1}"
        used_names.add(brand)

        print(f"  {path}")
        print(f"    品牌      {brand}")
        print(f"    内容      {crop.payload}")
        print(f"    处理方式  {'按内容重绘（推荐）' if crop.regenerated else '像素级透视裁剪'}")
        print(f"    图中码数  {len(crop.all_hits)}")
        for warning in crop.warnings:
            print(f"    [警告]    {warning}")

        if crop_dir:
            saved = crop.save(crop_dir / f"{brand}.png")
            print(f"    单码已存  {saved}")

        panels.append(
            Panel(
                exchange=brand,
                image=crop.image,
                # payload 一定要带上：compose() 靠它做回读校验，
                # 没有 payload 的格子只会被标成 verified=false。
                payload=crop.payload,
                subtitle="",
            )
        )

    if not panels:
        print("\n没有任何截图里识别出二维码，退出。")
        return 1

    # ---- 第二步：合成 ----
    print()
    print(f"[2/3] 合成聚合图（layout={args.layout}, qr_size={args.qr_size}, "
          f"gutter_ratio={args.gutter_ratio}）")
    try:
        result = compose(
            panels,
            layout=args.layout,
            qr_size=args.qr_size,
            gutter_ratio=args.gutter_ratio,
            title=args.title,
            footnote=args.footnote,
            verify=True,          # 合成后回读，确认每个码还扫得出来
        )
    except QRError as exc:
        print(f"  合成失败：{exc}")
        return 1

    out_path = result.save(args.out)
    print(f"  画布尺寸  {result.image.width}x{result.image.height}")
    print(f"  已写出    {out_path.resolve()}")

    # ---- 第三步：逐个报告校验结果 ----
    print()
    print("[3/3] 回读校验（在合成后的成品图上重新识别，逐格比对内容）")
    for exchange, ok in result.verified.items():
        print(f"  {exchange:<12} verified={'true ' if ok else 'false'}"
              f"  {'合成图里仍能正确扫出' if ok else '合成图里没扫出来 / 内容不符'}")
    print(f"  全部通过  {result.all_verified}")

    if result.warnings:
        print()
        print("警告：")
        for warning in result.warnings:
            print(f"  - {warning}")

    if not result.all_verified:
        print()
        print("有格子没通过回读。可以试：调大 --qr-size、把 --layout 换成 column、"
              "或者放弃聚合图改用单码（每个交易所一张图，收银台按用户选择展示）。")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
