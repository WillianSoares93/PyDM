const API = "http://127.0.0.1:9614";
const MODS_TTL = 3000;   // ms: por quanto tempo vale a tecla segurada no último clique
const CONTEXTS = ["link", "video", "audio", "image", "page", "selection"];
const DEFAULT_CFG = {
  title: "Baixar com PyDM",
  menu: { link: true, video: true, audio: true, image: true, page: false }
};

let lastMods = { mods: "", t: 0 };

function badge(text, color) {
  chrome.action.setBadgeText({ text });
  chrome.action.setBadgeBackgroundColor({ color: color || "#888" });
}

// O service worker dorme e acorda: o estado "ligado" é sempre lido do storage
// (antes ele era lido de forma assíncrona e um download podia ser capturado com a captura desligada).
async function isEnabled() {
  const v = await chrome.storage.local.get({ enabled: true });
  return v.enabled;
}
async function refreshBadge() { badge((await isEnabled()) ? "" : "OFF"); }
refreshBadge();

// Clique no ícone: liga/desliga a captura
chrome.action.onClicked.addListener(async () => {
  await chrome.storage.local.set({ enabled: !(await isEnabled()) });
  refreshBadge();
});

// ---------- mensagens dos scripts das páginas (keys.js e video.js) ----------
async function pydmJson(path, body, timeoutMs) {
  const ctrl = new AbortController();
  const timer = setTimeout(() => ctrl.abort(), timeoutMs || 5000);
  try {
    const r = await fetch(API + path, {
      method: body ? "POST" : "GET",
      headers: body ? { "Content-Type": "application/json" } : undefined,
      body: body ? JSON.stringify(body) : undefined,
      signal: ctrl.signal
    });
    return await r.json();
  } finally {
    clearTimeout(timer);
  }
}

const OFFLINE = { ok: false, error: "offline", message: "Não consegui falar com o PyDM. Ele está aberto?" };

// Dados do navegador que o PyDM usa (no YouTube o yt-dlp usa as próprias identidades, então não manda cookies).
async function pageInfo(msg) {
  const url = String(msg.url || "");
  if (!/^https?:\/\//i.test(url)) throw new Error("url");
  const yt = /(^|\.)(youtube\.com|youtu\.be)$/i.test(new URL(url).hostname);
  let cookie = "";
  if (!yt) cookie = (await chrome.cookies.getAll({ url })).map(c => `${c.name}=${c.value}`).join("; ");
  return { url, referrer: String(msg.referrer || ""), cookie, userAgent: navigator.userAgent };
}

const HANDLERS = {
  // o painel só aparece se o PyDM estiver aberto e com o recurso ligado nas Opções
  "video-config": async () => {
    try {
      const c = await pydmJson("/config", null, 1500);
      return { online: true, enabled: !!(c.video && c.video.enabled) };
    } catch (e) {
      return { online: false, enabled: false };
    }
  },
  // lista as qualidades de vídeo e os formatos de áudio (o PyDM roda o yt-dlp; pode levar alguns segundos)
  "media-formats": async (msg) => pydmJson("/media/formats", await pageInfo(msg), 120000),
  "media-download": async (msg) => pydmJson("/media/download", {
    ...(await pageInfo(msg)), id: String(msg.id || ""), title: String(msg.title || ""), est: Number(msg.est) || 0
  }, 10000),
  // arquivo de vídeo com endereço direto: baixa pelo motor normal do PyDM
  "download-direct": async (msg) => {
    const ok = await sendToPyDM(String(msg.url || ""), String(msg.referrer || ""), { manual: true });
    return { ok };
  }
};

chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
  if (!msg || sender.id !== chrome.runtime.id) return;
  if (msg.type === "mods") {               // teclas segurando o clique (keys.js)
    lastMods = { mods: String(msg.mods || ""), t: Date.now() };
    return;
  }
  const handler = HANDLERS[msg.type];
  if (!handler) return;
  handler(msg).then(sendResponse, () => sendResponse(OFFLINE));
  return true;                              // resposta assíncrona
});

// Retorna true se o PyDM aceitou o link. 409 = PyDM ignorou (filtros/teclas): o navegador baixa normalmente.
async function sendToPyDM(url, referrer, extra) {
  const cookies = await chrome.cookies.getAll({ url });
  const cookie = cookies.map(c => `${c.name}=${c.value}`).join("; ");
  const r = await fetch(API + "/add", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      url, referrer: referrer || "", cookie, userAgent: navigator.userAgent, ...(extra || {})
    })
  });
  return r.ok;
}

// Captura automática de downloads
chrome.downloads.onCreated.addListener(async (item) => {
  const mods = Date.now() - lastMods.t < MODS_TTL ? lastMods.mods : "";
  if (!(await isEnabled())) return;
  const url = item.finalUrl || item.url;
  if (!/^https?:/i.test(url)) return;          // ignora blob:, data:, file:
  const filename = (item.filename || "").split(/[\\/]/).pop();
  try {
    if (await sendToPyDM(url, item.referrer, { mods, filename, mime: item.mime || "" })) {
      await chrome.downloads.cancel(item.id);   // cancela o download do navegador
      await chrome.downloads.erase({ id: item.id });
    }
  } catch (e) {
    // PyDM fechado: o navegador continua o download normalmente
  }
});

// ---------- menu de clique direito (configurado nas Opções do PyDM) ----------
let syncing = Promise.resolve();
function syncMenu(force) {
  syncing = syncing.then(() => doSync(force)).catch(() => {});
  return syncing;
}

async function doSync(force) {
  const saved = (await chrome.storage.local.get({ menuCfg: null })).menuCfg;
  let cfg;
  try {
    const r = await fetch(API + "/config");
    if (!r.ok) throw new Error("http " + r.status);
    cfg = await r.json();
  } catch (e) {
    if (!force) return;                          // PyDM fechado: mantém o menu atual
    cfg = saved || DEFAULT_CFG;
  }
  if (!force && JSON.stringify(cfg) === JSON.stringify(saved)) return;   // nada mudou
  const contexts = Object.keys(cfg.menu || {}).filter(k => cfg.menu[k] && CONTEXTS.includes(k));
  await chrome.contextMenus.removeAll();
  if (contexts.length) {
    chrome.contextMenus.create({ id: "pydm", title: cfg.title || DEFAULT_CFG.title, contexts });
  }
  await chrome.storage.local.set({ menuCfg: cfg });
}

chrome.runtime.onInstalled.addListener(() => syncMenu(true));
chrome.runtime.onStartup.addListener(() => syncMenu(true));
chrome.alarms.get("pydm-sync", (a) => {
  if (!a) chrome.alarms.create("pydm-sync", { periodInMinutes: 1 });
});
chrome.alarms.onAlarm.addListener((a) => { if (a.name === "pydm-sync") syncMenu(false); });

chrome.contextMenus.onClicked.addListener(async (info) => {
  const url = info.linkUrl || info.srcUrl || info.pageUrl;
  if (!url || !/^https?:/i.test(url)) return;
  try {
    await sendToPyDM(url, info.pageUrl, { manual: true });   // menu de contexto: ignora os filtros
    badge("✓", "#188038");
  } catch (e) {
    badge("!", "#d93025");                      // PyDM não está aberto
  }
  setTimeout(refreshBadge, 3000);
});
