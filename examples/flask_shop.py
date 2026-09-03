#!/usr/bin/env python3
"""Flask 迷你商店：走 HTTP API + Python SDK 接入 multi-cex-pay。

依赖：
    pip install flask
    # SDK 是单文件零依赖，直接从仓库 import，生产上拷到你自己项目里即可

先起网关，再起本店：
    CEXPAY_ADMIN_TOKEN=dev CEXPAY_WEBHOOK_SECRET=shhh cexpay serve
    CEXPAY_WEBHOOK_SECRET=shhh python examples/flask_shop.py     # http://127.0.0.1:5000

两边的 CEXPAY_WEBHOOK_SECRET 必须一致，否则回调验签必然失败。

路由：
    GET  /         一个商品的落地页
    POST /buy      建单 -> 302 跳到网关收银台
    POST /webhook  接收 order.paid 回调，验签后标记已付
    GET  /orders   查看本店记的订单状态
"""

from __future__ import annotations

import os
import sys
import threading
from pathlib import Path

from flask import Flask, jsonify, redirect, request

# SDK 在仓库的 sdk/python/ 下；真实项目里把 cexpay_client.py 拷进去就行
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "sdk" / "python"))
from cexpay_client import CexPayClient, CexPayError

GATEWAY_URL = os.environ.get("CEXPAY_GATEWAY_URL", "http://127.0.0.1:8787")
WEBHOOK_SECRET = os.environ.get("CEXPAY_WEBHOOK_SECRET", "shhh")
SHOP_URL = os.environ.get("SHOP_URL", "http://127.0.0.1:5000")

PRODUCT = {"sku": "VPS-1C1G", "name": "VPS 1C1G 月付", "price": "9.9", "currency": "USDT"}

client = CexPayClient(GATEWAY_URL, webhook_secret=WEBHOOK_SECRET)

# 演示用的「订单表」。生产上换成数据库，并且 order_id 上建唯一索引——
# 回调可能重复到达，靠唯一索引 + 状态判断做幂等最省心。
ORDERS: dict[str, dict] = {}
LOCK = threading.Lock()
SEQ = {"n": 1000}

app = Flask(__name__)


@app.get("/")
def home():
    return f"""<!doctype html><meta charset="utf-8">
<title>迷你商店</title>
<body style="font-family:system-ui;max-width:40em;margin:4em auto">
<h1>{PRODUCT['name']}</h1>
<p>价格 <b>{PRODUCT['price']} {PRODUCT['currency']}</b>（SKU {PRODUCT['sku']}）</p>
<form method="post" action="/buy"><button>下单并去支付</button></form>
<p><a href="/orders">查看本店订单状态</a> · 网关 {GATEWAY_URL}</p>
</body>"""


@app.post("/buy")
def buy():
    with LOCK:
        SEQ["n"] += 1
        merchant_ref = f"SHOP-{SEQ['n']}"        # 你自己的单号

    try:
        # 同一个 merchant_ref 再传一次会复用未过期的待付订单（网关侧幂等），
        # 所以用户重复点「下单」不会刷出一堆订单。
        result = client.create_order(
            PRODUCT["price"],
            merchant_ref=merchant_ref,
            callback_url=f"{SHOP_URL}/webhook",
            metadata={"sku": PRODUCT["sku"]},
        )
    except CexPayError as exc:
        return f"建单失败：{exc}（网关 {GATEWAY_URL} 起了吗？）", 502

    order = result["order"]
    with LOCK:
        ORDERS[order["order_id"]] = {
            "merchant_ref": merchant_ref,
            "sku": PRODUCT["sku"],
            "pay_amount": order["pay_amount"],
            "currency": order["currency"],
            "state": "pending",
        }

    # checkout_url 是相对路径（/checkout?order_id=...），要自己拼网关域名
    return redirect(GATEWAY_URL + result["checkout_url"], code=302)


@app.post("/webhook")
def webhook():
    # 关键点一：验签必须用**原始字节**。
    # request.get_data() 拿的是未经解析的 body；千万不要用 request.json
    # 再 json.dumps() 回去——键序、空格、Unicode 转义都会变，HMAC 必然不一致。
    raw = request.get_data()

    try:
        ok = client.verify_webhook(
            raw,
            request.headers.get("X-CexPay-Timestamp", ""),
            request.headers.get("X-CexPay-Signature", ""),
        )
    except CexPayError as exc:                   # 没配 webhook_secret
        return jsonify(error=str(exc)), 500
    if not ok:
        # 签名不对或时间戳超出 300s 容忍窗（防重放），直接拒绝
        return jsonify(error="bad signature"), 400

    payload = request.get_json(force=True, silent=True) or {}
    if payload.get("event") != "order.paid":
        return jsonify(ok=True, ignored=payload.get("event")), 200

    order = payload.get("order") or {}
    order_id = order.get("order_id")
    if not order_id:
        return jsonify(error="missing order_id"), 400

    # 关键点二：必须对 order_id 幂等。
    # 网关的重试阶梯是 0/15s/1m/5m/30m/2h/6h，只要你没在 2xx 之前挂掉，
    # 同一笔订单就可能被投递多次；已处理过的直接返回 200，别重复发货。
    with LOCK:
        record = ORDERS.get(order_id)
        if record is None:
            # 本店没这单：可能是别的实例建的，也可能库被清了。
            # 记下来但不发货，返回 200 避免网关一直重试。
            ORDERS[order_id] = {"state": "paid-unknown", "raw": order}
            return jsonify(ok=True, note="unknown order recorded"), 200
        if record["state"] == "paid":
            return jsonify(ok=True, note="already processed"), 200
        record["state"] = "paid"
        record["settlement"] = order.get("settlement")

    # 这里放你真正的发货 / 开通逻辑（也要能被重复调用而不出事）
    app.logger.info("订单已支付，开始发货 %s", order_id)
    return jsonify(ok=True), 200


@app.get("/orders")
def orders():
    with LOCK:
        return jsonify(count=len(ORDERS), orders=ORDERS)


if __name__ == "__main__":
    print(f"网关 {GATEWAY_URL} / 本店 {SHOP_URL}")
    print(f"回调地址 {SHOP_URL}/webhook，签名密钥取自 CEXPAY_WEBHOOK_SECRET")
    app.run(host="127.0.0.1", port=int(os.environ.get("PORT", "5000")), debug=False)
