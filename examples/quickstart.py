#!/usr/bin/env python3
"""直接调用 cexpay 库下单并轮询核销（不经过 HTTP 服务）。

适用场景：你的程序和网关跑在同一个进程 / 同一台机器上，不想再起一个
FastAPI 服务。这条路径用的就是 `cexpay serve` 内部用的同一套代码。

运行：
    .venv/bin/python examples/quickstart.py --amount 9.9
    .venv/bin/python examples/quickstart.py --amount 9.9 --demo   # 无凭据也能跑通全流程

没配置交易所凭据时脚本不会报错退出，而是打印订单信息 + 配置指引后正常结束；
加 --demo 会在本地伪造一条进账，用来演示 sweep() → 核销 → 结算的完整链路
（不接触任何交易所接口）。
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

# 让 examples/ 下的脚本在未 pip install 时也能 import 到仓库里的 cexpay
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cexpay.config import CredentialStore, Settings
from cexpay.errors import CexPayError, ConfigError
from cexpay.exchanges.base import Transaction
from cexpay.gateway import PaymentGateway
from cexpay.matching import TIER_LABELS
from cexpay.store import STATUS_PAID, STATUS_PENDING, now_ms


def build_gateway(data_dir: str | None) -> PaymentGateway:
    """构造网关。

    注意：PaymentGateway() 不传 credentials 时会走 get_credential_store()，
    而后者读的是**全局** Settings，不是这里传进去的这份。所以只要自定义了
    data_dir，就必须把 CredentialStore 一起显式传进来，否则凭据会从默认
    目录读，行为和预期不一致。
    """
    settings = Settings(data_dir=data_dir) if data_dir else Settings()
    return PaymentGateway(settings=settings, credentials=CredentialStore(settings))


def describe_order(order) -> None:
    print("─" * 62)
    print(f"订单号      {order.order_id}")
    print(f"商户单号    {order.merchant_ref or '(未设置)'}")
    print(f"下单金额    {order.base_amount} {order.currency}")
    print(f"应付金额    {order.pay_amount} {order.currency}   <-- 必须一分不差地转这个数")
    print(f"指定交易所  {order.exchange or '任意已配置的交易所'}")
    if order.memo:
        print(f"备注码      {order.memo}   （T2，只有 Binance Pay 的转账备注能被读到）")
    left = max(0, (order.expires_ms - now_ms()) // 1000)
    print(f"有效期      {left}s")
    print("─" * 62)
    print(
        "为什么是「应付金额」而不是「下单金额」：T1 唯一金额匹配要求金额**完全相等**，\n"
        "系统给每笔订单分配了不同的 4 位小数后缀，靠这个后缀区分并发订单。\n"
        "用户少付超过 0.02 或多付超过 5 都不会自动核销。"
    )


def print_settlement(gateway: PaymentGateway, order_id: str) -> None:
    order = gateway.get_order(order_id)
    if order is None:
        print(f"[异常] 订单 {order_id} 不存在了")
        return
    data = order.to_dict(public=False)
    settlement = data.get("settlement") or {}
    tier = settlement.get("tier")
    print()
    print("=" * 62)
    print(f"订单 {order.order_id} 已核销")
    print(f"  状态      {order.status}")
    print(f"  实收      {order.pay_amount} {order.currency}")
    print(f"  交易所    {settlement.get('exchange')}")
    print(f"  流水号    {settlement.get('tx_id')}")
    print(f"  匹配层级  T{tier} {TIER_LABELS.get(tier, '')}")
    print(f"  匹配依据  {settlement.get('reason')}")
    print(f"  回调状态  {data.get('callback_state')}（没配 callback_url 时为 none）")
    print("=" * 62)


def install_demo_transaction(gateway: PaymentGateway, order, after_rounds: int = 2) -> None:
    """把 fetch_transactions 换成本地假数据，第 N 轮才「到账」。

    生产代码永远不要这么做——这里只是为了在没有任何交易所凭据的机器上
    也能把 sweep() 的核销链路走一遍。
    """
    state = {"round": 0}
    real_amount = order.pay_amount

    def fake_fetch(start_ms, end_ms, *, exchanges=None, limit=100):
        state["round"] += 1
        if state["round"] < after_rounds:
            return [], []
        tx = Transaction(
            exchange="binance",
            tx_id="DEMO-TX-0001",
            amount=real_amount,          # 精确金额 -> 命中 T1
            currency=order.currency,
            timestamp_ms=now_ms(),
            payer_name="Demo***User",
            memo=order.memo,
        )
        return [tx], []

    gateway.fetch_transactions = fake_fetch


def main() -> int:
    parser = argparse.ArgumentParser(
        description="cexpay 库直连用法：下单 + 轮询核销",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--amount", default="9.9", help="下单金额")
    parser.add_argument("--currency", default=None, help="币种，默认取 CEXPAY_CURRENCY")
    parser.add_argument("--exchange", default=None, help="限定交易所 binance/okx/bitget")
    parser.add_argument("--merchant-ref", default=None, help="商户单号（同号幂等复用未过期订单）")
    parser.add_argument("--ttl", type=int, default=None, help="订单有效期（秒）")
    parser.add_argument("--data-dir", default=None, help="数据目录，默认取 CEXPAY_DATA_DIR")
    parser.add_argument("--timeout", type=int, default=120, help="最长等待秒数")
    parser.add_argument("--interval", type=float, default=5.0, help="轮询间隔秒数")
    parser.add_argument(
        "--demo", action="store_true",
        help="演示模式：本地伪造一条进账，不请求任何交易所",
    )
    args = parser.parse_args()

    gateway = build_gateway(args.data_dir)
    print(f"数据目录    {gateway.settings.data_dir}")
    print(f"数据库      {gateway.settings.db_path}")

    configured = gateway.credentials.configured()
    print(f"已配置凭据  {configured or '(无)'}")

    # ---- 凭据检查：没有凭据也要能优雅收场 ----
    if not configured and not args.demo:
        print()
        print("当前没有任何可用的交易所凭据，无法真的去查进账。")
        print("订单仍然会正常创建（下面就是），但 sweep() 拉不到任何流水，永远不会核销。")
        print()
    elif configured and not args.demo:
        # 只读闸门：发现带写权限的 Key 直接拒绝继续
        try:
            gateway.assert_readonly()
            print("只读校验    通过")
        except ConfigError as exc:
            print(f"\n[只读校验失败] {exc}")
            return 2
        except CexPayError as exc:
            print(f"[警告] 只读校验没跑完（网络或接口问题）：{exc}")

    # ---- 下单 ----
    try:
        order = gateway.create_order(
            args.amount,
            exchange=args.exchange,
            currency=args.currency,
            merchant_ref=args.merchant_ref,
            ttl_s=args.ttl,
            metadata={"source": "examples/quickstart.py"},
        )
    except CexPayError as exc:
        print(f"[下单失败] {exc}")
        return 2

    describe_order(order)

    if not configured and not args.demo:
        print()
        print("下一步：配置一把**只读** API Key，然后重跑本脚本。")
        print("  export CEXPAY_BINANCE_API_KEY=...   CEXPAY_BINANCE_API_SECRET=...")
        print("  export CEXPAY_OKX_API_KEY=...       CEXPAY_OKX_API_SECRET=...  CEXPAY_OKX_PASSPHRASE=...")
        print("  export CEXPAY_BITGET_API_KEY=...    CEXPAY_BITGET_API_SECRET=... CEXPAY_BITGET_PASSPHRASE=...")
        print("或者用 CLI 写进加密后的凭据文件：")
        print("  cexpay creds set binance --api-key ... --api-secret ...")
        print("  cexpay creds test binance")
        print()
        print("想先看核销链路长什么样，不用配任何凭据：")
        print("  python examples/quickstart.py --amount 9.9 --demo")
        return 0

    if args.demo:
        print()
        print("[演示模式] 进账数据是本地伪造的，没有请求任何交易所接口。")
        install_demo_transaction(gateway, order)

    # ---- 轮询核销 ----
    print()
    print(f"开始轮询 sweep()，每 {args.interval}s 一次，最长 {args.timeout}s ...")
    deadline = time.monotonic() + args.timeout
    round_no = 0
    while time.monotonic() < deadline:
        round_no += 1
        try:
            # 只跑这一单，比全量 sweep 快；生产里后台 poller 跑的是全量
            result = gateway.sweep(order_id=order.order_id)
        except CexPayError as exc:
            print(f"  第 {round_no} 轮：sweep 失败 {exc}")
            time.sleep(args.interval)
            continue

        print(
            f"  第 {round_no} 轮：待核销 {result['checked']} 单，"
            f"拉到 {result['transactions']} 条进账，"
            f"核销 {len(result['settled'])} 单"
            + (f"，错误 {result['errors']}" if result["errors"] else "")
        )

        if result["settled"]:
            print_settlement(gateway, order.order_id)
            return 0

        current = gateway.get_order(order.order_id)
        if current is None:
            print("  订单不见了，退出")
            return 1
        if current.status == STATUS_PAID:
            # 后台 poller 可能已经先核销掉了
            print_settlement(gateway, order.order_id)
            return 0
        if current.status != STATUS_PENDING:
            print(f"  订单状态变为 {current.status}，停止轮询")
            return 1

        time.sleep(args.interval)

    print(f"\n等待超时（{args.timeout}s），订单 {order.order_id} 仍未支付。")
    print("订单没有作废，仍在有效期内；重跑本脚本并带上相同的 --merchant-ref 会复用它。")
    return 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\n已中断")
        sys.exit(130)
