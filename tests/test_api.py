"""HTTP 接口。用 TestClient 跑，不碰真实交易所。"""

import io

from PIL import Image

from cexpay.qr import render_qr

AUTH = {"Authorization": "Bearer test-token"}
BINANCE_URL = "https://app.binance.com/uni-qr/DEMO1234"
BITGET_URL = "https://www.bitget.com/pay/receive?qrAction=pay&uid=1000000165"


def qr_bytes(payload: str, size=400) -> bytes:
    page = Image.new("RGB", (700, 900), "#eeeeee")
    page.paste(render_qr(payload, size=size), (150, 200))
    buffer = io.BytesIO()
    page.save(buffer, format="PNG")
    return buffer.getvalue()


# ---------------------------------------------------------------- 基础
def test_health(client):
    body = client.get("/api/health").json()
    assert body["ok"] is True
    assert body["exchanges"] == []


def test_exchanges_empty_without_credentials(client):
    assert client.get("/api/exchanges").json()["exchanges"] == []


# ---------------------------------------------------------------- 订单
def test_create_and_fetch_order(client):
    res = client.post("/api/orders", json={"amount": "9.9"})
    assert res.status_code == 201
    order = res.json()["order"]
    assert order["status"] == "pending"
    assert order["base_amount"] == "9.9"
    assert order["pay_amount"] == "9.9001"     # 唯一金额尾数
    assert len(order["memo"]) == 6

    fetched = client.get(f"/api/orders/{order['order_id']}").json()["order"]
    assert fetched["order_id"] == order["order_id"]


def test_orders_get_distinct_amounts(client):
    amounts = {
        client.post("/api/orders", json={"amount": "9.9"}).json()["order"]["pay_amount"]
        for _ in range(5)
    }
    assert len(amounts) == 5


def test_merchant_ref_is_idempotent(client):
    first = client.post("/api/orders", json={"amount": "5", "merchant_ref": "SHOP-1"}).json()
    second = client.post("/api/orders", json={"amount": "5", "merchant_ref": "SHOP-1"}).json()
    assert first["order"]["order_id"] == second["order"]["order_id"]


def test_invalid_amount_rejected(client):
    assert client.post("/api/orders", json={"amount": "-1"}).status_code == 400
    assert client.post("/api/orders", json={"amount": "abc"}).status_code == 400


def test_unknown_exchange_rejected(client):
    res = client.post("/api/orders", json={"amount": "1", "exchange": "coinbase"})
    assert res.status_code == 400


def test_missing_order_is_404(client):
    assert client.get("/api/orders/nope").status_code == 404


def test_cancel_order(client):
    order_id = client.post("/api/orders", json={"amount": "1"}).json()["order"]["order_id"]
    res = client.post(f"/api/orders/{order_id}/cancel")
    assert res.json()["order"]["status"] == "cancelled"


def test_check_without_exchanges_reports_zero(client):
    order_id = client.post("/api/orders", json={"amount": "1"}).json()["order"]["order_id"]
    body = client.post(f"/api/orders/{order_id}/check").json()
    assert body["is_paid"] is False
    assert body["scanned"] == 0


def test_identifier_validation(client):
    order_id = client.post("/api/orders", json={"amount": "1"}).json()["order"]["order_id"]
    bad = client.post(f"/api/orders/{order_id}/identifier",
                      json={"kind": "payer_uid_last3", "value": "12"})
    assert bad.status_code == 400
    ok = client.post(f"/api/orders/{order_id}/identifier",
                     json={"kind": "payer_uid_last3", "value": "123"})
    assert ok.status_code == 200


def test_identifier_unknown_kind_rejected(client):
    order_id = client.post("/api/orders", json={"amount": "1"}).json()["order"]["order_id"]
    res = client.post(f"/api/orders/{order_id}/identifier",
                      json={"kind": "shoe_size", "value": "42"})
    assert res.status_code == 400


# ---------------------------------------------------------------- 鉴权
def test_admin_requires_token(client):
    assert client.get("/api/admin/credentials").status_code == 401
    assert client.get("/api/admin/orders").status_code == 401


def test_admin_rejects_wrong_token(client):
    res = client.get("/api/admin/credentials", headers={"Authorization": "Bearer nope"})
    assert res.status_code == 401


def test_admin_accepts_token(client):
    body = client.get("/api/admin/credentials", headers=AUTH).json()
    assert {c["exchange"] for c in body["credentials"]} == {"binance", "okx", "bitget"}


# ---------------------------------------------------------------- 凭据
def test_save_credential_redacts_secrets(client):
    res = client.put(
        "/api/admin/credentials/binance",
        headers=AUTH,
        json={"api_key": "K" * 40, "api_secret": "S" * 40, "account_label": "Pay 12345"},
    )
    body = res.json()
    assert body["complete"] is True
    assert "S" * 40 not in str(body)          # secret 不能原样回显
    assert body["credential"]["api_secret"].count("*") >= 6


def test_okx_credential_needs_passphrase(client):
    body = client.put(
        "/api/admin/credentials/okx",
        headers=AUTH,
        json={"api_key": "K" * 30, "api_secret": "S" * 30},
    ).json()
    assert body["complete"] is False
    assert "passphrase" in body["missing"]


def test_unknown_exchange_credential_404(client):
    res = client.put("/api/admin/credentials/kraken", headers=AUTH, json={"api_key": "x"})
    assert res.status_code == 404


# ---------------------------------------------------------------- 二维码
def test_upload_qr_auto_crops(client):
    res = client.post(
        "/api/admin/qr/binance",
        headers=AUTH,
        files={"file": ("shot.png", qr_bytes(BINANCE_URL), "image/png")},
    )
    body = res.json()
    assert res.status_code == 200
    assert body["payload"] == BINANCE_URL
    assert body["regenerated"] is True
    assert body["brand"] == "binance"

    # 存下来的图能取回，而且是 PNG
    got = client.get("/api/admin/qr/binance.png", headers=AUTH)
    assert got.status_code == 200
    assert got.headers["content-type"] == "image/png"


def test_upload_qr_warns_on_brand_mismatch(client):
    body = client.post(
        "/api/admin/qr/okx",
        headers=AUTH,
        files={"file": ("shot.png", qr_bytes(BITGET_URL), "image/png")},
    ).json()
    assert any("bitget" in w for w in body["warnings"])


def test_upload_qr_without_code_is_400(client):
    blank = io.BytesIO()
    Image.new("RGB", (300, 300), "white").save(blank, format="PNG")
    res = client.post(
        "/api/admin/qr/binance",
        headers=AUTH,
        files={"file": ("blank.png", blank.getvalue(), "image/png")},
    )
    assert res.status_code == 400


def test_upload_qr_empty_file_is_400(client):
    res = client.post(
        "/api/admin/qr/binance",
        headers=AUTH,
        files={"file": ("empty.png", b"", "image/png")},
    )
    assert res.status_code == 400


def test_qr_preview_reports_hits(client):
    body = client.post(
        "/api/admin/qr/binance/preview",
        headers=AUTH,
        files={"file": ("shot.png", qr_bytes(BINANCE_URL), "image/png")},
    ).json()
    assert body["count"] == 1
    assert body["hits"][0]["brand"] == "binance"


def test_delete_qr(client):
    client.post("/api/admin/qr/binance", headers=AUTH,
                files={"file": ("s.png", qr_bytes(BINANCE_URL), "image/png")})
    assert client.delete("/api/admin/qr/binance", headers=AUTH).status_code == 200
    assert client.get("/api/admin/qr/binance.png", headers=AUTH).status_code == 404


def test_aggregate_404_without_any_qr(client):
    assert client.get("/api/qr/aggregate.png").status_code == 404


def test_compose_route_is_not_shadowed_by_exchange_route(client):
    """/api/admin/qr/compose 不能被 /api/admin/qr/{exchange} 抢走。"""
    res = client.post("/api/admin/qr/compose", headers=AUTH, json={"layout": "row"})
    # 没有二维码时应该是 400 "没有可用的收款码"，而不是 404 "不支持的交易所"
    assert res.status_code == 400
    assert "收款码" in res.json()["detail"]


def test_compose_and_serve_aggregate(client):
    # 配两家凭据 + 两张码
    client.put("/api/admin/credentials/binance", headers=AUTH,
               json={"api_key": "K" * 40, "api_secret": "S" * 40, "account_label": "Pay 1"})
    client.put("/api/admin/credentials/bitget", headers=AUTH,
               json={"api_key": "K" * 40, "api_secret": "S" * 40,
                     "passphrase": "P" * 12, "account_label": "UID 2"})
    for exchange, url in (("binance", BINANCE_URL), ("bitget", BITGET_URL)):
        client.post(f"/api/admin/qr/{exchange}", headers=AUTH,
                    files={"file": ("s.png", qr_bytes(url), "image/png")})

    body = client.post("/api/admin/qr/compose", headers=AUTH,
                       json={"layout": "row", "qr_size": 420}).json()
    assert body["all_verified"] is True
    assert set(body["verified"]) == {"binance", "bitget"}

    image = client.get("/api/qr/aggregate.png?layout=row&size=420")
    assert image.status_code == 200
    assert image.headers["content-type"] == "image/png"

    # 收银台也应该能拿到聚合图
    order_id = client.post("/api/orders", json={"amount": "3"}).json()["order"]["order_id"]
    assert client.get(f"/api/orders/{order_id}/qr.png").status_code == 200
    assert client.get(f"/api/orders/{order_id}/qr.png?exchange=binance").status_code == 200
    assert client.get(f"/api/orders/{order_id}/qr.png?exchange=okx").status_code == 404


# ---------------------------------------------------------------- 后台订单
def test_admin_manual_settle(client):
    order_id = client.post("/api/orders", json={"amount": "7"}).json()["order"]["order_id"]
    res = client.post(f"/api/admin/orders/{order_id}/settle", headers=AUTH,
                      json={"exchange": "binance", "tx_id": "MANUAL-1", "note": "线下确认"})
    order = res.json()["order"]
    assert order["status"] == "paid"
    assert order["settlement"]["tier"] == 4

    # 同一笔流水不能再核销另一张单
    other = client.post("/api/orders", json={"amount": "7"}).json()["order"]["order_id"]
    dup = client.post(f"/api/admin/orders/{other}/settle", headers=AUTH,
                      json={"exchange": "binance", "tx_id": "MANUAL-1"})
    assert dup.status_code == 400


def test_admin_orders_listing_and_stats(client):
    client.post("/api/orders", json={"amount": "1"})
    body = client.get("/api/admin/orders", headers=AUTH).json()
    assert len(body["orders"]) == 1
    assert body["stats"]["by_status"]["pending"] == 1


def test_admin_sweep_runs_without_exchanges(client):
    client.post("/api/orders", json={"amount": "1"})
    body = client.post("/api/admin/sweep", headers=AUTH).json()
    assert body["checked"] == 1
    assert body["settled"] == []


def test_static_pages_served(client):
    for path in ("/", "/checkout", "/admin"):
        assert client.get(path).status_code == 200
