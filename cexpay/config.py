"""运行期配置与凭据存储。

设计原则：
  1. 凭据不进代码、不进仓库。默认落在 ``data/credentials.json``（0600 权限），
     并且 ``data/`` 已在 .gitignore 中。
  2. 环境变量优先级最高，方便容器化部署。
  3. 如果安装了 ``cryptography`` 且设置了 ``CEXPAY_MASTER_KEY``，
     凭据文件会用 Fernet 加密后落盘。
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import stat
import threading
from dataclasses import asdict, dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any

from .errors import ConfigError, CredentialError

SUPPORTED_EXCHANGES = ("binance", "okx", "bitget")

# 各交易所需要的凭据字段
CREDENTIAL_FIELDS: dict[str, tuple[str, ...]] = {
    "binance": ("api_key", "api_secret"),
    "okx": ("api_key", "api_secret", "passphrase"),
    "bitget": ("api_key", "api_secret", "passphrase"),
}

SECRET_FIELDS = ("api_secret", "passphrase")


def _env(name: str, default: str | None = None) -> str | None:
    value = os.environ.get(name)
    if value is None or value == "":
        return default
    return value


def _env_bool(name: str, default: bool) -> bool:
    raw = _env(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def _env_int(name: str, default: int) -> int:
    raw = _env(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError as exc:  # pragma: no cover - 配置错误直接暴露
        raise ConfigError(f"环境变量 {name} 需要是整数，收到 {raw!r}") from exc


def _env_decimal(name: str, default: str) -> Decimal:
    raw = _env(name, default)
    try:
        return Decimal(str(raw))
    except Exception as exc:  # pragma: no cover
        raise ConfigError(f"环境变量 {name} 需要是数字，收到 {raw!r}") from exc


@dataclass
class MatchPolicy:
    """订单核销策略。"""

    # 允许用户多付的额度（少付一律不通过）
    amount_tolerance: Decimal = Decimal("0.02")
    # 多付超过这个数额也不自动核销，避免把大额充值误配到小额订单上；None 表示不限制
    max_overpay: Decimal | None = Decimal("5")
    # 交易时间允许早于订单创建时间的秒数（用户先付款后下单）
    window_before_s: int = 1800
    # 订单创建后多久内的入账才算数
    window_after_s: int = 3600
    # 是否必须提供付款方标识（关掉后仅靠"唯一金额 + 时间窗"核销）
    require_identifier: bool = False
    # Binance 付款方昵称相似度阈值（0~1）
    name_similarity_threshold: float = 0.6
    # 只接受该币种入账
    currency: str = "USDT"
    # 同一个"唯一金额"被占用后的冷却时间（秒），避免用户晚付导致串单
    amount_cooldown_s: int = 86400
    # 是否启用备注码匹配（目前仅 Binance Pay 的转账备注支持）
    enable_memo_match: bool = True
    # 备注码长度
    memo_length: int = 6

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        for key in ("amount_tolerance", "max_overpay"):
            if data[key] is not None:
                data[key] = str(data[key])
        return data


@dataclass
class Settings:
    """全局设置，来自环境变量。"""

    data_dir: Path = field(default_factory=lambda: Path(_env("CEXPAY_DATA_DIR", "data")))
    host: str = field(default_factory=lambda: _env("CEXPAY_HOST", "127.0.0.1"))
    port: int = field(default_factory=lambda: _env_int("CEXPAY_PORT", 8787))
    admin_token: str | None = field(default_factory=lambda: _env("CEXPAY_ADMIN_TOKEN"))
    master_key: str | None = field(default_factory=lambda: _env("CEXPAY_MASTER_KEY"))
    # 订单默认有效期（秒）
    order_ttl_s: int = field(default_factory=lambda: _env_int("CEXPAY_ORDER_TTL", 1800))
    # 后台轮询间隔（秒）；0 表示不启动轮询，只走手动 /claim
    poll_interval_s: int = field(default_factory=lambda: _env_int("CEXPAY_POLL_INTERVAL", 20))
    # 是否给每笔订单分配唯一金额（推荐开启，能在无标识时自动核销）
    unique_amount: bool = field(default_factory=lambda: _env_bool("CEXPAY_UNIQUE_AMOUNT", True))
    # 唯一金额的小数位数：4 位小数 => 单一价位可并发 9999 笔订单
    unique_amount_decimals: int = field(
        default_factory=lambda: _env_int("CEXPAY_UNIQUE_AMOUNT_DECIMALS", 4)
    )
    # 回调签名密钥
    webhook_secret: str | None = field(default_factory=lambda: _env("CEXPAY_WEBHOOK_SECRET"))
    # 网络请求超时
    http_timeout_s: int = field(default_factory=lambda: _env_int("CEXPAY_HTTP_TIMEOUT", 15))
    # 启动时是否强制校验 API 为只读
    enforce_readonly: bool = field(
        default_factory=lambda: _env_bool("CEXPAY_ENFORCE_READONLY", True)
    )

    def __post_init__(self) -> None:
        self.data_dir = Path(self.data_dir).expanduser()
        self.data_dir.mkdir(parents=True, exist_ok=True)

    # --- 常用路径 ---
    @property
    def credentials_path(self) -> Path:
        return self.data_dir / "credentials.json"

    @property
    def db_path(self) -> Path:
        return self.data_dir / "cexpay.sqlite3"

    @property
    def qr_dir(self) -> Path:
        path = self.data_dir / "qr"
        path.mkdir(parents=True, exist_ok=True)
        return path

    @property
    def policy(self) -> MatchPolicy:
        return MatchPolicy(
            amount_tolerance=_env_decimal("CEXPAY_AMOUNT_TOLERANCE", "0.02"),
            max_overpay=_env_decimal("CEXPAY_MAX_OVERPAY", "5"),
            window_before_s=_env_int("CEXPAY_WINDOW_BEFORE", 1800),
            window_after_s=_env_int("CEXPAY_WINDOW_AFTER", 3600),
            require_identifier=_env_bool("CEXPAY_REQUIRE_IDENTIFIER", False),
            name_similarity_threshold=float(_env("CEXPAY_NAME_SIMILARITY", "0.6")),
            currency=(_env("CEXPAY_CURRENCY", "USDT") or "USDT").upper(),
            amount_cooldown_s=_env_int("CEXPAY_AMOUNT_COOLDOWN", 86400),
            enable_memo_match=_env_bool("CEXPAY_ENABLE_MEMO", True),
            memo_length=_env_int("CEXPAY_MEMO_LENGTH", 6),
        )


# --------------------------------------------------------------------------
# 凭据加密（可选）
# --------------------------------------------------------------------------
def _fernet(master_key: str):
    try:
        from cryptography.fernet import Fernet
    except ImportError as exc:  # pragma: no cover - 可选依赖
        raise ConfigError(
            "设置了 CEXPAY_MASTER_KEY 但没有安装 cryptography，"
            "请执行 pip install cryptography，或取消该环境变量"
        ) from exc
    # 允许用户填任意字符串，内部派生出合法的 32 字节 Fernet key
    digest = hashlib.sha256(master_key.encode("utf-8")).digest()
    return Fernet(base64.urlsafe_b64encode(digest))


@dataclass
class ExchangeCredential:
    exchange: str
    api_key: str = ""
    api_secret: str = ""
    passphrase: str = ""
    enabled: bool = True
    # 收款账号展示用信息（例如 Binance Pay ID / OKX UID）
    account_label: str = ""
    note: str = ""

    def missing_fields(self) -> list[str]:
        required = CREDENTIAL_FIELDS.get(self.exchange, ())
        return [f for f in required if not (getattr(self, f, "") or "").strip()]

    def is_complete(self) -> bool:
        return not self.missing_fields()

    def to_dict(self, *, redacted: bool = False) -> dict[str, Any]:
        data = asdict(self)
        if redacted:
            for key in ("api_key", *SECRET_FIELDS):
                data[key] = _redact(data.get(key, ""))
        return data


def _redact(value: str) -> str:
    if not value:
        return ""
    if len(value) <= 8:
        return "*" * len(value)
    return f"{value[:4]}{'*' * 6}{value[-4:]}"


class CredentialStore:
    """凭据的读写。环境变量 > 文件。"""

    def __init__(self, settings: Settings):
        self.settings = settings
        self._lock = threading.RLock()
        self._cache: dict[str, ExchangeCredential] | None = None

    # --- 环境变量形式：CEXPAY_BINANCE_API_KEY 等 ---
    def _from_env(self, exchange: str) -> dict[str, str]:
        prefix = f"CEXPAY_{exchange.upper()}_"
        out: dict[str, str] = {}
        for field_name in ("api_key", "api_secret", "passphrase", "account_label"):
            value = _env(prefix + field_name.upper())
            if value:
                out[field_name] = value
        return out

    def _read_file(self) -> dict[str, Any]:
        path = self.settings.credentials_path
        if not path.exists():
            return {}
        raw = path.read_bytes()
        if not raw.strip():
            return {}
        if self.settings.master_key:
            try:
                raw = _fernet(self.settings.master_key).decrypt(raw)
            except ConfigError:
                raise
            except Exception as exc:
                raise CredentialError(
                    f"解密 {path} 失败，请确认 CEXPAY_MASTER_KEY 是否与写入时一致"
                ) from exc
        try:
            return json.loads(raw.decode("utf-8"))
        except json.JSONDecodeError as exc:
            raise CredentialError(f"{path} 不是合法 JSON：{exc}") from exc

    def _write_file(self, payload: dict[str, Any]) -> None:
        path = self.settings.credentials_path
        blob = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
        if self.settings.master_key:
            blob = _fernet(self.settings.master_key).encrypt(blob)
        tmp = path.with_suffix(".tmp")
        tmp.write_bytes(blob)
        os.chmod(tmp, stat.S_IRUSR | stat.S_IWUSR)  # 0600
        tmp.replace(path)
        os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)

    def load(self, *, refresh: bool = False) -> dict[str, ExchangeCredential]:
        with self._lock:
            if self._cache is not None and not refresh:
                return self._cache
            stored = self._read_file()
            result: dict[str, ExchangeCredential] = {}
            for exchange in SUPPORTED_EXCHANGES:
                data = dict(stored.get(exchange) or {})
                data.update(self._from_env(exchange))  # 环境变量覆盖文件
                cred = ExchangeCredential(
                    exchange=exchange,
                    api_key=(data.get("api_key") or "").strip(),
                    api_secret=(data.get("api_secret") or "").strip(),
                    passphrase=(data.get("passphrase") or "").strip(),
                    enabled=bool(data.get("enabled", True)),
                    account_label=(data.get("account_label") or "").strip(),
                    note=(data.get("note") or "").strip(),
                )
                result[exchange] = cred
            self._cache = result
            return result

    def get(self, exchange: str) -> ExchangeCredential:
        exchange = exchange.lower()
        if exchange not in SUPPORTED_EXCHANGES:
            raise ConfigError(f"不支持的交易所：{exchange}")
        return self.load()[exchange]

    def save(self, exchange: str, **fields: Any) -> ExchangeCredential:
        exchange = exchange.lower()
        if exchange not in SUPPORTED_EXCHANGES:
            raise ConfigError(f"不支持的交易所：{exchange}")
        with self._lock:
            stored = self._read_file()
            entry = dict(stored.get(exchange) or {})
            for key, value in fields.items():
                if value is None:
                    continue
                if key in ("api_key", "api_secret", "passphrase", "account_label", "note"):
                    entry[key] = str(value).strip()
                elif key == "enabled":
                    entry[key] = bool(value)
            stored[exchange] = entry
            self._write_file(stored)
            self._cache = None
            return self.get(exchange)

    def delete(self, exchange: str) -> None:
        with self._lock:
            stored = self._read_file()
            stored.pop(exchange.lower(), None)
            self._write_file(stored)
            self._cache = None

    def configured(self) -> list[str]:
        return [
            name
            for name, cred in self.load().items()
            if cred.enabled and cred.is_complete()
        ]


_settings: Settings | None = None
_store: CredentialStore | None = None


def get_settings(*, refresh: bool = False) -> Settings:
    global _settings
    if _settings is None or refresh:
        _settings = Settings()
    return _settings


def get_credential_store(*, refresh: bool = False) -> CredentialStore:
    global _store
    if _store is None or refresh:
        _store = CredentialStore(get_settings(refresh=refresh))
    return _store
