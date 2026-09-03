"""Binance Pay 适配器。

数据来源：``GET /sapi/v1/pay/transactions``（只读权限即可调用）
权限自检：``GET /sapi/v1/account/apiRestrictions``

Binance Pay 是三家里信息最全的：能同时拿到付款方昵称、Binance ID 和转账备注，
所以它支持"唯一金额 / 备注码 / 付款方昵称"三层匹配。
"""

from __future__ import annotations

import hashlib
import hmac
import time
from typing import Any
from urllib.parse import urlencode

from ..errors import ExchangeAPIError
from .base import (
    ExchangeAdapter,
    IdentifierSpec,
    PermissionReport,
    Transaction,
    to_decimal,
    to_ms,
)

BASE_URL = "https://api.binance.com"

# 计入"收款"的订单类型（正数金额）
INCOMING_ORDER_TYPES = {
    "PAY",            # C 端用户在商户侧消费
    "C2C",            # C 端用户间转账
    "CRYPTO_BOX",     # 红包
    "PAYOUT",         # 商户给用户付款
    "REMITTANCE",     # 汇款
    "C2C_HOLDING",    # 转账给非币安用户
}


class BinanceAdapter(ExchangeAdapter):
    name = "binance"
    display_name = "Binance Pay"
    brand_color = "#F0B90B"
    supports_memo = True
    pay_hint = "打开币安 App → Pay → 扫一扫，或直接向收款 Pay ID 转账"

    # ---------------- 签名 ----------------
    def _signed_params(self, params: dict[str, Any]) -> str:
        payload = dict(params)
        payload.setdefault("timestamp", int(time.time() * 1000))
        payload.setdefault("recvWindow", 5000)
        query = urlencode(payload, doseq=True)
        signature = hmac.new(
            self.credential.api_secret.encode("utf-8"),
            query.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
        return f"{query}&signature={signature}"

    def _get(self, endpoint: str, params: dict[str, Any] | None = None) -> Any:
        query = self._signed_params(params or {})
        url = f"{BASE_URL}{endpoint}?{query}"
        headers = {"X-MBX-APIKEY": self.credential.api_key}
        data = self._request("GET", url, headers=headers)
        # Binance 出错时会返回 {"code": -xxxx, "msg": "..."}
        if isinstance(data, dict) and data.get("code") not in (None, "000000", 0) and "msg" in data:
            raise ExchangeAPIError(
                self.name, f"{data.get('code')}: {data.get('msg')}", payload=data
            )
        return data

    # ---------------- 进账记录 ----------------
    def fetch_incoming(
        self, start_ms: int, end_ms: int, *, limit: int = 100
    ) -> list[Transaction]:
        params: dict[str, Any] = {"limit": min(max(limit, 1), 100)}
        # Binance 要求 startTime / endTime 间隔不超过 90 天
        if start_ms:
            params["startTime"] = int(start_ms)
        if end_ms:
            params["endTime"] = int(end_ms)

        data = self._get("/sapi/v1/pay/transactions", params)
        if isinstance(data, str):
            raise ExchangeAPIError(self.name, f"接口返回异常: {data}")
        if not isinstance(data, dict) or not data.get("success"):
            message = (data or {}).get("message") or "pay/transactions 返回 success=false"
            raise ExchangeAPIError(self.name, message, payload=data)

        records = data.get("data") or []
        out: list[Transaction] = []
        for record in records:
            tx = self._parse(record)
            if tx is not None:
                out.append(tx)
        return out

    def _parse(self, record: dict[str, Any]) -> Transaction | None:
        amount = to_decimal(record.get("amount"))
        # 负数是支出，直接跳过
        if amount is None or amount <= 0:
            return None

        order_type = (record.get("orderType") or "").upper()
        if order_type and order_type not in INCOMING_ORDER_TYPES:
            return None

        timestamp = to_ms(record.get("transactionTime"))
        if timestamp is None:
            return None

        tx_id = str(record.get("transactionId") or "").strip()
        if not tx_id:
            return None

        payer = record.get("payerInfo") or {}
        payer_name = (payer.get("name") or "").strip() or None
        payer_uid = str(payer.get("binanceId") or payer.get("accountId") or "").strip() or None

        # 备注字段在不同订单类型下命名不一致，逐个兜底
        memo = None
        for key in ("note", "remark", "orderNote", "description"):
            value = record.get(key)
            if isinstance(value, str) and value.strip():
                memo = value.strip()
                break

        return Transaction(
            exchange=self.name,
            tx_id=tx_id,
            amount=amount,
            currency=(record.get("currency") or "").upper(),
            timestamp_ms=timestamp,
            payer_name=payer_name,
            payer_uid=payer_uid,
            memo=memo,
            channel=order_type.lower() or "pay",
            raw=record,
        )

    # ---------------- 权限自检 ----------------
    def check_permissions(self) -> PermissionReport:
        try:
            data = self._get("/sapi/v1/account/apiRestrictions")
        except ExchangeAPIError as exc:
            return PermissionReport(
                exchange=self.name, ok=False, read_only=None, detail=str(exc)
            )

        write_flags = {
            "enableWithdrawals": "提币",
            "enableInternalTransfer": "内部划转",
            "enableSpotAndMarginTrading": "现货/杠杆交易",
            "enableFutures": "合约交易",
            "enableMargin": "杠杆",
            "enableVanillaOptions": "期权",
            "permitsUniversalTransfer": "万能划转",
        }
        granted = [label for key, label in write_flags.items() if data.get(key)]
        ip_restricted = bool(data.get("ipRestrict"))

        if granted:
            detail = "该 API Key 拥有写权限：" + "、".join(granted) + "。请改用纯只读 Key。"
            return PermissionReport(
                exchange=self.name,
                ok=True,
                read_only=False,
                detail=detail,
                permissions=granted,
                ip_restricted=ip_restricted,
            )

        detail = "只读 Key ✓"
        if not ip_restricted:
            detail += "（建议再加上 IP 白名单）"
        return PermissionReport(
            exchange=self.name,
            ok=True,
            read_only=True,
            detail=detail,
            permissions=["读取"],
            ip_restricted=ip_restricted,
        )

    def identifier_spec(self) -> IdentifierSpec:
        return IdentifierSpec(
            kind="payer_name",
            label="您的币安昵称",
            placeholder="例如 Ming*****Li",
            pattern=r"^.{1,64}$",
            help_text="币安 Pay 转账记录里会显示付款方昵称，填写后可自动核对。",
        )
