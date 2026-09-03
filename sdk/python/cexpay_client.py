"""multi-cex-pay 商户端 SDK（Python）。

只依赖标准库，单文件拷走即可用。

    from cexpay_client import CexPayClient

    client = CexPayClient("http://127.0.0.1:8787", webhook_secret="...")
    order = client.create_order("9.9", merchant_ref="SHOP-1001")
    print(order["checkout_url"])

    # 在你的 webhook 路由里：
    if not client.verify_webhook(raw_body, timestamp_header, signature_header):
        return 400
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
import urllib.error
import urllib.request
from typing import Any


class CexPayError(RuntimeError):
    def __init__(self, message: str, status: int | None = None):
        self.status = status
        super().__init__(message)


class CexPayClient:
    def __init__(
        self,
        base_url: str,
        *,
        webhook_secret: str | None = None,
        admin_token: str | None = None,
        timeout: float = 10.0,
    ):
        self.base_url = base_url.rstrip("/")
        self.webhook_secret = webhook_secret
        self.admin_token = admin_token
        self.timeout = timeout

    # ---------------- 内部 ----------------
    def _call(self, method: str, path: str, body: dict[str, Any] | None = None,
              *, admin: bool = False) -> dict[str, Any]:
        url = self.base_url + path
        data = json.dumps(body).encode("utf-8") if body is not None else None
        request = urllib.request.Request(url, data=data, method=method)
        request.add_header("Content-Type", "application/json")
        if admin:
            if not self.admin_token:
                raise CexPayError("该接口需要 admin_token")
            request.add_header("Authorization", "Bearer " + self.admin_token)
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                return json.loads(response.read().decode("utf-8") or "{}")
        except urllib.error.HTTPError as exc:
            raw = exc.read().decode("utf-8", errors="replace")
            try:
                detail = json.loads(raw).get("detail", raw)
            except ValueError:
                detail = raw
            raise CexPayError(detail, exc.code) from exc
        except urllib.error.URLError as exc:
            raise CexPayError(f"无法连接 {url}: {exc.reason}") from exc

    # ---------------- 订单 ----------------
    def create_order(
        self,
        amount: str | float,
        *,
        exchange: str | None = None,
        merchant_ref: str | None = None,
        callback_url: str | None = None,
        ttl_s: int | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """创建订单。返回体含 order / checkout_url / qr_url。

        传相同的 ``merchant_ref`` 会复用未过期的待付订单（幂等）。
        """
        payload: dict[str, Any] = {"amount": str(amount)}
        for key, value in (
            ("exchange", exchange), ("merchant_ref", merchant_ref),
            ("callback_url", callback_url), ("ttl_s", ttl_s), ("metadata", metadata),
        ):
            if value is not None:
                payload[key] = value
        return self._call("POST", "/api/orders", payload)

    def get_order(self, order_id: str) -> dict[str, Any]:
        return self._call("GET", f"/api/orders/{order_id}")["order"]

    def check_order(self, order_id: str) -> dict[str, Any]:
        """主动催一次核销（用户点"我已支付"时用）。"""
        return self._call("POST", f"/api/orders/{order_id}/check")

    def cancel_order(self, order_id: str) -> dict[str, Any]:
        return self._call("POST", f"/api/orders/{order_id}/cancel")["order"]

    def wait_for_payment(self, order_id: str, *, timeout_s: int = 900,
                         interval_s: float = 5.0) -> dict[str, Any]:
        """阻塞等待支付结果（适合脚本 / 小工具，生产建议用 webhook）。"""
        deadline = time.time() + timeout_s
        while time.time() < deadline:
            order = self.get_order(order_id)
            if order["status"] != "pending":
                return order
            time.sleep(interval_s)
        raise CexPayError(f"等待超时：订单 {order_id} 仍未支付")

    def exchanges(self) -> list:
        return self._call("GET", "/api/exchanges")["exchanges"]

    # ---------------- 回调验签 ----------------
    def verify_webhook(self, raw_body: bytes | str, timestamp: str | int,
                       signature: str, *, tolerance_s: int = 300) -> bool:
        """校验回调请求。

        raw_body 必须是**原始字节**，不要先反序列化再重新 dump。
        """
        if not self.webhook_secret:
            raise CexPayError("未配置 webhook_secret")
        if isinstance(raw_body, bytes):
            raw_body = raw_body.decode("utf-8")
        try:
            stamp = int(timestamp)
        except (TypeError, ValueError):
            return False
        # 拒绝重放
        if tolerance_s and abs(time.time() - stamp) > tolerance_s:
            return False
        expected = hmac.new(
            self.webhook_secret.encode("utf-8"),
            f"{stamp}.{raw_body}".encode(),
            hashlib.sha256,
        ).hexdigest()
        return hmac.compare_digest(expected, signature or "")

    # ---------------- 后台 ----------------
    def admin_sweep(self) -> dict[str, Any]:
        return self._call("POST", "/api/admin/sweep", admin=True)

    def admin_orders(self, *, status: str | None = None, limit: int = 50) -> dict[str, Any]:
        query = f"?limit={limit}" + (f"&status={status}" if status else "")
        return self._call("GET", "/api/admin/orders" + query, admin=True)


__all__ = ["CexPayClient", "CexPayError"]
