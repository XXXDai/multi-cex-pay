"""Bitget 适配器。

数据来源：``GET /api/v2/spot/wallet/deposit-records``
权限自检：``GET /api/v2/spot/account/info``（返回 ``authorities``）

Bitget 的内部转账会把付款方 UID 放在 ``fromAddress``（纯数字），
所以人工核对时让用户提供 UID 后三位。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import time
from typing import Any

from ..errors import ExchangeAPIError
from .base import (
    ExchangeAdapter,
    IdentifierSpec,
    PermissionReport,
    Transaction,
    to_decimal,
    to_ms,
)

BASE_URL = "https://api.bitget.com"
OK_CODE = "00000"


class BitgetAdapter(ExchangeAdapter):
    name = "bitget"
    display_name = "Bitget"
    brand_color = "#00F0FF"
    supports_memo = False
    pay_hint = "打开 Bitget App → 资产 → 扫一扫/内部转账，向收款 UID 转账（免手续费、秒到）"

    # ---------------- 签名 ----------------
    def _headers(self, method: str, request_path: str, body: str = "") -> dict[str, str]:
        timestamp = str(int(time.time() * 1000))
        message = timestamp + method.upper() + request_path + body
        mac = hmac.new(
            self.credential.api_secret.encode("utf-8"),
            message.encode("utf-8"),
            hashlib.sha256,
        )
        signature = base64.b64encode(mac.digest()).decode("utf-8")
        return {
            "ACCESS-KEY": self.credential.api_key,
            "ACCESS-SIGN": signature,
            "ACCESS-TIMESTAMP": timestamp,
            "ACCESS-PASSPHRASE": self.credential.passphrase,
            "Content-Type": "application/json",
            "locale": "zh-CN",
        }

    def _get(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        request_path = path
        if params:
            pairs = [f"{k}={v}" for k, v in params.items() if v not in (None, "")]
            if pairs:
                request_path += "?" + "&".join(pairs)

        data = self._request(
            "GET", BASE_URL + request_path, headers=self._headers("GET", request_path)
        )
        if not isinstance(data, dict):
            raise ExchangeAPIError(self.name, f"返回体格式异常: {data!r}")
        if data.get("code") != OK_CODE:
            raise ExchangeAPIError(
                self.name, f"{data.get('code')}: {data.get('msg')}", payload=data
            )
        return data

    # ---------------- 进账记录 ----------------
    def fetch_incoming(
        self, start_ms: int, end_ms: int, *, limit: int = 100
    ) -> list[Transaction]:
        # Bitget 不传时间范围会直接报 400172，所以这两个参数必须给全
        params = {
            "coin": self.settings.policy.currency.upper(),
            "startTime": str(int(start_ms)),
            "endTime": str(int(end_ms)),
            "limit": str(min(max(limit, 1), 100)),
        }
        data = self._get("/api/v2/spot/wallet/deposit-records", params)

        out: list[Transaction] = []
        for record in data.get("data") or []:
            tx = self._parse(record)
            if tx is not None:
                out.append(tx)
        return out

    def _parse(self, record: dict[str, Any]) -> Transaction | None:
        if (record.get("status") or "").lower() != "success":
            return None

        amount = to_decimal(record.get("size"))
        if amount is None or amount <= 0:
            return None

        timestamp = to_ms(record.get("cTime"))
        if timestamp is None:
            return None

        tx_id = str(record.get("orderId") or record.get("tradeId") or "").strip()
        if not tx_id:
            return None

        from_address = str(record.get("fromAddress") or "").strip()
        # 内部转账时 fromAddress 是纯数字 UID；链上充值时是钱包地址
        payer_uid = from_address if from_address.isdigit() else None
        dest = (record.get("dest") or "").lower()
        channel = "internal" if dest == "internal" or payer_uid else "on_chain"

        return Transaction(
            exchange=self.name,
            tx_id=tx_id,
            amount=amount,
            currency=(record.get("coin") or "").upper(),
            timestamp_ms=timestamp,
            payer_uid=payer_uid,
            channel=channel,
            raw=record,
        )

    # ---------------- 权限自检 ----------------
    def check_permissions(self) -> PermissionReport:
        try:
            data = self._get("/api/v2/spot/account/info")
        except ExchangeAPIError as exc:
            return PermissionReport(
                exchange=self.name, ok=False, read_only=None, detail=str(exc)
            )

        info = data.get("data") or {}
        if isinstance(info, list):
            info = info[0] if info else {}

        account_label = str(info.get("userId") or "")
        ips = str(info.get("ips") or "").strip()
        authorities = info.get("authorities") or []
        if isinstance(authorities, str):
            authorities = [a.strip() for a in authorities.split(",") if a.strip()]
        perms = [str(a) for a in authorities]
        lowered = [p.lower() for p in perms]

        write_perms = [
            p for p in perms if p.lower() in ("trade", "withdraw", "transfer", "spot_trade")
        ]
        if write_perms:
            return PermissionReport(
                exchange=self.name,
                ok=True,
                read_only=False,
                detail="该 API Key 拥有写权限：" + "、".join(write_perms) + "。请改用只读 Key。",
                permissions=perms,
                ip_restricted=bool(ips),
                account_label=account_label,
            )

        if not perms:
            return PermissionReport(
                exchange=self.name,
                ok=True,
                read_only=None,
                detail="未读到 authorities 字段，无法判定权限，请自行确认 Key 为只读。",
                ip_restricted=bool(ips),
                account_label=account_label,
            )

        read_only = any(p in ("readonly", "read_only", "read") for p in lowered)
        if read_only:
            detail = "只读 Key ✓"
        else:
            detail = "未发现写权限，但权限字段不在已知列表内：" + "、".join(perms)
        if read_only and not ips:
            detail += "（建议再加上 IP 白名单）"
        return PermissionReport(
            exchange=self.name,
            ok=True,
            read_only=True if read_only else None,
            detail=detail,
            permissions=perms,
            ip_restricted=bool(ips),
            account_label=account_label,
        )

    def identifier_spec(self) -> IdentifierSpec:
        return IdentifierSpec(
            kind="payer_uid_last3",
            label="您的 Bitget UID 后三位",
            placeholder="例如 165",
            pattern=r"^\d{3}$",
            help_text="Bitget App 首页点击头像即可看到 UID。",
        )
