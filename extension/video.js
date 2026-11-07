// Botão "Baixar este vídeo" sobre os <video> da página, com painel de qualidades (vídeo) e formatos de áudio.
// Quem analisa e baixa é o Raio (via background.js). Este script só desenha a interface.
(() => {
  "use strict";
  if (globalThis.__pydmVideoLoaded) return;
  globalThis.__pydmVideoLoaded = true;

  const MIN_W = 240, MIN_H = 135;   // vídeos menores (miniaturas, anúncios) são ignorados
  const HIDE_MS = 1000;

  let host = null, root = null, btn = null, panel = null;
  let current = null;               // <video> sobre o qual o botão está
  let panelOpen = false, busy = false, token = 0;
  let hideTimer = 0, posTimer = 0, lastMove = 0;
  let cfgCache = { t: 0, on: false };

  // ---------- utilidades ----------
  function fmtSize(n) {
    if (!n || n <= 0) return "";
    const u = ["B", "KB", "MB", "GB"];
    let i = 0, v = n;
    while (v >= 1024 && i < u.length - 1) { v /= 1024; i++; }
    return "~" + (i >= 2 ? v.toFixed(1) : Math.round(v)) + " " + u[i];
  }

  // No YouTube só as páginas de vídeo; a página inicial tem prévias que não são vídeos baixáveis.
  function pageSupported(loc) {
    if (/(^|\.)youtube\.com$/i.test(loc.hostname)) return /^\/(watch|shorts|embed|live)(\/|$)/.test(loc.pathname);
    return true;
  }

  // Endereços http(s) do próprio <video> (blob: não dá para baixar direto).
  function directSources(v, base) {
    const out = new Set();
    const add = (u) => {
      if (!u) return;   // vazio resolveria para a própria URL da página
      try {
        const a = new URL(u, base);
        if (a.protocol === "http:" || a.protocol === "https:") out.add(a.href);
      } catch (e) { /* ignora */ }
    };
    if (v) {
      add(v.currentSrc); add(v.src);
      if (v.querySelectorAll) v.querySelectorAll("source[src]").forEach((s) => add(s.src));
    }
    return [...out].filter(Boolean).slice(0, 4);
  }

  function send(msg) {
    try {
      return Promise.resolve(chrome.runtime.sendMessage(msg)).catch(() => null);
    } catch (e) {   // extensão recarregada: o contexto deste script ficou inválido
      return Promise.resolve(null);
    }
  }

  async function enabled() {
    if (Date.now() - cfgCache.t < 15000) return cfgCache.on;
    const c = await send({ type: "video-config" });
    cfgCache = { t: Date.now(), on: !!(c && c.online && c.enabled) };
    return cfgCache.on;
  }

  function el(tag, cls, text) {
    const e = document.createElement(tag);
    if (cls) e.className = cls;
    if (text != null) e.textContent = text;
    return e;
  }

  // ---------- interface (Shadow DOM: o CSS da página não mexe nela e vice-versa) ----------
  const CSS = `
    :host{all:initial}
    *{box-sizing:border-box;font-family:"Segoe UI",Roboto,Arial,sans-serif}
    .wrap{position:absolute;right:0;top:0;display:flex;flex-direction:column;align-items:flex-end;pointer-events:none}
    .btn{pointer-events:auto;display:flex;align-items:center;gap:7px;cursor:pointer;border:0;border-radius:6px;
      padding:6px 11px;background:rgba(20,20,20,.88);color:#fff;font-size:13px;font-weight:600;
      box-shadow:0 2px 8px rgba(0,0,0,.45)}
    .btn:hover{background:#1a73e8}
    .arrow{color:#34d058;font-size:15px;line-height:1}
    .panel{pointer-events:auto;margin-top:6px;width:380px;max-width:92vw;overflow:auto;background:#fff;color:#202124;
      border:1px solid #c9c9c9;border-radius:8px;box-shadow:0 6px 24px rgba(0,0,0,.35);font-size:13px}
    .panel[hidden]{display:none}
    .head{display:flex;justify-content:space-between;align-items:center;gap:8px;padding:8px 10px;
      border-bottom:1px solid #e3e3e3;background:#f6f7f9;font-weight:600}
    .title{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
    .x{cursor:pointer;border:0;background:none;font-size:16px;line-height:1;color:#555;padding:2px 4px}
    .sec{padding:8px 10px 3px;font-size:11px;font-weight:700;letter-spacing:.05em;color:#5f6368;text-transform:uppercase}
    .row{display:flex;justify-content:space-between;gap:12px;padding:7px 10px;cursor:pointer}
    .row:hover{background:#e8f0fe}
    .size{color:#5f6368;white-space:nowrap}
    .msg{padding:12px 10px;line-height:1.45}
    .warn{padding:8px 10px;background:#fff8e1;color:#7a5b00;font-size:12px;border-top:1px solid #f0e2a8;line-height:1.4}
    .ok{color:#188038;font-weight:600}
    .err{color:#b3261e}
  `;

  function ensureUi() {
    if (host) return;
    host = document.createElement("pydm-video-ui");
    host.style.cssText = "all:initial;position:fixed;z-index:2147483647;display:none;left:0;top:0;width:0;height:0;";
    root = host.attachShadow({ mode: "closed" });
    try {
      const sheet = new CSSStyleSheet();
      sheet.replaceSync(CSS);
      root.adoptedStyleSheets = [sheet];
    } catch (e) {
      const st = el("style"); st.textContent = CSS; root.append(st);
    }
    const wrap = el("div", "wrap");
    btn = el("button", "btn");
    btn.type = "button";
    btn.append(el("span", "arrow", "\u2B07"), el("span", "", "Baixar este vídeo"));
    panel = el("div", "panel");
    panel.hidden = true;
    wrap.append(btn, panel);
    root.append(wrap);
    document.documentElement.append(host);

    btn.addEventListener("click", (e) => {
      if (!e.isTrusted) return;   // ignora cliques fabricados pela página
      e.stopPropagation();
      panelOpen ? closePanel() : openPanel();
    });
    host.addEventListener("mouseenter", cancelHide);
    host.addEventListener("mouseleave", scheduleHide);
  }

  function place() {
    if (!current || !host) return;
    const r = current.getBoundingClientRect();
    const visible = current.isConnected && r.width >= MIN_W && r.height >= MIN_H &&
      r.bottom > 48 && r.top < innerHeight - 48 && r.right > 48 && r.left < innerWidth - 48;
    if (!visible || document.fullscreenElement) { hideAll(); return; }
    const x = Math.min(r.right, innerWidth) - 10;
    const y = Math.max(r.top, 0) + 10;
    host.style.left = x + "px";
    host.style.top = y + "px";
    panel.style.maxHeight = Math.max(160, innerHeight - y - 70) + "px";
  }

  function cancelHide() { clearTimeout(hideTimer); hideTimer = 0; }
  function scheduleHide() {
    if (panelOpen) return;
    cancelHide();
    hideTimer = setTimeout(hideAll, HIDE_MS);
  }
  function hideAll() {
    cancelHide();
    closePanel();
    if (host) host.style.display = "none";
    current = null;
    if (posTimer) { clearInterval(posTimer); posTimer = 0; }
  }

  async function show(v) {
    if (!pageSupported(location)) return;
    if (current === v && host && host.style.display === "block") { cancelHide(); return; }
    if (!(await enabled())) return;
    current = v;
    ensureUi();
    host.style.display = "block";
    place();
    cancelHide();
    if (!posTimer) posTimer = setInterval(place, 150);
  }

  // ---------- painel ----------
  function closePanel() {
    panelOpen = false;
    token++;
    if (panel) panel.hidden = true;
  }

  function header(text) {
    const h = el("div", "head");
    const t = el("span", "title", text);
    t.title = text;
    const x = el("button", "x", "\u2715");
    x.type = "button";
    x.addEventListener("click", (e) => { if (e.isTrusted) closePanel(); });
    h.append(t, x);
    return h;
  }

  function showMessage(title, text, cls) {
    panel.replaceChildren(header(title), el("div", "msg " + (cls || ""), text));
  }

  async function openPanel() {
    const v = current;
    if (!v) return;
    panelOpen = true;
    panel.hidden = false;
    showMessage("Baixar este vídeo", "Analisando o vídeo...");
    const my = ++token;
    const res = await send({ type: "media-formats", url: location.href, referrer: document.referrer });
    if (my !== token || !panelOpen) return;   // o painel foi fechado ou reaberto enquanto analisava
    render(res, directSources(v, location.href));
  }

  function optionRow(res, opt) {
    const row = el("div", "row");
    row.append(el("span", "", opt.label), el("span", "size", fmtSize(opt.size)));
    row.addEventListener("click", (e) => { if (e.isTrusted) pick(res, opt); });
    return row;
  }

  function directRow(u) {
    let ext = "";
    try { ext = (new URL(u).pathname.match(/\.([a-z0-9]{2,5})$/i) || [])[1] || ""; } catch (e) { /* ignora */ }
    const row = el("div", "row");
    row.append(el("span", "", "Arquivo do vídeo" + (ext ? " (" + ext.toUpperCase() + ")" : "")), el("span", "size", "direto"));
    row.addEventListener("click", async (e) => {
      if (!e.isTrusted || busy) return;
      busy = true;
      const r = await send({ type: "download-direct", url: u, referrer: document.referrer || location.href });
      busy = false;
      done(r && r.ok, "Adicionado ao Raio", r && r.message);
    });
    return row;
  }

  function render(res, direct) {
    if (!res || res.ok === false) {
      const text = !res ? "Não consegui falar com o Raio. Ele está aberto?" :
        (res.message || "Não foi possível analisar este vídeo.");
      const parts = [header("Baixar este vídeo"), el("div", "msg err", text)];
      if (direct.length) {
        parts.push(el("div", "sec", "Arquivo da página"));
        direct.forEach((u) => parts.push(directRow(u)));
      }
      panel.replaceChildren(...parts);
      return;
    }
    const parts = [header(res.title || document.title || "Baixar este vídeo")];
    if (res.live) parts.push(el("div", "warn", "Esta página parece uma transmissão ao vivo; o download pode não terminar."));
    if (res.video.length) {
      parts.push(el("div", "sec", "Vídeo"));
      res.video.forEach((o) => parts.push(optionRow(res, o)));
    }
    if (res.audio.length) {
      parts.push(el("div", "sec", "Somente áudio"));
      res.audio.forEach((o) => parts.push(optionRow(res, o)));
    }
    if (!res.ffmpeg) {
      parts.push(el("div", "warn", "Sem o ffmpeg só aparecem as qualidades que já trazem som e não dá para " +
        "converter em MP3/WAV. Instale em Raio > Opções > Vídeo."));
    }
    if (res.deno === false && /(^|\.)youtube\.com$/i.test(location.hostname)) {
      parts.push(el("div", "warn", "O YouTube exige o Deno para liberar todas as qualidades. " +
        "Instale em Raio > Opções > Vídeo."));
    }
    panel.replaceChildren(...parts);
  }

  async function pick(res, opt) {
    if (busy) return;
    busy = true;
    const title = (res.title || document.title || "").replace(/ - YouTube$/, "");
    const r = await send({
      type: "media-download", url: location.href, referrer: document.referrer,
      id: opt.id, title, est: opt.size || 0
    });
    busy = false;
    done(r && r.ok, "Adicionado ao Raio: " + opt.label, r && r.message);
  }

  function done(ok, okText, errText) {
    if (!panelOpen) return;
    if (ok) {
      showMessage("Raio", "\u2713 " + okText, "ok");
      setTimeout(() => { if (panelOpen) closePanel(); }, 1600);
    } else {
      showMessage("Raio", errText || "Não foi possível enviar ao Raio. Ele está aberto?", "err");
    }
  }

  // ---------- detecção do vídeo sob o mouse ----------
  function qualifies(v) {
    const r = v.getBoundingClientRect();
    if (r.width < MIN_W || r.height < MIN_H) return false;
    const cs = getComputedStyle(v);
    return cs.visibility !== "hidden" && cs.display !== "none";
  }

  function videoAt(x, y) {
    for (const e of document.elementsFromPoint(x, y)) {
      if (e.tagName === "VIDEO" && qualifies(e)) return e;
    }
    return null;
  }

  addEventListener("mousemove", (e) => {
    const now = Date.now();
    if (now - lastMove < 100) return;   // não precisa checar a cada pixel
    lastMove = now;
    if (document.fullscreenElement) return;
    if (host && e.composedPath().includes(host)) { cancelHide(); return; }
    const v = videoAt(e.clientX, e.clientY);
    if (v) show(v); else scheduleHide();
  }, true);

  // clicar fora (ou Esc) fecha o painel
  addEventListener("mousedown", (e) => {
    if (panelOpen && !(host && e.composedPath().includes(host))) closePanel();
  }, true);
  addEventListener("keydown", (e) => { if (e.key === "Escape" && panelOpen) closePanel(); }, true);
  addEventListener("scroll", () => place(), true);
  addEventListener("resize", () => place());
  document.addEventListener("fullscreenchange", () => { if (document.fullscreenElement) hideAll(); });

  // gancho de teste (não existe no navegador)
  if (globalThis.__PYDM_TEST__) {
    Object.assign(globalThis.__PYDM_TEST__, {
      fmtSize, pageSupported, directSources, show, openPanel, closePanel, hideAll, place,
      state: () => ({ host, panel, btn, current, panelOpen })
    });
  }
})();
