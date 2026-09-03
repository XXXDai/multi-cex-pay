/* 收银台逻辑：轮询订单状态 + 切换交易所 + 提交付款方标识 */
(function () {
  "use strict";

  const $ = (id) => document.getElementById(id);
  const params = new URLSearchParams(location.search);
  const orderId = params.get("order_id");
  // 被 embed.js 用 iframe 嵌进别人页面时：隐藏顶栏、状态变化用 postMessage 通知父页
  const embedded = params.get("embed") === "1";
  const forcedTheme = params.get("theme");

  let order = null;
  let exchanges = [];
  let current = null;      // 当前选中的交易所名；null = 聚合图
  let pollTimer = null;
  let tickTimer = null;

  /* ---------- 主题 ---------- */
  const saved = forcedTheme || safeGet("cexpay-theme");
  if (saved) document.documentElement.dataset.theme = saved;
  if (embedded) document.documentElement.dataset.embed = "1";
  $("themeBtn").onclick = () => {
    const root = document.documentElement;
    const next = root.dataset.theme === "dark" ? "light" : "dark";
    root.dataset.theme = next;
    safeSet("cexpay-theme", next);
  };
  function safeGet(k) { try { return localStorage.getItem(k); } catch (e) { return null; } }
  function safeSet(k, v) { try { localStorage.setItem(k, v); } catch (e) { /* 无痕模式 */ } }

  /* ---------- 工具 ---------- */
  async function api(path, options) {
    const res = await fetch(path, Object.assign({ headers: { "Content-Type": "application/json" } }, options));
    const text = await res.text();
    let data = {};
    try { data = text ? JSON.parse(text) : {}; } catch (e) { data = { detail: text }; }
    if (!res.ok) throw new Error(data.detail || `HTTP ${res.status}`);
    return data;
  }

  /** 嵌入模式下把状态变化告诉宿主页面（embed.js 会校验 origin） */
  let notified = null;
  function notifyParent(type, order) {
    if (!embedded || window.parent === window) return;
    if (notified === type) return;          // 同一状态只播一次
    notified = type;
    try {
      window.parent.postMessage({ type: `cexpay:${type}`, order: order || null }, "*");
    } catch (e) { /* 父页跨域拒收就算了 */ }
  }

  function show(view) {
    ["loading", "payView", "doneView", "deadView"].forEach((id) => {
      $(id).style.display = id === view ? "" : "none";
    });
    reportHeight();
  }

  /** 把当前内容高度告诉宿主页面，让弹窗贴合内容（避免大片空白）。

      刻意不用 requestAnimationFrame 做测量：文档处于隐藏或被节流的状态时
      （典型场景是宿主页面在后台标签里打开），rAF 回调根本不会执行，
      高度就永远上报不出去。这里改成同步测量 + ResizeObserver 跟踪后续变化。 */
  let lastHeight = 0;
  function reportHeight() {
    if (!embedded || window.parent === window) return;
    const wrap = document.querySelector(".wrap");
    if (!wrap) return;
    const h = Math.ceil(wrap.getBoundingClientRect().height) + 40;
    if (h < 120 || Math.abs(h - lastHeight) < 8) return;   // 还没渲染完 / 抖动都不上报
    lastHeight = h;
    try {
      window.parent.postMessage({ type: "cexpay:height", height: h }, "*");
    } catch (e) { /* 父页跨域拒收就算了 */ }
  }

  // 图片加载完、标签切换、文案换行都会改变高度，交给 ResizeObserver 统一跟踪
  function watchHeight() {
    if (!embedded || window.parent === window) return;
    const wrap = document.querySelector(".wrap");
    if (!wrap || typeof ResizeObserver === "undefined") return;
    new ResizeObserver(reportHeight).observe(wrap);
  }

  function setMsg(el, text, kind) {
    el.textContent = text || "";
    el.className = "msg" + (kind ? " " + kind : "");
  }

  async function copyText(value, button) {
    try {
      await navigator.clipboard.writeText(value);
    } catch (e) {
      // clipboard 在非 https 下不可用，退回选中输入框
      const input = document.createElement("input");
      input.value = value;
      document.body.appendChild(input);
      input.select();
      try { document.execCommand("copy"); } catch (_) {}
      input.remove();
    }
    const original = button.textContent;
    button.textContent = "已复制";
    setTimeout(() => { button.textContent = original; }, 1400);
  }

  /* ---------- 渲染 ---------- */
  function renderTabs() {
    const tabs = $("tabs");
    tabs.innerHTML = "";

    const options = [];
    // 订单未指定交易所时，允许看聚合图
    if (!order.exchange && exchanges.length > 1) {
      options.push({ name: null, title: "全部", sub: `${exchanges.length} 个渠道` });
    }
    exchanges
      .filter((e) => !order.exchange || e.name === order.exchange)
      .forEach((e) => options.push({ name: e.name, title: e.display_name, sub: e.account_label || "" }));

    options.forEach((opt) => {
      const el = document.createElement("button");
      el.className = "tab" + (opt.name === current ? " active" : "");
      if (opt.name) el.dataset.ex = opt.name;
      el.innerHTML = `${escapeHtml(opt.title)}<span class="sub">${escapeHtml(opt.sub)}</span>`;
      el.onclick = () => { current = opt.name; renderTabs(); renderQR(); };
      tabs.appendChild(el);
    });

    if (current === undefined || (current && !exchanges.some((e) => e.name === current))) {
      current = options.length ? options[0].name : null;
    }
  }

  function escapeHtml(s) {
    return String(s == null ? "" : s).replace(/[&<>"']/g, (c) =>
      ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  }

  function renderQR() {
    const box = $("qrBox");
    const img = $("qrImg");
    const stamp = Date.now();
    if (current) {
      box.classList.remove("aggregate");
      img.src = `/api/orders/${orderId}/qr.png?exchange=${encodeURIComponent(current)}&t=${stamp}`;
    } else {
      box.classList.add("aggregate");
      img.src = `/api/orders/${orderId}/qr.png?layout=row&size=460&t=${stamp}`;
    }
    img.onerror = () => {
      img.removeAttribute("src");
      img.alt = "该渠道还没有配置收款码，请改用账号转账";
    };

    const info = exchanges.find((e) => e.name === current);
    $("payHint").textContent = info
      ? info.pay_hint
      : "用相机对准所需品牌那一格扫描；点上方标签可切到单码大图，更容易扫上";

    const account = info && info.account_label;
    $("accountLine").style.display = account ? "" : "none";
    if (account) $("accountLabel").value = account;

    // 备注码只有 Binance 读得到
    const memoLine = $("memoLine");
    if (order.memo && info && info.supports_memo) {
      memoLine.style.display = "";
      memoLine.innerHTML = `转账备注可填 <code>${escapeHtml(order.memo)}</code>（选填，能提高自动到账成功率）`;
    } else {
      memoLine.style.display = "none";
    }

    renderIdentifierForm(info);
    reportHeight();
  }

  function renderIdentifierForm(info) {
    const spec = info && info.identifier;
    if (!spec) {
      $("identBox").innerHTML = '<p class="hint">请先选择一个具体的支付渠道。</p>';
      return;
    }
    $("identLabel").textContent = spec.label;
    $("identInput").placeholder = spec.placeholder || "";
    $("identInput").dataset.kind = spec.kind;
    $("identHelp").textContent = spec.help_text || "";
  }

  function renderOrder() {
    $("payAmount").textContent = order.pay_amount;
    $("payCurrency").textContent = order.currency;
    $("amountCopy").value = order.pay_amount;

    if (order.pay_amount === order.base_amount) {
      $("amountNote").style.display = "none";
    }

    if (order.status === "paid") {
      show("doneView");
      notifyParent("paid", order);
      const s = order.settlement || {};
      $("doneDetail").textContent =
        `${order.pay_amount} ${order.currency} · 来自 ${s.exchange || "-"}` +
        (s.reason ? ` · ${s.reason}` : "");
      stopTimers();
      return;
    }
    if (order.status === "expired" || order.status === "cancelled") {
      $("deadTitle").textContent = order.status === "expired" ? "订单已过期" : "订单已取消";
      show("deadView");
      notifyParent(order.status === "expired" ? "expired" : "close", order);
      stopTimers();
      return;
    }
    show("payView");
  }

  function tick() {
    const left = Math.max(0, Math.floor((order.expires_ms - Date.now()) / 1000));
    if (left <= 0) {
      $("countdown").textContent = "订单已过期";
      return;
    }
    const m = String(Math.floor(left / 60)).padStart(2, "0");
    const s = String(left % 60).padStart(2, "0");
    $("countdown").textContent = `剩余支付时间 ${m}:${s}`;
  }

  function stopTimers() {
    clearInterval(pollTimer);
    clearInterval(tickTimer);
  }

  /* ---------- 交互 ---------- */
  $("copyAmount").onclick = (e) => copyText(order.pay_amount, e.target);
  $("copyAccount").onclick = (e) => copyText($("accountLabel").value, e.target);

  $("checkBtn").onclick = async function () {
    const btn = this;
    btn.disabled = true;
    btn.textContent = "核对中…";
    setMsg($("checkMsg"), "");
    try {
      const data = await api(`/api/orders/${orderId}/check`, { method: "POST" });
      order = data.order;
      if (data.is_paid) {
        renderOrder();
        return;
      }
      const tail = data.errors && data.errors.length ? `（${data.errors.length} 个渠道查询失败）` : "";
      setMsg($("checkMsg"),
        `暂时没有查到这笔款项${tail}。链上/内部转账通常几秒到账，稍等片刻再试；` +
        `如果金额被改动过，请在下方补充付款方信息。`, "warn");
    } catch (err) {
      setMsg($("checkMsg"), err.message, "err");
    } finally {
      btn.disabled = false;
      btn.textContent = "我已支付，立即核对";
    }
  };

  $("identBtn").onclick = async function () {
    const input = $("identInput");
    const value = input.value.trim();
    if (!value) { setMsg($("identMsg"), "请先填写", "warn"); return; }
    this.disabled = true;
    try {
      const data = await api(`/api/orders/${orderId}/identifier`, {
        method: "POST",
        body: JSON.stringify({ kind: input.dataset.kind, value: value }),
      });
      order = data.order;
      if (order.status === "paid") { renderOrder(); return; }
      setMsg($("identMsg"), "已记录，系统会持续为你核对，请稍候。", "ok");
    } catch (err) {
      setMsg($("identMsg"), err.message, "err");
    } finally {
      this.disabled = false;
    }
  };

  /* ---------- 启动 ---------- */
  async function poll() {
    try {
      const data = await api(`/api/orders/${orderId}`);
      order = data.order;
      renderOrder();
    } catch (e) { /* 网络抖动就等下一轮 */ }
  }

  async function init() {
    if (!orderId) {
      $("loading").innerHTML = '<p class="msg err">缺少 order_id 参数</p>';
      return;
    }
    try {
      const [orderData, exData] = await Promise.all([
        api(`/api/orders/${orderId}`),
        api("/api/exchanges"),
      ]);
      order = orderData.order;
      exchanges = exData.exchanges || [];
      if (!exchanges.length) {
        $("loading").innerHTML =
          '<p class="msg err">商户还没有配置任何收款渠道，请联系商户。</p>';
        return;
      }
      // 窄屏（手机）默认单码：聚合图在手机上每格只有百来像素，相机很难对焦到一格
      const preferSingle = window.matchMedia("(max-width: 640px)").matches;
      current = order.exchange
        || (exchanges.length > 1 && !preferSingle ? null : exchanges[0].name);
      renderTabs();
      renderOrder();
      renderQR();
      tick();
      watchHeight();
      tickTimer = setInterval(tick, 1000);
      pollTimer = setInterval(poll, 5000);
    } catch (err) {
      $("loading").innerHTML = `<p class="msg err">${escapeHtml(err.message)}</p>`;
    }
  }

  init();
})();
