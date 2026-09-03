"""OKX 适配器。

数据来源：``GET /api/v5/asset/deposit-history``
  - ``type=3`` 是内部转账（OKX 用户之间互转，也就是"OKX Pay/闪电到账"）
  - ``type=4`` 是链上充值
权限自检：``GET /api/v5/account/config`` 返回的 ``perm`` 字段

OKX 的内部转账拿不到付款方昵称，但会带上付款方的"提币申请 ID"(fromWdId)，
所以人工核对时让用户提供该 ID 的后三位。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
from datetime import datetime, timezone
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

BASE_URL = "https://www.okx.com"

# 充值状态：2 = 充值成功
STATE_SUCCESS = {"2"}

STATE_LABELS = {
    "0": "等待确认",
    "1": "确认到账",
    "2": "充值成功",
    "8": "暂停充值未到账",
    "11": "命中黑名单",
    "12": "账户或充值被冻结",
    "13": "子账户充值拦截",
    "14": "KYC 限额",
    "17": "等待国际转账规则认证",
}


class OKXAdapter(ExchangeAdapter):
    name = "okx"
    display_name = "OKX"
    brand_color = "#000000"
    supports_memo = False
    pay_hint = "打开 OKX App → 资产 → 转账/扫一扫，向收款 UID 内部转账（免手续费、秒到）"

    # ---------------- 签名 ----------------
    def _headers(self, method: str, request_path: str, body: str = "") -> dict[str, str]:
        timestamp = (
            datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
        )
        message = timestamp + method.upper() + request_path + body
        mac = hmac.new(
            self.credential.api_secret.encode("utf-8"),
            message.encode("utf-8"),
            hashlib.sha256,
        )
        signature = base64.b64encode(mac.digest()).decode("utf-8")
        return {
            "OK-ACCESS-KEY": self.credential.api_key,
            "OK-ACCESS-SIGN": signature,
            "OK-ACCESS-TIMESTAMP": timestamp,
            "OK-ACCESS-PASSPHRASE": self.credential.passphrase,
            "Content-Type": "application/json",
        }

    def _get(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        # OKX 的签名必须包含 query string，所以要自己拼好 request_path
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
        if data.get("code") != "0":
            raise ExchangeAPIError(
                self.name, f"{data.get('code')}: {data.get('msg')}", payload=data
            )
        return data

    # ---------------- 进账记录 ----------------
    def fetch_incoming(
        self, start_ms: int, end_ms: int, *, limit: int = 100
    ) -> list[Transaction]:
        currency = self.settings.policy.currency
        # OKX 语义：after = 查询在此时间之前的数据，before = 在此之后
        params = {
            "ccy": currency,
            "after": str(int(end_ms)) if end_ms else "",
            "before": str(int(start_ms)) if start_ms else "",
            "limit": str(min(max(limit, 1), 100)),
        }
        data = self._get("/api/v5/asset/deposit-history", params)

        out: list[Transaction] = []
        for record in data.get("data") or []:
            tx = self._parse(record)
            if tx is not None:
                out.append(tx)
        return out

    def _parse(self, record: dict[str, Any]) -> Transaction | None:
        if str(record.get("state")) not in STATE_SUCCESS:
            return None

        amount = to_decimal(record.get("amt"))
        if amount is None or amount <= 0:
            return None

        timestamp = to_ms(record.get("ts"))
        if timestamp is None:
            return None

        tx_id = str(record.get("depId") or record.get("txId") or "").strip()
        if not tx_id:
            return None

        from_wd_id = str(record.get("fromWdId") or "").strip() or None
        # 有 fromWdId 说明是站内转账，否则是链上充值
        channel = "internal" if from_wd_id else "on_chain"

        return Transaction(
            exchange=self.name,
            tx_id=tx_id,
            amount=amount,
            currency=(record.get("ccy") or "").upper(),
            timestamp_ms=timestamp,
            withdraw_id=from_wd_id,
            # OKX 内部转账的 from 是付款方账号（手机号/邮箱脱敏值）
            # 站内转账时 from 是付款方账号（脱敏后的手机号/邮箱）；链上充值时没有这个语义
            payer_name=(
                (str(record.get("from") or "").strip() or None)
                if channel == "internal"
                else None
            ),
            channel=channel,
            raw=record,
        )

    # ---------------- 权限自检 ----------------
    def check_permissions(self) -> PermissionReport:
        try:
            data = self._get("/api/v5/account/config")
        except ExchangeAPIError as exc:
            return PermissionReport(
                exchange=self.name, ok=False, read_only=None, detail=str(exc)
            )

        entries = data.get("data") or []
        if not entries:
            return PermissionReport(
                exchange=self.name,
                ok=True,
                read_only=None,
                detail="接口没有返回权限信息，无法判定，请自行确认 Key 为只读。",
            )

        config = entries[0]
        perm_raw = str(config.get("perm") or "")
        perms = [p.strip() for p in perm_raw.split(",") if p.strip()]
        account_label = str(config.get("uid") or "")
        ip_restricted = bool(str(config.get("ip") or "").strip())

        write_perms = [p for p in perms if p.lower() in ("trade", "withdraw")]
        if write_perms:
            return PermissionReport(
                exchange=self.name,
                ok=True,
                read_only=False,
                detail="该 API Key 拥有写权限：" + "、".join(write_perms) + "。请改用只读 Key。",
                permissions=perms,
                ip_restricted=ip_restricted,
                account_label=account_label,
            )

        if not perms:
            return PermissionReport(
                exchange=self.name,
                ok=True,
                read_only=None,
                detail="未读到 perm 字段，无法判定权限。",
                account_label=account_label,
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
            permissions=perms,
            ip_restricted=ip_restricted,
            account_label=account_label,
        )

    def identifier_spec(self) -> IdentifierSpec:
        return IdentifierSpec(
            kind="withdraw_id_last3",
            label="提币申请 ID 后三位",
            placeholder="例如 728",
            pattern=r"^\d{3}$",
            help_text="OKX App → 资产 → 账单/提币记录，打开这笔转账即可看到提币申请 ID。",
        )
