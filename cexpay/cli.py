"""命令行工具。

    cexpay serve                          启动服务
    cexpay creds set binance --api-key .. 配置凭据（secret 走交互输入，不留在 history 里）
    cexpay creds test                     连通性 + 只读权限自检
    cexpay creds list                     查看已配置的凭据（脱敏）
    cexpay qr crop shot.png -e binance    从截图里抠出收款码
    cexpay qr compose -o all.png          生成聚合收款图
    cexpay qr scan shot.png               只识别，打印二维码内容
    cexpay order create 9.9               下一笔测试订单
    cexpay order check <order_id>         立刻核销一次
    cexpay tx --minutes 60                查看最近进账
"""

from __future__ import annotations

import argparse
import getpass
import json
import sys
from pathlib import Path
from typing import Any

from . import __version__
from .config import CREDENTIAL_FIELDS, SUPPORTED_EXCHANGES, get_credential_store, get_settings
from .errors import CexPayError
from .logging_conf import setup_logging

GREEN, RED, YELLOW, DIM, RESET = "\033[32m", "\033[31m", "\033[33m", "\033[2m", "\033[0m"


def _print_json(data: Any) -> None:
    print(json.dumps(data, ensure_ascii=False, indent=2))


def _gateway():
    from .gateway import PaymentGateway

    return PaymentGateway()


# --------------------------------------------------------------------------
# serve
# --------------------------------------------------------------------------
def cmd_serve(args: argparse.Namespace) -> int:
    import uvicorn

    settings = get_settings()
    host = args.host or settings.host
    port = args.port or settings.port

    if not settings.admin_token:
        print(
            f"{YELLOW}提示：未设置 CEXPAY_ADMIN_TOKEN，/admin 后台接口会被禁用。{RESET}",
            file=sys.stderr,
        )
    print(f"收银台  http://{host}:{port}/")
    print(f"后台    http://{host}:{port}/admin")
    print(f"接口文档 http://{host}:{port}/docs")

    uvicorn.run(
        "cexpay.server:get_app",
        factory=True,
        host=host,
        port=port,
        reload=args.reload,
        log_level=args.log_level,
    )
    return 0


# --------------------------------------------------------------------------
# creds
# --------------------------------------------------------------------------
def cmd_creds_list(_: argparse.Namespace) -> int:
    store = get_credential_store(refresh=True)
    for name, cred in store.load(refresh=True).items():
        mark = f"{GREEN}✓{RESET}" if cred.is_complete() and cred.enabled else f"{RED}✗{RESET}"
        state = "" if cred.enabled else f" {DIM}(已停用){RESET}"
        print(f"{mark} {name:<8}{state}")
        if cred.is_complete():
            redacted = cred.to_dict(redacted=True)
            print(f"    api_key    {redacted['api_key']}")
            print(f"    api_secret {redacted['api_secret']}")
            if "passphrase" in CREDENTIAL_FIELDS[name]:
                print(f"    passphrase {redacted['passphrase']}")
            if cred.account_label:
                print(f"    收款账号   {cred.account_label}")
        else:
            print(f"    {DIM}缺少：{', '.join(cred.missing_fields())}{RESET}")
    settings = get_settings()
    print(f"\n配置文件：{settings.credentials_path}")
    print(f"落盘加密：{'开启' if settings.master_key else '未开启（建议设置 CEXPAY_MASTER_KEY）'}")
    return 0


def cmd_creds_set(args: argparse.Namespace) -> int:
    exchange = args.exchange.lower()
    if exchange not in SUPPORTED_EXCHANGES:
        print(f"{RED}不支持的交易所：{exchange}{RESET}", file=sys.stderr)
        return 2

    fields = {}
    if args.api_key:
        fields["api_key"] = args.api_key
    if args.account_label:
        fields["account_label"] = args.account_label
    if args.enable:
        fields["enabled"] = True
    if args.disable:
        fields["enabled"] = False

    required = CREDENTIAL_FIELDS[exchange]
    # secret / passphrase 一律交互输入，避免落进 shell history
    if "api_key" in required and not args.api_key and not args.only_secret:
        fields["api_key"] = input(f"{exchange} API Key: ").strip()
    if "api_secret" in required and not args.only_key:
        secret = getpass.getpass(f"{exchange} API Secret（输入不回显，留空跳过）: ").strip()
        if secret:
            fields["api_secret"] = secret
    if "passphrase" in required and not args.only_key:
        phrase = getpass.getpass(f"{exchange} Passphrase（留空跳过）: ").strip()
        if phrase:
            fields["passphrase"] = phrase

    cred = get_credential_store().save(exchange, **fields)
    if cred.is_complete():
        status = f"{GREEN}完整{RESET}"
    else:
        status = f"{YELLOW}缺少 {', '.join(cred.missing_fields())}{RESET}"
    print(f"已保存 {exchange}：{status}")
    print(f"{DIM}下一步：cexpay creds test -e {exchange}{RESET}")
    return 0


def cmd_creds_test(args: argparse.Namespace) -> int:
    gateway = _gateway()
    reports = gateway.check_permissions([args.exchange] if args.exchange else None)
    if not reports:
        print(f"{YELLOW}还没有配置任何交易所。{RESET}")
        return 1

    failed = False
    for report in reports:
        name = report["exchange"]
        if not report["ok"]:
            print(f"{RED}✗ {name}{RESET}  {report['detail']}")
            failed = True
            continue
        if report["read_only"] is False:
            print(f"{RED}✗ {name}{RESET}  {report['detail']}")
            failed = True
            continue
        icon = f"{GREEN}✓{RESET}" if report["read_only"] else f"{YELLOW}?{RESET}"
        print(f"{icon} {name}  {report['detail']}")
        if report.get("permissions"):
            print(f"    {DIM}权限：{', '.join(report['permissions'])}{RESET}")
        if report.get("account_label"):
            print(f"    {DIM}账号：{report['account_label']}{RESET}")
    return 1 if failed else 0


# --------------------------------------------------------------------------
# qr
# --------------------------------------------------------------------------
def cmd_qr_scan(args: argparse.Namespace) -> int:
    from .qr import detect_qrcodes

    hits = detect_qrcodes(args.image)
    if not hits:
        print(f"{RED}没有识别到二维码{RESET}")
        return 1
    for i, hit in enumerate(hits):
        x, y, w, h = hit.to_dict()["bbox"].values()
        brand = hit.to_dict()["brand"] or "未知"
        print(f"[{i}] {brand:<8} {w}x{h} @({x},{y})  {hit.payload}")
    return 0


def cmd_qr_crop(args: argparse.Namespace) -> int:
    from .qr import crop_qr
    from .qr.detect import guess_brand

    result = crop_qr(
        args.image,
        index=args.index,
        payload_filter=args.filter,
        size=args.size,
        regenerate=not args.no_regenerate,
    )

    settings = get_settings()
    if args.output:
        target = Path(args.output)
    elif args.exchange:
        target = settings.qr_dir / f"{args.exchange}.png"
    else:
        target = Path(args.image).with_name(Path(args.image).stem + "_qr.png")
    result.save(target)

    print(f"{GREEN}✓{RESET} 已保存 {target}")
    print(f"  内容：{result.payload}")
    print(f"  归属：{guess_brand(result.payload or '') or '未知'}")
    print(f"  方式：{'按内容重绘' if result.regenerated else '像素裁剪'}")
    for warning in result.warnings:
        print(f"  {YELLOW}! {warning}{RESET}")

    brand = guess_brand(result.payload or "")
    if args.exchange and brand and brand != args.exchange:
        print(f"  {RED}! 这张码看起来属于 {brand}，但你存成了 {args.exchange}{RESET}")
        return 1
    return 0


def cmd_qr_compose(args: argparse.Namespace) -> int:
    from .qr import Panel, compose

    settings = get_settings()
    sources = {}
    for exchange in SUPPORTED_EXCHANGES:
        explicit = getattr(args, exchange, None)
        if explicit:
            sources[exchange] = explicit
        else:
            default = settings.qr_dir / f"{exchange}.png"
            if default.exists():
                sources[exchange] = default

    if not sources:
        print(
            f"{RED}没有找到任何收款码。{RESET}\n"
            f"先用 `cexpay qr crop 截图.png -e binance` 生成，或用 --binance/--okx/--bitget 指定。",
            file=sys.stderr,
        )
        return 1

    panels = [Panel.from_source(name, path) for name, path in sources.items()]
    result = compose(
        panels,
        layout=args.layout,
        qr_size=args.size,
        gutter_ratio=args.gutter,
        title=args.title,
        footnote=args.footnote,
    )
    target = Path(args.output)
    result.save(target)

    print(f"{GREEN}✓{RESET} 已生成 {target}  ({result.image.size[0]}x{result.image.size[1]})")
    print(f"  包含：{'、'.join(sources)}")
    for exchange, ok in result.verified.items():
        icon = f"{GREEN}✓{RESET}" if ok else f"{RED}✗{RESET}"
        print(f"  {icon} {exchange} 回读校验")
    for warning in result.warnings:
        print(f"  {YELLOW}! {warning}{RESET}")
    return 0 if result.all_verified else 1


# --------------------------------------------------------------------------
# order / tx
# --------------------------------------------------------------------------
def cmd_order_create(args: argparse.Namespace) -> int:
    gateway = _gateway()
    order = gateway.create_order(
        args.amount, exchange=args.exchange, merchant_ref=args.ref, ttl_s=args.ttl
    )
    print(f"{GREEN}✓{RESET} 订单 {order.order_id}")
    print(
        f"  请支付：{YELLOW}{order.pay_amount} {order.currency}{RESET}"
        f"（原价 {order.base_amount}）"
    )
    if order.memo:
        print(f"  备注码：{order.memo}")
    print(f"  有效期：{args.ttl or gateway.settings.order_ttl_s} 秒")
    return 0


def cmd_order_check(args: argparse.Namespace) -> int:
    gateway = _gateway()
    result = gateway.sweep(order_id=args.order_id)
    order = gateway.get_order(args.order_id)
    if order is None:
        print(f"{RED}订单不存在{RESET}")
        return 1
    icon = GREEN + "✓" + RESET if order.status == "paid" else YELLOW + "…" + RESET
    print(f"{icon} {order.order_id} 状态：{order.status}")
    print(f"  扫描了 {result['transactions']} 笔进账")
    if order.status == "paid":
        print(f"  核销来源：{order.matched_exchange} / {order.matched_tx_id}")
        print(f"  匹配依据：{order.match_reason}")
    for error in result["errors"]:
        print(f"  {RED}! {error}{RESET}")
    return 0


def cmd_order_list(args: argparse.Namespace) -> int:
    gateway = _gateway()
    orders = gateway.store.list(status=args.status, limit=args.limit)
    if not orders:
        print(f"{DIM}没有订单{RESET}")
        return 0
    for order in orders:
        print(
            f"{order.order_id}  {order.status:<9} {order.pay_amount} {order.currency}"
            f"  {order.matched_exchange or '-'}"
        )
    _print_json(gateway.stats())
    return 0


def cmd_tx(args: argparse.Namespace) -> int:
    from .store import now_ms

    gateway = _gateway()
    end = now_ms()
    start = end - args.minutes * 60 * 1000
    txs, errors = gateway.fetch_transactions(
        start, end, exchanges=[args.exchange] if args.exchange else None
    )
    txs.sort(key=lambda t: t.timestamp_ms, reverse=True)

    if not txs:
        print(f"{DIM}最近 {args.minutes} 分钟没有进账{RESET}")
    for tx in txs:
        import datetime

        stamp = datetime.datetime.fromtimestamp(tx.timestamp_ms / 1000).strftime("%m-%d %H:%M:%S")
        ident = " ".join(f"{k}={v}" for k, v in tx.identifiers().items() if v)
        print(f"{stamp}  {tx.exchange:<8} {tx.amount:>12} {tx.currency}  {ident}")
    for error in errors:
        print(f"{RED}! {error}{RESET}", file=sys.stderr)
    return 1 if errors and not txs else 0


# --------------------------------------------------------------------------
# webhook-test：给接入方本地调通验签用
# --------------------------------------------------------------------------
def cmd_webhook_test(args: argparse.Namespace) -> int:
    """往指定 URL 发一条**签名正确**的假回调，让接入方能在本地把验签调通。

    不需要真的收到钱，也不用等交易所——这是接入过程中最费时间的一环。
    """
    import time as _time
    import uuid as _uuid

    from .notify import deliver, sign_payload

    settings = get_settings()
    secret = args.secret or settings.webhook_secret
    if not secret:
        print(
            f"{YELLOW}没有配置回调密钥。{RESET}\n"
            f"请设置 CEXPAY_WEBHOOK_SECRET 或用 --secret 传入——"
            f"否则发出去的请求不带签名，验签这一步就测不到。",
            file=sys.stderr,
        )
        if not args.allow_unsigned:
            return 2

    order_id = args.order_id or _uuid.uuid4().hex[:20]
    payload = {
        "event": "order.paid",
        "order": {
            "order_id": order_id,
            "merchant_ref": args.ref,
            "exchange": args.exchange,
            "base_amount": args.amount,
            "pay_amount": args.amount,
            "currency": args.currency,
            "status": "paid",
            "memo": None,
            "created_ms": int(_time.time() * 1000) - 60_000,
            "expires_ms": int(_time.time() * 1000) + 840_000,
            "expires_in_s": 840,
            "paid_ms": int(_time.time() * 1000),
            "metadata": {"webhook_test": True},
            "settlement": {
                "exchange": args.exchange,
                "tx_id": f"TEST-{order_id[:8]}",
                "tier": 1,
                "reason": f"金额精确命中 {args.amount}（这是 webhook-test 造的假数据）",
            },
        },
    }

    stamp = int(_time.time())
    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))

    print(f"{DIM}POST{RESET} {args.url}")
    print(f"{DIM}X-CexPay-Timestamp:{RESET} {stamp}")
    if secret:
        print(f"{DIM}X-CexPay-Signature:{RESET} {sign_payload(secret, stamp, body)}")
    print(f"{DIM}被签名的字符串:{RESET} {stamp}.<原始请求体>")
    if args.verbose:
        print(f"{DIM}请求体:{RESET}")
        _print_json(payload)

    ok, detail = deliver(
        args.url, payload, secret=secret, timeout=args.timeout, timestamp=stamp
    )
    print()
    if ok:
        print(f"{GREEN}✓ 对方返回 {detail}{RESET} —— 2xx 即视为投递成功，不会重试。")
        return 0

    print(f"{RED}✗ {detail}{RESET}")
    print(
        f"{DIM}排查顺序：\n"
        f"  1. 验签用的是**原始字节**吗？先 parse 再 stringify 一定对不上\n"
        f"  2. 两边的 CEXPAY_WEBHOOK_SECRET 是同一个吗？\n"
        f"  3. 被签名的是 f\"{{timestamp}}.{{raw_body}}\"，别漏掉那个点\n"
        f"  4. SDK 默认拒绝时间戳偏移超过 300s，服务器时间对得上吗？{RESET}"
    )
    print(f"{DIM}真实投递失败会按 0/15s/1m/5m/30m/2h/6h 重试 7 次，"
          f"所以你的处理必须对 order_id 幂等。{RESET}")
    return 1


# --------------------------------------------------------------------------
# openapi：导出规格，接入方可以自己生成任意语言的 client
# --------------------------------------------------------------------------
def cmd_openapi(args: argparse.Namespace) -> int:
    from .server import create_app

    spec = create_app(start_poller=False).openapi()
    text = json.dumps(spec, ensure_ascii=False, indent=2)
    if args.output:
        Path(args.output).write_text(text, encoding="utf-8")
        print(f"{GREEN}✓{RESET} 已写出 {args.output}（{len(spec.get('paths', {}))} 个路由）")
        print(f"{DIM}可以喂给 openapi-generator / oapi-codegen 生成任意语言的客户端。{RESET}")
    else:
        print(text)
    return 0


# --------------------------------------------------------------------------
# 入口
# --------------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="cexpay",
        description="多交易所聚合支付 · Binance / OKX / Bitget 个人收款自动核销",
    )
    parser.add_argument("--version", action="version", version=f"multi-cex-pay {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    # serve
    p_serve = sub.add_parser("serve", help="启动 HTTP 服务")
    p_serve.add_argument("--host")
    p_serve.add_argument("--port", type=int)
    p_serve.add_argument("--reload", action="store_true", help="改代码自动重启（开发用）")
    p_serve.add_argument("--log-level", default="info")
    p_serve.set_defaults(func=cmd_serve)

    # creds
    p_creds = sub.add_parser("creds", help="管理交易所 API 凭据")
    creds_sub = p_creds.add_subparsers(dest="creds_command", required=True)

    p_cl = creds_sub.add_parser("list", help="查看已配置的凭据（脱敏）")
    p_cl.set_defaults(func=cmd_creds_list)

    p_cs = creds_sub.add_parser("set", help="配置某个交易所的凭据")
    p_cs.add_argument("exchange", choices=SUPPORTED_EXCHANGES)
    p_cs.add_argument("--api-key")
    p_cs.add_argument("--account-label", help="收款账号，如 Pay ID / UID，展示在收银台")
    p_cs.add_argument("--enable", action="store_true")
    p_cs.add_argument("--disable", action="store_true")
    p_cs.add_argument("--only-key", action="store_true", help="只改 api_key")
    p_cs.add_argument("--only-secret", action="store_true", help="只改 secret / passphrase")
    p_cs.set_defaults(func=cmd_creds_set)

    p_ct = creds_sub.add_parser("test", help="连通性与只读权限自检")
    p_ct.add_argument("-e", "--exchange", choices=SUPPORTED_EXCHANGES)
    p_ct.set_defaults(func=cmd_creds_test)

    # qr
    p_qr = sub.add_parser("qr", help="收款码识别 / 裁剪 / 聚合")
    qr_sub = p_qr.add_subparsers(dest="qr_command", required=True)

    p_scan = qr_sub.add_parser("scan", help="识别图里的二维码并打印内容")
    p_scan.add_argument("image")
    p_scan.set_defaults(func=cmd_qr_scan)

    p_crop = qr_sub.add_parser("crop", help="从截图里自动抠出收款码")
    p_crop.add_argument("image")
    p_crop.add_argument("-o", "--output")
    p_crop.add_argument("-e", "--exchange", choices=SUPPORTED_EXCHANGES,
                        help="存成该交易所的收款码")
    p_crop.add_argument("--index", type=int, default=0, help="图里有多个码时选第几个")
    p_crop.add_argument("--filter", help="只要内容包含该子串的码")
    p_crop.add_argument("--size", type=int, default=640)
    p_crop.add_argument("--no-regenerate", action="store_true",
                        help="不按内容重绘，做像素级透视裁剪")
    p_crop.set_defaults(func=cmd_qr_crop)

    p_comp = qr_sub.add_parser("compose", help="把多张收款码拼成一张聚合图")
    p_comp.add_argument("-o", "--output", default="aggregate.png")
    p_comp.add_argument("--binance")
    p_comp.add_argument("--okx")
    p_comp.add_argument("--bitget")
    p_comp.add_argument("--layout", default="row", choices=("row", "column", "grid"))
    p_comp.add_argument("--size", type=int, default=520)
    p_comp.add_argument("--gutter", type=float, default=0.45,
                        help="格间距比例，别低于 0.3 否则容易串码")
    p_comp.add_argument("--title", default="扫码支付 · 任选一家")
    p_comp.add_argument("--footnote", default="请使用对应交易所 App 扫描该品牌下方的二维码")
    p_comp.set_defaults(func=cmd_qr_compose)

    # order
    p_order = sub.add_parser("order", help="订单操作")
    order_sub = p_order.add_subparsers(dest="order_command", required=True)

    p_oc = order_sub.add_parser("create", help="创建订单")
    p_oc.add_argument("amount")
    p_oc.add_argument("-e", "--exchange", choices=SUPPORTED_EXCHANGES)
    p_oc.add_argument("--ref", help="商户单号")
    p_oc.add_argument("--ttl", type=int, help="有效期（秒）")
    p_oc.set_defaults(func=cmd_order_create)

    p_ock = order_sub.add_parser("check", help="立刻核销一次")
    p_ock.add_argument("order_id")
    p_ock.set_defaults(func=cmd_order_check)

    p_ol = order_sub.add_parser("list", help="列出订单")
    p_ol.add_argument("--status")
    p_ol.add_argument("--limit", type=int, default=20)
    p_ol.set_defaults(func=cmd_order_list)

    # webhook-test
    p_wh = sub.add_parser(
        "webhook-test",
        help="给你的回调地址发一条签名正确的假回调（接入调试神器）",
    )
    p_wh.add_argument("url", help="你的回调地址，如 http://127.0.0.1:5000/webhook")
    p_wh.add_argument("--secret", help="回调密钥，默认取 CEXPAY_WEBHOOK_SECRET")
    p_wh.add_argument("--amount", default="9.9001", help="假订单的金额")
    p_wh.add_argument("--currency", default="USDT")
    p_wh.add_argument("--exchange", default="binance",
                      choices=SUPPORTED_EXCHANGES, help="假装钱从哪家进来")
    p_wh.add_argument("--order-id", help="指定订单号，默认随机（可用来测幂等）")
    p_wh.add_argument("--ref", help="商户单号 merchant_ref")
    p_wh.add_argument("--timeout", type=int, default=10)
    p_wh.add_argument("-v", "--verbose", action="store_true", help="打印完整请求体")
    p_wh.add_argument("--allow-unsigned", action="store_true",
                      help="没有密钥也照发（那样测不到验签）")
    p_wh.set_defaults(func=cmd_webhook_test)

    # openapi
    p_oa = sub.add_parser("openapi", help="导出 OpenAPI 规格，用来生成客户端")
    p_oa.add_argument("-o", "--output", help="写到文件，默认打到标准输出")
    p_oa.set_defaults(func=cmd_openapi)

    # tx
    p_tx = sub.add_parser("tx", help="查看各所最近进账")
    p_tx.add_argument("--minutes", type=int, default=120)
    p_tx.add_argument("-e", "--exchange", choices=SUPPORTED_EXCHANGES)
    p_tx.set_defaults(func=cmd_tx)

    return parser


def main(argv: list[str] | None = None) -> int:
    setup_logging()
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except CexPayError as exc:
        print(f"{RED}错误：{exc}{RESET}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\n已取消", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
