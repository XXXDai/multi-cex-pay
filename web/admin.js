/* 后台逻辑：凭据配置、二维码上传裁剪、聚合图生成、订单与进账排查 */
(function () {
  "use strict";

  const $ = (id) => document.getElementById(id);
  const EXCHANGES = [
    { name: "binance", title: "Binance Pay", needsPassphrase: false },
    { name: "okx",     title: "OKX",         needsPassphrase: true },
    { name: "bitget",  title: "Bitget",      needsPassphrase: true },
  ];

  let token = "";

  /* ---------- 主题 ---------- */
  const savedTheme = safeGet("cexpay-theme");
  if (savedTheme) document.documentElement.dataset.theme = savedTheme;
  $("themeBtn").onclick = () => {
    const root = document.documentElement;
    const next = root.dataset.theme === "dark" ? "light" : "dark";
    root.dataset.theme = next;
    safeSet("cexpay-theme", next);
  };
  function safeGet(k) { try { return localStorage.getItem(k); } catch (e) { return null; } }
  function safeSet(k, v) { try { localStorage.setItem(k, v); } catch (e) {} }

  /* ---------- 工具 ---------- */
  function esc(s) {
    return String(s == null ? "" : s).replace(/[&<>"']/g, (c) =>
      ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  }

  function setMsg(el, text, kind) {
    if (typeof el === "string") el = $(el);
    el.textContent = text || "";
    el.className = "msg" + (kind ? " " + kind : "");
  }

  async function api(path, options) {
    options = options || {};
    const headers = Object.assign({ Authorization: "Bearer " + token }, options.headers || {});
    if (options.body && !(options.body instanceof FormData)) {
      headers["Content-Type"] = "application/json";
    }
    const res = await fetch(path, Object.assign({}, options, { headers }));
    const text = await res.text();
    let data = {};
    try { data = text ? JSON.parse(text) : {}; } catch (e) { data = { detail: text }; }
    if (!res.ok) throw new Error(data.detail || `HTTP ${res.status}`);
    return data;
  }

  function fmtTime(ms) {
    if (!ms) return "-";
    const d = new Date(ms);
    const p = (n) => String(n).padStart(2, "0");
    return `${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`;
  }

  const STATUS_PILL = {
    paid: ["ok", "已支付"], pending: ["", "待支付"],
    expired: ["warn", "已过期"], cancelled: ["", "已取消"],
  };

  /* ---------- 登录 ---------- */
  // 用 form submit 而不是按钮 click：Enter 提交由浏览器原生处理，不依赖 keydown
  $("authForm").addEventListener("submit", (e) => {
    e.preventDefault();
    login($("tokenInput").value.trim());
  });

  async function login(value) {
    if (!value) { setMsg("authMsg", "请填写令牌", "warn"); return; }
    token = value;
    try {
      await api("/api/admin/credentials");
      safeSet("cexpay-admin-token", value);
      $("authCard").style.display = "none";
      $("panel").style.display = "";
      await Promise.all([loadCredentials(), loadOrders()]);
      renderQRCards();
    } catch (err) {
      token = "";
      setMsg("authMsg", err.message, "err");
    }
  }

  /* ---------- 1. 凭据 ---------- */
  async function loadCredentials() {
    const data = await api("/api/admin/credentials");
    $("encPill").textContent = data.encrypted ? "凭据已加密落盘" : "凭据未加密";
    $("encPill").className = "pill " + (data.encrypted ? "ok" : "warn");

    const byName = {};
    (data.credentials || []).forEach((c) => { byName[c.exchange] = c; });

    const host = $("credCards");
    host.innerHTML = "";
    EXCHANGES.forEach((meta) => {
      const cred = byName[meta.name] || {};
      const el = document.createElement("div");
      el.className = "card";
      el.style.margin = "0";
      el.innerHTML = `
        <div class="row">
          <strong>${esc(meta.title)}</strong>
          <span class="spacer"></span>
          <span class="pill ${cred.complete ? "ok" : ""}" id="st-${meta.name}">
            ${cred.complete ? "已配置" : "未配置"}
          </span>
        </div>
        <label>API Key</label>
        <input type="text" id="key-${meta.name}" placeholder="${esc(cred.api_key || "粘贴 API Key")}">
        <label>API Secret</label>
        <input type="password" id="sec-${meta.name}" placeholder="${cred.api_secret ? "已保存，留空则不改" : "粘贴 Secret"}">
        ${meta.needsPassphrase ? `
        <label>Passphrase</label>
        <input type="password" id="pass-${meta.name}" placeholder="${cred.passphrase ? "已保存，留空则不改" : "创建 API 时设置的口令"}">`
        : `<div aria-hidden="true" style="visibility:hidden">
        <label>&nbsp;</label><input type="text" tabindex="-1"></div>`}
        <label>收款账号（展示给付款人，可留空）</label>
        <input type="text" id="acc-${meta.name}" value="${esc(cred.account_label || "")}" placeholder="如 Pay ID / UID">
        <div class="row" style="margin-top:14px">
          <button class="primary sm" data-save="${meta.name}">保存</button>
          <button class="sm" data-test="${meta.name}">自检</button>
        </div>
        <div class="msg" id="cm-${meta.name}"></div>`;
      host.appendChild(el);
    });

    host.querySelectorAll("[data-save]").forEach((btn) => {
      btn.onclick = () => saveCredential(btn.dataset.save, btn);
    });
    host.querySelectorAll("[data-test]").forEach((btn) => {
      btn.onclick = () => testCredential(btn.dataset.test, btn);
    });
  }

  async function saveCredential(name, btn) {
    const body = {};
    const key = $(`key-${name}`).value.trim();
    const sec = $(`sec-${name}`).value.trim();
    const passEl = $(`pass-${name}`);
    const acc = $(`acc-${name}`).value.trim();
    if (key) body.api_key = key;
    if (sec) body.api_secret = sec;
    if (passEl && passEl.value.trim()) body.passphrase = passEl.value.trim();
    body.account_label = acc;

    btn.disabled = true;
    try {
      const data = await api(`/api/admin/credentials/${name}`, {
        method: "PUT", body: JSON.stringify(body),
      });
      if (data.complete) {
        setMsg(`cm-${name}`, "已保存，建议接着点「自检」验证权限。", "ok");
      } else {
        setMsg(`cm-${name}`, "已保存，但还缺：" + data.missing.join("、"), "warn");
      }
      // 清空密码框，避免明文留在页面上
      $(`sec-${name}`).value = "";
      if (passEl) passEl.value = "";
      await loadCredentials();
      renderQRCards();
    } catch (err) {
      setMsg(`cm-${name}`, err.message, "err");
    } finally {
      btn.disabled = false;
    }
  }

  async function testCredential(name, btn) {
    btn.disabled = true;
    setMsg(`cm-${name}`, "正在连接交易所…");
    try {
      const data = await api(`/api/admin/credentials/test?exchange=${name}`, { method: "POST" });
      const r = (data.reports || [])[0];
      if (!r) { setMsg(`cm-${name}`, "没有返回结果", "warn"); return; }
      const kind = !r.ok ? "err" : r.read_only === false ? "err" : r.read_only ? "ok" : "warn";
      let text = r.detail;
      if (r.account_label) text += `　账号 ${r.account_label}`;
      setMsg(`cm-${name}`, text, kind);
    } catch (err) {
      setMsg(`cm-${name}`, err.message, "err");
    } finally {
      btn.disabled = false;
    }
  }

  $("testAllBtn").onclick = async function () {
    this.disabled = true;
    try {
      const data = await api("/api/admin/credentials/test", { method: "POST" });
      (data.reports || []).forEach((r) => {
        const kind = !r.ok ? "err" : r.read_only === false ? "err" : r.read_only ? "ok" : "warn";
        setMsg(`cm-${r.exchange}`, r.detail, kind);
      });
    } finally {
      this.disabled = false;
    }
  };

  /* ---------- 2. 二维码 ---------- */
  function renderQRCards() {
    const host = $("qrCards");
    host.innerHTML = "";
    EXCHANGES.forEach((meta) => {
      const el = document.createElement("div");
      el.className = "card";
      el.style.margin = "0";
      el.innerHTML = `
        <div class="row"><strong>${esc(meta.title)}</strong></div>
        <div class="dropzone" id="dz-${meta.name}" style="margin-top:12px">
          <div>把收款页截图拖进来<br><span class="faint mini">或点击选择文件</span></div>
          <img id="dzimg-${meta.name}" style="display:none" alt="">
        </div>
        <input type="file" id="file-${meta.name}" accept="image/*" style="display:none">
        <div class="msg" id="qm-${meta.name}"></div>
        <div class="row" style="margin-top:10px">
          <button class="sm" data-view="${meta.name}">查看当前码</button>
          <button class="sm danger" data-delq="${meta.name}">删除</button>
        </div>`;
      host.appendChild(el);

      const dz = $(`dz-${meta.name}`);
      const file = $(`file-${meta.name}`);
      dz.onclick = () => file.click();
      file.onchange = () => { if (file.files[0]) uploadQR(meta.name, file.files[0]); };
      dz.ondragover = (e) => { e.preventDefault(); dz.classList.add("over"); };
      dz.ondragleave = () => dz.classList.remove("over");
      dz.ondrop = (e) => {
        e.preventDefault();
        dz.classList.remove("over");
        const f = e.dataTransfer.files[0];
        if (f) uploadQR(meta.name, f);
      };
      showCurrentQR(meta.name);
    });

    host.querySelectorAll("[data-view]").forEach((b) => {
      b.onclick = () => showCurrentQR(b.dataset.view, true);
    });
    host.querySelectorAll("[data-delq]").forEach((b) => {
      b.onclick = async () => {
        await api(`/api/admin/qr/${b.dataset.delq}`, { method: "DELETE" });
        $(`dzimg-${b.dataset.delq}`).style.display = "none";
        setMsg(`qm-${b.dataset.delq}`, "已删除", "warn");
      };
    });
  }

  async function showCurrentQR(name, verbose) {
    try {
      const res = await fetch(`/api/admin/qr/${name}.png?t=${Date.now()}`, {
        headers: { Authorization: "Bearer " + token },
      });
      if (!res.ok) {
        if (verbose) setMsg(`qm-${name}`, "还没有配置收款码", "warn");
        return;
      }
      const blob = await res.blob();
      const img = $(`dzimg-${name}`);
      img.src = URL.createObjectURL(blob);
      img.style.display = "";
    } catch (e) { /* 忽略 */ }
  }

  async function uploadQR(name, file) {
    setMsg(`qm-${name}`, "正在识别二维码…");
    const form = new FormData();
    form.append("file", file);
    form.append("regenerate", "true");
    form.append("size", "640");
    try {
      const data = await api(`/api/admin/qr/${name}`, { method: "POST", body: form });
      const bits = [];
      bits.push(data.regenerated ? "已按内容重绘为标准码" : "已透视裁剪");
      if (data.brand) bits.push(`识别为 ${data.brand}`);
      const warn = (data.warnings || []).join(" ");
      setMsg(`qm-${name}`, bits.join("，") + (warn ? "。" + warn : "。"),
             warn ? "warn" : "ok");
      await showCurrentQR(name);
    } catch (err) {
      setMsg(`qm-${name}`, err.message, "err");
    }
  }

  /* ---------- 3. 聚合图 ---------- */
  $("composeBtn").onclick = async function () {
    this.disabled = true;
    setMsg("composeMsg", "正在合成并回读校验…");
    try {
      const body = {
        layout: $("layoutSel").value,
        qr_size: Number($("sizeInput").value),
        gutter_ratio: Number($("gutterInput").value),
        title: $("titleInput").value,
      };
      const data = await api("/api/admin/qr/compose", { method: "POST", body: JSON.stringify(body) });

      const okCount = Object.values(data.verified || {}).filter(Boolean).length;
      const total = Object.keys(data.verified || {}).length;
      let kind = data.all_verified ? "ok" : "warn";
      let text = `已生成 ${data.size[0]}×${data.size[1]}，回读校验 ${okCount}/${total} 通过。`;
      if (data.missing && data.missing.length) {
        text += ` 未包含（缺二维码）：${data.missing.join("、")}。`;
      }
      if (data.warnings && data.warnings.length) text += " " + data.warnings.join(" ");
      setMsg("composeMsg", text, kind);

      const q = `layout=${body.layout}&size=${body.qr_size}&t=${Date.now()}`;
      $("aggPreview").innerHTML =
        `<img src="/api/qr/aggregate.png?${q}" alt="聚合收款图">
         <p class="hint">右键即可另存。公开链接：<code>/api/qr/aggregate.png</code></p>`;
    } catch (err) {
      setMsg("composeMsg", err.message, "err");
    } finally {
      this.disabled = false;
    }
  };

  /* ---------- 4. 订单 ---------- */
  async function loadOrders() {
    try {
      const data = await api("/api/admin/orders?limit=50");
      const s = data.stats || {};
      const by = s.by_status || {};
      $("statsLine").textContent =
        `已支付 ${s.paid_count || 0} 笔，累计 ${s.paid_total || 0}　|　` +
        `待付 ${by.pending || 0}，过期 ${by.expired || 0}　|　` +
        `已配置渠道：${(s.exchanges || []).join("、") || "无"}`;

      const rows = $("orderRows");
      rows.innerHTML = "";
      (data.orders || []).forEach((o) => {
        const [cls, label] = STATUS_PILL[o.status] || ["", o.status];
        const st = o.settlement || {};
        const tr = document.createElement("tr");
        tr.innerHTML = `
          <td class="mono mini">${esc(o.order_id.slice(0, 10))}</td>
          <td class="mono">${esc(o.pay_amount)} ${esc(o.currency)}</td>
          <td><span class="pill ${cls}">${esc(label)}</span></td>
          <td>${esc(st.exchange || o.exchange || "任意")}</td>
          <td class="mini faint">${esc(st.reason || "")}</td>
          <td class="mini faint">${fmtTime(o.created_ms)}</td>
          <td>${o.status === "pending"
                ? `<button class="sm" data-settle="${esc(o.order_id)}">人工核销</button>` : ""}</td>`;
        rows.appendChild(tr);
      });

      rows.querySelectorAll("[data-settle]").forEach((b) => {
        b.onclick = () => manualSettle(b.dataset.settle);
      });
    } catch (err) {
      setMsg("orderMsg", err.message, "err");
    }
  }

  async function manualSettle(orderId) {
    const exchange = prompt("这笔钱是从哪个渠道收到的？(binance / okx / bitget)");
    if (!exchange) return;
    const txId = prompt("对应的交易所流水号 / 订单号（用于防止重复核销）");
    if (!txId) return;
    try {
      await api(`/api/admin/orders/${orderId}/settle`, {
        method: "POST",
        body: JSON.stringify({ exchange: exchange.trim(), tx_id: txId.trim(), note: "后台人工核销" }),
      });
      setMsg("orderMsg", "已核销", "ok");
      loadOrders();
    } catch (err) {
      setMsg("orderMsg", err.message, "err");
    }
  }

  $("reloadOrders").onclick = loadOrders;
  $("sweepBtn").onclick = async function () {
    this.disabled = true;
    setMsg("orderMsg", "正在拉取各所进账…");
    try {
      const data = await api("/api/admin/sweep", { method: "POST" });
      let text = `检查了 ${data.checked} 笔待付订单，扫描 ${data.transactions} 笔进账，核销 ${data.settled.length} 笔。`;
      if (data.errors && data.errors.length) text += " 错误：" + data.errors.join("; ");
      setMsg("orderMsg", text, data.errors && data.errors.length ? "warn" : "ok");
      loadOrders();
    } catch (err) {
      setMsg("orderMsg", err.message, "err");
    } finally {
      this.disabled = false;
    }
  };

  /* ---------- 5. 进账 ---------- */
  $("loadTxBtn").onclick = async function () {
    this.disabled = true;
    setMsg("txMsg", "查询中…");
    try {
      const data = await api(`/api/admin/transactions?minutes=${$("txMinutes").value}`);
      const rows = $("txRows");
      rows.innerHTML = "";
      (data.transactions || []).forEach((t) => {
        const ident = ["payer_name", "payer_uid", "withdraw_id", "memo"]
          .filter((k) => t[k]).map((k) => `${k}=${t[k]}`).join(" ");
        const tr = document.createElement("tr");
        tr.innerHTML = `
          <td class="mini">${fmtTime(t.timestamp_ms)}</td>
          <td>${esc(t.exchange)}</td>
          <td class="mono">${esc(t.amount)} ${esc(t.currency)}</td>
          <td class="mini faint">${esc(ident)}</td>
          <td class="mono mini faint">${esc(String(t.tx_id).slice(0, 18))}</td>`;
        rows.appendChild(tr);
      });
      const n = (data.transactions || []).length;
      setMsg("txMsg", n ? `共 ${n} 笔` : "该时间段没有进账",
             (data.errors || []).length ? "warn" : "");
      if ((data.errors || []).length) {
        setMsg("txMsg", `共 ${n} 笔。错误：` + data.errors.join("; "), "warn");
      }
    } catch (err) {
      setMsg("txMsg", err.message, "err");
    } finally {
      this.disabled = false;
    }
  };

  /* ---------- 启动 ---------- */
  const remembered = safeGet("cexpay-admin-token");
  if (remembered) {
    $("tokenInput").value = remembered;
    login(remembered);
  }
})();
