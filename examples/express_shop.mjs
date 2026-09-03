// Express 迷你商店：走 HTTP API + Node SDK 接入 multi-cex-pay。
//
// 需要 Node ≥ 18（SDK 用了内置 fetch）。装依赖：
//
//   examples/package.json
//   {
//     "name": "cexpay-express-shop",
//     "private": true,
//     "type": "module",
//     "dependencies": { "express": "^4.19.2" }
//   }
//
//   cd examples && npm install
//
// 先起网关，再起本店：
//   CEXPAY_ADMIN_TOKEN=dev CEXPAY_WEBHOOK_SECRET=shhh cexpay serve
//   CEXPAY_WEBHOOK_SECRET=shhh node examples/express_shop.mjs      # http://127.0.0.1:3000
//
// 两边的 CEXPAY_WEBHOOK_SECRET 必须一致，否则回调验签必然失败。
//
// 路由：
//   GET  /         一个商品的落地页
//   POST /buy      建单 -> 302 跳到网关收银台
//   POST /webhook  接收 order.paid 回调，验签后标记已付
//   GET  /orders   查看本店记的订单状态

import express from "express";
import { CexPayClient, CexPayError } from "../sdk/node/cexpay.mjs";

const GATEWAY_URL = process.env.CEXPAY_GATEWAY_URL || "http://127.0.0.1:8787";
const WEBHOOK_SECRET = process.env.CEXPAY_WEBHOOK_SECRET || "shhh";
const PORT = Number(process.env.PORT || 3000);
const SHOP_URL = process.env.SHOP_URL || `http://127.0.0.1:${PORT}`;

const PRODUCT = { sku: "VPS-1C1G", name: "VPS 1C1G 月付", price: "9.9", currency: "USDT" };

const client = new CexPayClient(GATEWAY_URL, { webhookSecret: WEBHOOK_SECRET });

// 演示用的「订单表」。生产上换成数据库，并且在 order_id 上建唯一索引，
// 回调可能重复到达，靠唯一索引 + 状态判断做幂等最省心。
const ORDERS = new Map();
let seq = 1000;

const app = express();

app.get("/", (_req, res) => {
  res.type("html").send(`<!doctype html><meta charset="utf-8">
<title>迷你商店</title>
<body style="font-family:system-ui;max-width:40em;margin:4em auto">
<h1>${PRODUCT.name}</h1>
<p>价格 <b>${PRODUCT.price} ${PRODUCT.currency}</b>（SKU ${PRODUCT.sku}）</p>
<form method="post" action="/buy"><button>下单并去支付</button></form>
<p><a href="/orders">查看本店订单状态</a> · 网关 ${GATEWAY_URL}</p>
</body>`);
});

app.post("/buy", async (_req, res) => {
  const merchantRef = `SHOP-${++seq}`;          // 你自己的单号

  let result;
  try {
    // 同一个 merchantRef 再传一次会复用未过期的待付订单（网关侧幂等），
    // 所以用户重复点「下单」不会刷出一堆订单。
    result = await client.createOrder(PRODUCT.price, {
      merchantRef,
      callbackUrl: `${SHOP_URL}/webhook`,
      metadata: { sku: PRODUCT.sku },
    });
  } catch (err) {
    const detail = err instanceof CexPayError ? err.message : String(err);
    return res.status(502).send(`建单失败：${detail}（网关 ${GATEWAY_URL} 起了吗？）`);
  }

  const order = result.order;
  ORDERS.set(order.order_id, {
    merchant_ref: merchantRef,
    sku: PRODUCT.sku,
    pay_amount: order.pay_amount,
    currency: order.currency,
    state: "pending",
  });

  // checkout_url 是相对路径（/checkout?order_id=...），要自己拼网关域名
  res.redirect(302, GATEWAY_URL + result.checkout_url);
});

// 关键点一：这条路由必须挂 express.raw()，而且要在任何 express.json()
// 之前生效。验签算的是原始字节上的 HMAC；一旦 body 被 json 中间件解析过，
// req.body 就成了对象，再 JSON.stringify() 回去键序/空格/转义都可能变，
// 签名必然对不上。这里用 { type: "*/*" } 兜住所有 Content-Type。
app.post("/webhook", express.raw({ type: "*/*" }), (req, res) => {
  const raw = req.body;                          // Buffer，原始字节

  let ok;
  try {
    ok = client.verifyWebhook(
      raw,
      req.get("X-CexPay-Timestamp") || "",
      req.get("X-CexPay-Signature") || "",
    );
  } catch (err) {                                // 没配 webhookSecret
    return res.status(500).json({ error: String(err.message || err) });
  }
  if (!ok) {
    // 签名不对，或时间戳超出 300s 容忍窗（防重放）
    return res.status(400).json({ error: "bad signature" });
  }

  let payload = {};
  try {
    payload = JSON.parse(raw.toString("utf8"));
  } catch {
    return res.status(400).json({ error: "bad json" });
  }
  if (payload.event !== "order.paid") {
    return res.json({ ok: true, ignored: payload.event });
  }

  const order = payload.order || {};
  const orderId = order.order_id;
  if (!orderId) return res.status(400).json({ error: "missing order_id" });

  // 关键点二：必须对 order_id 幂等。
  // 网关的重试阶梯是 0/15s/1m/5m/30m/2h/6h，只要你没在 2xx 之前挂掉，
  // 同一笔订单就可能被投递多次；已处理过的直接返回 200，别重复发货。
  const record = ORDERS.get(orderId);
  if (!record) {
    // 本店没这单：可能是别的实例建的，也可能库被清了。
    // 记下来但不发货，返回 200 避免网关一直重试。
    ORDERS.set(orderId, { state: "paid-unknown", raw: order });
    return res.json({ ok: true, note: "unknown order recorded" });
  }
  if (record.state === "paid") {
    return res.json({ ok: true, note: "already processed" });
  }
  record.state = "paid";
  record.settlement = order.settlement;

  // 这里放你真正的发货 / 开通逻辑（也要能被重复调用而不出事）
  console.log("订单已支付，开始发货", orderId);
  res.json({ ok: true });
});

app.get("/orders", (_req, res) => {
  res.json({ count: ORDERS.size, orders: Object.fromEntries(ORDERS) });
});

app.listen(PORT, "127.0.0.1", () => {
  console.log(`网关 ${GATEWAY_URL} / 本店 ${SHOP_URL}`);
  console.log(`回调地址 ${SHOP_URL}/webhook，签名密钥取自 CEXPAY_WEBHOOK_SECRET`);
});
