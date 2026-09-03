// multi-cex-pay 商户端 SDK（Node.js ≥ 18，零依赖）
//
//   import { CexPayClient } from "./cexpay.mjs";
//   const client = new CexPayClient("http://127.0.0.1:8787", { webhookSecret: "..." });
//   const { order, checkout_url } = await client.createOrder("9.9", { merchantRef: "SHOP-1" });
//
// Express 里验签（注意要拿原始 body）：
//   app.post("/hook", express.raw({ type: "*/*" }), (req, res) => {
//     const ok = client.verifyWebhook(req.body, req.get("X-CexPay-Timestamp"),
//                                     req.get("X-CexPay-Signature"));
//     ...
//   });

import { createHmac, timingSafeEqual } from "node:crypto";

export class CexPayError extends Error {
  constructor(message, status) {
    super(message);
    this.name = "CexPayError";
    this.status = status;
  }
}

export class CexPayClient {
  constructor(baseUrl, { webhookSecret = null, adminToken = null, timeoutMs = 10000 } = {}) {
    this.baseUrl = baseUrl.replace(/\/+$/, "");
    this.webhookSecret = webhookSecret;
    this.adminToken = adminToken;
    this.timeoutMs = timeoutMs;
  }

  async #call(method, path, body = undefined, { admin = false } = {}) {
    const headers = { "Content-Type": "application/json" };
    if (admin) {
      if (!this.adminToken) throw new CexPayError("该接口需要 adminToken");
      headers.Authorization = `Bearer ${this.adminToken}`;
    }
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), this.timeoutMs);
    let res;
    try {
      res = await fetch(this.baseUrl + path, {
        method,
        headers,
        body: body === undefined ? undefined : JSON.stringify(body),
        signal: controller.signal,
      });
    } catch (err) {
      throw new CexPayError(`无法连接 ${this.baseUrl}${path}: ${err.message}`);
    } finally {
      clearTimeout(timer);
    }

    const text = await res.text();
    let data = {};
    try { data = text ? JSON.parse(text) : {}; } catch { data = { detail: text }; }
    if (!res.ok) throw new CexPayError(data.detail || `HTTP ${res.status}`, res.status);
    return data;
  }

  /** 创建订单。相同 merchantRef 会复用未过期的待付订单（幂等）。 */
  createOrder(amount, { exchange, merchantRef, callbackUrl, ttlS, metadata } = {}) {
    const payload = { amount: String(amount) };
    if (exchange) payload.exchange = exchange;
    if (merchantRef) payload.merchant_ref = merchantRef;
    if (callbackUrl) payload.callback_url = callbackUrl;
    if (ttlS) payload.ttl_s = ttlS;
    if (metadata) payload.metadata = metadata;
    return this.#call("POST", "/api/orders", payload);
  }

  async getOrder(orderId) {
    return (await this.#call("GET", `/api/orders/${orderId}`)).order;
  }

  /** 主动催一次核销（用户点「我已支付」时用）。 */
  checkOrder(orderId) {
    return this.#call("POST", `/api/orders/${orderId}/check`);
  }

  async cancelOrder(orderId) {
    return (await this.#call("POST", `/api/orders/${orderId}/cancel`)).order;
  }

  async exchanges() {
    return (await this.#call("GET", "/api/exchanges")).exchanges;
  }

  /** 阻塞等待支付结果。生产环境建议改用 webhook。 */
  async waitForPayment(orderId, { timeoutMs = 900000, intervalMs = 5000 } = {}) {
    const deadline = Date.now() + timeoutMs;
    while (Date.now() < deadline) {
      const order = await this.getOrder(orderId);
      if (order.status !== "pending") return order;
      await new Promise((r) => setTimeout(r, intervalMs));
    }
    throw new CexPayError(`等待超时：订单 ${orderId} 仍未支付`);
  }

  /** 校验回调。rawBody 必须是原始字节/字符串，别先 parse 再 stringify。 */
  verifyWebhook(rawBody, timestamp, signature, { toleranceS = 300 } = {}) {
    if (!this.webhookSecret) throw new CexPayError("未配置 webhookSecret");
    const stamp = Number(timestamp);
    if (!Number.isFinite(stamp)) return false;
    if (toleranceS && Math.abs(Date.now() / 1000 - stamp) > toleranceS) return false;

    const body = Buffer.isBuffer(rawBody) ? rawBody.toString("utf8") : String(rawBody);
    const expected = createHmac("sha256", this.webhookSecret)
      .update(`${stamp}.${body}`)
      .digest("hex");
    const a = Buffer.from(expected);
    const b = Buffer.from(String(signature || ""));
    return a.length === b.length && timingSafeEqual(a, b);
  }

  adminSweep() {
    return this.#call("POST", "/api/admin/sweep", undefined, { admin: true });
  }
}

export default CexPayClient;
