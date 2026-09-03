"""FastAPI 服务：收银台 + 商户 API + 后台配置。

路由分三组：
  /api/*        商户/收银台调用，公开
  /api/admin/*  后台，需要 Bearer Token（CEXPAY_ADMIN_TOKEN）
  /            静态页面（收银台 + 后台）
"""

from __future__ import annotations

import io
import secrets
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import __version__
from .config import SUPPORTED_EXCHANGES, get_credential_store, get_settings
from .errors import CexPayError, ConfigError, OrderError, QRError
from .gateway import PaymentGateway
from .logging_conf import setup_logging
from .poller import Poller
from .qr import Panel, compose, crop_qr, detect_qrcodes, load_image
from .store import STATUS_PAID

log = setup_logging()
WEB_DIR = Path(__file__).resolve().parent.parent / "web"


# --------------------------------------------------------------------------
# 请求模型
# --------------------------------------------------------------------------
class CreateOrderRequest(BaseModel):
    amount: str = Field(..., description="订单金额，例如 \"9.9\"")
    exchange: str | None = Field(None, description="指定交易所；留空表示任意")
    currency: str | None = None
    merchant_ref: str | None = Field(None, description="商户单号，用于幂等")
    callback_url: str | None = None
    ttl_s: int | None = Field(None, ge=60, le=86400)
    metadata: dict[str, Any] | None = None


class SubmitIdentifierRequest(BaseModel):
    kind: str
    value: str


class CredentialRequest(BaseModel):
    api_key: str | None = None
    api_secret: str | None = None
    passphrase: str | None = None
    account_label: str | None = None
    note: str | None = None
    enabled: bool | None = None


class ManualSettleRequest(BaseModel):
    exchange: str
    tx_id: str
    note: str = ""


class ComposeRequest(BaseModel):
    exchanges: list[str] | None = None
    layout: str = "row"
    qr_size: int = Field(520, ge=200, le=1200)
    gutter_ratio: float = Field(0.45, ge=0.1, le=2.0)
    title: str = "扫码支付 · 任选一家"
    footnote: str = "请使用对应交易所 App 扫描该品牌下方的二维码"


# --------------------------------------------------------------------------
# 应用
# --------------------------------------------------------------------------
def create_app(*, start_poller: bool = True) -> FastAPI:
    settings = get_settings()
    gateway = PaymentGateway(settings=settings)
    poller = Poller(gateway)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        configured = gateway.credentials.configured()
        if not configured:
            log.warning(
                "还没有配置任何交易所凭据。打开 /admin 或运行 `cexpay creds set` 完成配置。"
            )
        else:
            log.info("已配置的交易所：%s", "、".join(configured))
            try:
                gateway.assert_readonly()
            except ConfigError as exc:
                log.error("只读校验未通过：%s", exc)
                raise
        if start_poller:
            poller.start()
        try:
            yield
        finally:
            poller.stop()

    app = FastAPI(
        title="多交易所聚合支付 · multi-cex-pay",
        description="Binance / OKX / Bitget 个人收款自动核销网关",
        version=__version__,
        lifespan=lifespan,
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.state.gateway = gateway
    app.state.poller = poller

    # ------------------------- 鉴权 -------------------------
    def require_admin(request: Request) -> None:
        token = settings.admin_token
        if not token:
            raise HTTPException(
                status_code=503,
                detail="未设置 CEXPAY_ADMIN_TOKEN，后台接口已禁用。请先配置该环境变量。",
            )
        header = request.headers.get("authorization", "")
        provided = header[7:].strip() if header.lower().startswith("bearer ") else ""
        if not provided:
            provided = request.headers.get("x-admin-token", "")
        if not secrets.compare_digest(provided, token):
            raise HTTPException(status_code=401, detail="后台令牌不正确")

    # ------------------------- 异常 -------------------------
    @app.exception_handler(CexPayError)
    async def _cexpay_error(_: Request, exc: CexPayError):
        status = 400 if isinstance(exc, (OrderError, QRError, ConfigError)) else 502
        return JSONResponse(status_code=status, content={"detail": str(exc)})

    # ------------------------- 公共 -------------------------
    @app.get("/api/health")
    async def health():
        return {
            "ok": True,
            "version": __version__,
            "exchanges": gateway.credentials.configured(),
            "poller": poller.enabled,
        }

    @app.get("/api/exchanges")
    async def exchanges():
        """收银台用：有哪些可选的支付方式。"""
        return {"exchanges": gateway.exchange_info()}

    @app.post("/api/orders", status_code=201)
    async def create_order(body: CreateOrderRequest):
        order = gateway.create_order(
            body.amount,
            exchange=body.exchange,
            currency=body.currency,
            merchant_ref=body.merchant_ref,
            callback_url=body.callback_url,
            ttl_s=body.ttl_s,
            metadata=body.metadata,
        )
        return {
            "order": order.to_dict(),
            "checkout_url": f"/checkout?order_id={order.order_id}",
            "qr_url": f"/api/orders/{order.order_id}/qr.png",
        }

    @app.get("/api/orders/{order_id}")
    async def get_order(order_id: str):
        order = gateway.get_order(order_id)
        if order is None:
            raise HTTPException(status_code=404, detail="订单不存在")
        return {"order": order.to_dict()}

    @app.post("/api/orders/{order_id}/identifier")
    async def submit_identifier(order_id: str, body: SubmitIdentifierRequest):
        order = gateway.submit_identifier(order_id, body.kind, body.value)
        # 提交完立刻查一次，用户体验上就是"点了就出结果"
        gateway.sweep(order_id=order_id)
        order = gateway.get_order(order_id) or order
        return {"order": order.to_dict()}

    @app.post("/api/orders/{order_id}/check")
    async def check_order(order_id: str):
        """用户点"我已支付"时调用：立刻拉一次进账并尝试核销。"""
        order = gateway.get_order(order_id)
        if order is None:
            raise HTTPException(status_code=404, detail="订单不存在")
        result = gateway.sweep(order_id=order_id)
        order = gateway.get_order(order_id) or order
        return {
            "order": order.to_dict(),
            "is_paid": order.status == STATUS_PAID,
            "scanned": result["transactions"],
            "errors": result["errors"],
        }

    @app.post("/api/orders/{order_id}/cancel")
    async def cancel_order(order_id: str):
        order = gateway.store.cancel(order_id)
        if order is None:
            raise HTTPException(status_code=404, detail="订单不存在")
        gateway.store.release_amount(order.currency, order.pay_amount)
        return {"order": order.to_dict()}

    @app.get("/api/orders/{order_id}/qr.png")
    async def order_qr(
        order_id: str,
        exchange: str | None = Query(None, description="留空则返回聚合图"),
        layout: str = Query("row", pattern="^(row|column|grid)$"),
        size: int = Query(520, ge=200, le=1200),
    ):
        order = gateway.get_order(order_id)
        if order is None:
            raise HTTPException(status_code=404, detail="订单不存在")
        image = _build_qr_image(
            gateway,
            exchange=exchange or order.exchange,
            layout=layout,
            qr_size=size,
        )
        return _png_response(image)

    # ------------------------- 后台：凭据 -------------------------
    @app.get("/api/admin/credentials", dependencies=[Depends(require_admin)])
    async def list_credentials():
        store = get_credential_store(refresh=True)
        return {
            "credentials": [
                {
                    **cred.to_dict(redacted=True),
                    "complete": cred.is_complete(),
                    "missing": cred.missing_fields(),
                    "has_qr": (settings.qr_dir / f"{name}.png").exists(),
                }
                for name, cred in store.load(refresh=True).items()
            ],
            "encrypted": bool(settings.master_key),
        }

    @app.put("/api/admin/credentials/{exchange}", dependencies=[Depends(require_admin)])
    async def save_credential(exchange: str, body: CredentialRequest):
        if exchange not in SUPPORTED_EXCHANGES:
            raise HTTPException(status_code=404, detail=f"不支持的交易所：{exchange}")
        store = get_credential_store()
        cred = store.save(exchange, **body.model_dump(exclude_none=True))
        return {
            "credential": cred.to_dict(redacted=True),
            "complete": cred.is_complete(),
            "missing": cred.missing_fields(),
        }

    @app.delete("/api/admin/credentials/{exchange}", dependencies=[Depends(require_admin)])
    async def delete_credential(exchange: str):
        get_credential_store().delete(exchange)
        return {"ok": True}

    @app.post("/api/admin/credentials/test", dependencies=[Depends(require_admin)])
    async def test_credentials(exchange: str | None = Query(None)):
        """连通性 + 只读权限自检。"""
        return {"reports": gateway.check_permissions([exchange] if exchange else None)}

    # ------------------------- 后台：二维码 -------------------------
    # 注意：/qr/compose 必须注册在 /qr/{exchange} 之前，
    # 否则 FastAPI 会按注册顺序把 compose 当成交易所名字匹配进去。
    @app.post("/api/admin/qr/compose", dependencies=[Depends(require_admin)])
    async def compose_qr(body: ComposeRequest):
        """生成聚合图并返回校验结果（不返回图片本身，图片走 GET）。"""
        panels, missing = _collect_panels(gateway, body.exchanges)
        if not panels:
            raise HTTPException(
                status_code=400,
                detail="没有可用的收款码，请先在后台上传各交易所的二维码",
            )
        result = compose(
            panels,
            layout=body.layout,
            qr_size=body.qr_size,
            gutter_ratio=body.gutter_ratio,
            title=body.title,
            footnote=body.footnote,
        )
        target = settings.qr_dir / "aggregate.png"
        result.save(target)
        payload = result.to_dict()
        payload["missing"] = missing
        payload["saved_to"] = str(target)
        return payload

    @app.post("/api/admin/qr/{exchange}", dependencies=[Depends(require_admin)])
    async def upload_qr(
        exchange: str,
        file: UploadFile = File(..., description="收款码截图，会自动识别并裁剪"),
        regenerate: bool = Form(True),
        size: int = Form(640),
    ):
        """上传收款码截图 → 自动定位二维码 → 透视校正 → 重绘 → 落盘。"""
        if exchange not in SUPPORTED_EXCHANGES:
            raise HTTPException(status_code=404, detail=f"不支持的交易所：{exchange}")

        data = await file.read()
        if not data:
            raise HTTPException(status_code=400, detail="上传的文件是空的")

        result = crop_qr(data, size=size, regenerate=regenerate)
        target = settings.qr_dir / f"{exchange}.png"
        result.save(target)

        from .qr.detect import guess_brand

        brand = guess_brand(result.payload or "")
        warnings = list(result.warnings)
        if brand and brand != exchange:
            warnings.append(
                f"注意：这张码看起来是 {brand} 的，但你把它配置到了 {exchange}。"
            )
        elif brand is None and result.payload:
            warnings.append("无法从二维码内容判断所属交易所，请自行确认没传错。")

        return {
            "exchange": exchange,
            "saved_to": str(target),
            **result.to_dict(),
            "warnings": warnings,
        }

    @app.post("/api/admin/qr/{exchange}/preview", dependencies=[Depends(require_admin)])
    async def preview_qr(exchange: str, file: UploadFile = File(...)):
        """只识别不保存，用于上传前预览。"""
        data = await file.read()
        hits = detect_qrcodes(data)
        image = load_image(data)
        return {
            "exchange": exchange,
            "image_size": list(image.size),
            "count": len(hits),
            "hits": [h.to_dict() for h in hits],
        }

    @app.get("/api/admin/qr/{exchange}.png", dependencies=[Depends(require_admin)])
    async def get_qr(exchange: str):
        path = settings.qr_dir / f"{exchange}.png"
        if not path.exists():
            raise HTTPException(status_code=404, detail=f"{exchange} 还没有配置收款码")
        return FileResponse(path, media_type="image/png")

    @app.delete("/api/admin/qr/{exchange}", dependencies=[Depends(require_admin)])
    async def delete_qr(exchange: str):
        path = settings.qr_dir / f"{exchange}.png"
        if path.exists():
            path.unlink()
        return {"ok": True}

    @app.get("/api/qr/aggregate.png")
    async def aggregate_qr(
        layout: str = Query("row", pattern="^(row|column|grid)$"),
        size: int = Query(520, ge=200, le=1200),
        exchanges: str | None = Query(None, description="逗号分隔，留空表示全部"),
    ):
        """公开的聚合收款图，可以直接 <img src> 引用或打印张贴。"""
        wanted = (
            [e.strip() for e in exchanges.split(",") if e.strip()] if exchanges else None
        )
        image = _build_qr_image(gateway, exchange=None, layout=layout, qr_size=size, only=wanted)
        return _png_response(image)

    # ------------------------- 后台：订单 -------------------------
    @app.get("/api/admin/orders", dependencies=[Depends(require_admin)])
    async def admin_orders(
        status: str | None = Query(None),
        limit: int = Query(50, ge=1, le=500),
        offset: int = Query(0, ge=0),
    ):
        orders = gateway.store.list(status=status, limit=limit, offset=offset)
        return {
            "orders": [o.to_dict(public=False) for o in orders],
            "stats": gateway.stats(),
        }

    @app.post("/api/admin/orders/{order_id}/settle", dependencies=[Depends(require_admin)])
    async def admin_settle(order_id: str, body: ManualSettleRequest):
        order = gateway.manual_settle(
            order_id, exchange=body.exchange, tx_id=body.tx_id, note=body.note
        )
        return {"order": order.to_dict(public=False)}

    @app.post("/api/admin/sweep", dependencies=[Depends(require_admin)])
    async def admin_sweep():
        return gateway.sweep()

    @app.get("/api/admin/transactions", dependencies=[Depends(require_admin)])
    async def admin_transactions(
        minutes: int = Query(120, ge=1, le=60 * 24 * 30),
        exchange: str | None = Query(None),
    ):
        """看看各所最近都收到了什么钱，排查用。"""
        from .store import now_ms

        end = now_ms()
        start = end - minutes * 60 * 1000
        txs, errors = gateway.fetch_transactions(
            start, end, exchanges=[exchange] if exchange else None
        )
        txs.sort(key=lambda t: t.timestamp_ms, reverse=True)
        return {
            "transactions": [t.to_dict() for t in txs],
            "errors": errors,
            "window": {"start_ms": start, "end_ms": end},
        }

    # ------------------------- 静态页 -------------------------
    if WEB_DIR.exists():
        app.mount("/static", StaticFiles(directory=str(WEB_DIR)), name="static")

        @app.get("/")
        async def index():
            return FileResponse(WEB_DIR / "index.html")

        @app.get("/checkout")
        async def checkout():
            return FileResponse(WEB_DIR / "checkout.html")

        @app.get("/admin")
        async def admin_page():
            return FileResponse(WEB_DIR / "admin.html")

    return app


# --------------------------------------------------------------------------
# 辅助
# --------------------------------------------------------------------------
def _png_response(image) -> Response:
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return Response(
        content=buffer.getvalue(),
        media_type="image/png",
        headers={"Cache-Control": "no-store"},
    )


def _collect_panels(gateway: PaymentGateway, only: list[str] | None = None):
    """从已保存的收款码里组装 Panel 列表。"""
    settings = gateway.settings
    panels: list[Panel] = []
    missing: list[str] = []
    for adapter in gateway.adapters():
        if only and adapter.name not in only:
            continue
        path = settings.qr_dir / f"{adapter.name}.png"
        label = adapter.credential.account_label
        if path.exists():
            panels.append(
                Panel.from_source(
                    adapter.name,
                    path,
                    subtitle=label,
                    title=adapter.display_name,
                )
            )
        elif label:
            # 没上传二维码但填了收款账号，至少把账号展示出来
            missing.append(adapter.name)
        else:
            missing.append(adapter.name)
    return panels, missing


def _build_qr_image(
    gateway: PaymentGateway,
    *,
    exchange: str | None,
    layout: str,
    qr_size: int,
    only: list[str] | None = None,
):
    settings = gateway.settings
    if exchange:
        path = settings.qr_dir / f"{exchange}.png"
        if not path.exists():
            raise HTTPException(status_code=404, detail=f"{exchange} 还没有配置收款码")
        return load_image(path)

    panels, _ = _collect_panels(gateway, only)
    if not panels:
        raise HTTPException(
            status_code=404, detail="还没有配置任何收款码，请先在 /admin 上传"
        )
    if len(panels) == 1:
        return panels[0].resolve_image(qr_size)
    return compose(panels, layout=layout, qr_size=qr_size, verify=False).image


app = None  # 由 cexpay.cli / uvicorn 工厂创建


def get_app() -> FastAPI:
    """uvicorn 入口：``uvicorn cexpay.server:get_app --factory``"""
    return create_app()
