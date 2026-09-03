"""确保各语言 SDK 的验签结果和服务端签名一致。

签名口径：hex(HMAC-SHA256(secret, f"{timestamp}.{raw_body}"))
只要哪个 SDK 改坏了，这里就会红。
"""

import json
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

from cexpay.notify import sign_payload

SDK_DIR = Path(__file__).resolve().parent.parent / "sdk"
SECRET = "s3cret-key"
BODY = json.dumps({"event": "order.paid", "order": {"order_id": "abc"}},
                  ensure_ascii=False, separators=(",", ":"))


@pytest.fixture(scope="module")
def signed():
    stamp = int(time.time())
    return stamp, sign_payload(SECRET, stamp, BODY)


def test_python_sdk_matches_server(signed):
    sys.path.insert(0, str(SDK_DIR / "python"))
    try:
        from cexpay_client import CexPayClient
    finally:
        sys.path.pop(0)

    stamp, signature = signed
    client = CexPayClient("http://localhost", webhook_secret=SECRET)
    assert client.verify_webhook(BODY.encode(), stamp, signature)
    assert not client.verify_webhook(BODY.encode(), stamp, "0" * 64)
    assert not client.verify_webhook(b'{"tampered":1}', stamp, signature)
    # 时间戳太旧要拒（防重放）
    old_sig = sign_payload(SECRET, stamp - 9999, BODY)
    assert not client.verify_webhook(BODY.encode(), stamp - 9999, old_sig)


@pytest.mark.skipif(not shutil.which("node"), reason="需要 node")
def test_node_sdk_matches_server(signed, tmp_path):
    stamp, signature = signed
    script = tmp_path / "check.mjs"
    script.write_text(f"""
import {{ CexPayClient }} from {json.dumps(str(SDK_DIR / "node" / "cexpay.mjs"))};
const c = new CexPayClient("http://localhost", {{ webhookSecret: {json.dumps(SECRET)} }});
const body = {json.dumps(BODY)};
const out = {{
  valid: c.verifyWebhook(body, "{stamp}", {json.dumps(signature)}),
  badSig: c.verifyWebhook(body, "{stamp}", "0".repeat(64)),
  tampered: c.verifyWebhook('{{"tampered":1}}', "{stamp}", {json.dumps(signature)}),
}};
console.log(JSON.stringify(out));
""", encoding="utf-8")

    proc = subprocess.run(
        ["node", str(script)], capture_output=True, text=True, timeout=30
    )
    assert proc.returncode == 0, proc.stderr
    result = json.loads(proc.stdout.strip().splitlines()[-1])
    assert result == {"valid": True, "badSig": False, "tampered": False}


@pytest.mark.skipif(not shutil.which("go"), reason="需要 go")
def test_go_sdk_matches_server(signed, tmp_path):
    stamp, signature = signed
    module = tmp_path / "gocheck"
    module.mkdir()
    (module / "go.mod").write_text(
        "module gocheck\n\ngo 1.21\n\nrequire cexpay v0.0.0\n"
        "replace cexpay => " + str(SDK_DIR / "go") + "\n",
        encoding="utf-8",
    )
    (module / "main.go").write_text(f"""
package main

import (
	"fmt"
	"cexpay"
)

func main() {{
	c := cexpay.New("http://localhost", cexpay.Options{{WebhookSecret: {json.dumps(SECRET)}}})
	body := []byte({json.dumps(BODY)})
	fmt.Printf("%v %v %v\\n",
		c.VerifyWebhook(body, "{stamp}", {json.dumps(signature)}),
		c.VerifyWebhook(body, "{stamp}", "{'0' * 64}"),
		c.VerifyWebhook([]byte(`{{"tampered":1}}`), "{stamp}", {json.dumps(signature)}),
	)
}}
""", encoding="utf-8")

    proc = subprocess.run(
        ["go", "run", "."], cwd=module, capture_output=True, text=True, timeout=180
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip().splitlines()[-1] == "true false false"


@pytest.mark.skipif(not shutil.which("php"), reason="需要 php")
def test_php_sdk_matches_server(signed, tmp_path):
    stamp, signature = signed
    script = tmp_path / "check.php"
    script.write_text(f"""<?php
require {json.dumps(str(SDK_DIR / "php" / "CexPayClient.php"))};
$c = new CexPayClient('http://localhost', ['webhook_secret' => {json.dumps(SECRET)}]);
$body = {json.dumps(BODY)};
echo json_encode([
    'valid' => $c->verifyWebhook($body, '{stamp}', {json.dumps(signature)}),
    'badSig' => $c->verifyWebhook($body, '{stamp}', str_repeat('0', 64)),
    'tampered' => $c->verifyWebhook('{{"tampered":1}}', '{stamp}', {json.dumps(signature)}),
]);
""", encoding="utf-8")

    proc = subprocess.run(
        ["php", str(script)], capture_output=True, text=True, timeout=30
    )
    assert proc.returncode == 0, proc.stderr
    result = json.loads(proc.stdout.strip().splitlines()[-1])
    assert result == {"valid": True, "badSig": False, "tampered": False}
