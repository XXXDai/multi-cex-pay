import shutil
import tempfile

import pytest


@pytest.fixture()
def data_dir(monkeypatch):
    """每个测试一个干净的数据目录。"""
    path = tempfile.mkdtemp(prefix="cexpay-test-")
    monkeypatch.setenv("CEXPAY_DATA_DIR", path)
    monkeypatch.setenv("CEXPAY_ADMIN_TOKEN", "test-token")
    monkeypatch.setenv("CEXPAY_POLL_INTERVAL", "0")
    # 清掉可能污染测试的凭据环境变量
    for name in ("BINANCE", "OKX", "BITGET"):
        for field in ("API_KEY", "API_SECRET", "PASSPHRASE"):
            monkeypatch.delenv(f"CEXPAY_{name}_{field}", raising=False)
    import cexpay.config as config
    config._settings = None
    config._store = None
    yield path
    shutil.rmtree(path, ignore_errors=True)
    config._settings = None
    config._store = None


@pytest.fixture()
def gateway(data_dir):
    from cexpay.gateway import PaymentGateway
    return PaymentGateway()


@pytest.fixture()
def client(data_dir):
    from fastapi.testclient import TestClient

    from cexpay.server import create_app
    with TestClient(create_app(start_poller=False)) as c:
        yield c
