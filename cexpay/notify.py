"""支付成功回调（webhook）。

- 请求体是订单 JSON，签名放在 ``X-CexPay-Signature``（HMAC-SHA256，hex）
- 商户侧验签：``hmac_sha256(secret, f"{timestamp}.{raw_body}")``
- 失败按固定阶梯重试：0s / 15s / 1m / 5m / 30m / 2h / 6h，共 7 次
- 商户返回 2xx 即视为成功；重复投递需商户侧按 order_id 幂等处理
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
from typing import Any

import requests

log = logging.getLogger("cexpay.notify")

# 第 n 次失败后等待的秒数
RETRY_LADDER = (0, 15, 60, 300, 1800, 7200, 21600)
MAX_ATTEMPTS = len(RETRY_LADDER)


def sign_payload(secret: str, timestamp: int, body: str) -> str:
    message = f"{timestamp}.{body}".encode()
    return hmac.new(secret.encode("utf-8"), message, hashlib.sha256).hexdigest()


def verify_signature(secret: str, timestamp: int, body: str, signature: str) -> bool:
    """给商户 SDK 用的验签函数。"""
    expected = sign_payload(secret, timestamp, body)
    return hmac.compare_digest(expected, signature or "")


def next_delay_s(attempts: int) -> int | None:
    """已经失败 ``attempts`` 次后，下一次该等多久；None 表示放弃。"""
    if attempts >= MAX_ATTEMPTS:
        return None
    return RETRY_LADDER[attempts]


def deliver(
    url: str,
    payload: dict[str, Any],
    *,
    secret: str | None = None,
    timeout: int = 10,
    timestamp: int | None = None,
) -> tuple[bool, str]:
    """投递一次回调，返回 (是否成功, 说明)。"""
    import time as _time

    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    stamp = timestamp if timestamp is not None else int(_time.time())
    headers = {
        "Content-Type": "application/json",
        "X-CexPay-Timestamp": str(stamp),
        "User-Agent": "multi-cex-pay/0.1",
    }
    if secret:
        headers["X-CexPay-Signature"] = sign_payload(secret, stamp, body)

    try:
        response = requests.post(
            url, data=body.encode("utf-8"), headers=headers, timeout=timeout
        )
    except requests.RequestException as exc:
        return False, f"网络错误: {exc}"

    if 200 <= response.status_code < 300:
        return True, f"HTTP {response.status_code}"
    return False, f"HTTP {response.status_code}: {response.text[:200]}"
