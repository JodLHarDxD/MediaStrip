// MediaStrip Catcher — background service worker (MV3)
// Sniffs media network responses (observational webRequest — no blocking needed),
// keeps a per-tab catch list, and forwards download requests to the MediaStrip server.

const DEFAULT_SERVER = "https://mediastrip.jodlx.in";
const MAX_ITEMS_PER_TAB = 50;
const MIN_MEDIA_BYTES = 100 * 1024; // ignore tiny blobs (ads, ping pixels)

const MANIFEST_URL_RE = /\.(m3u8|mpd)([?#]|$)/i;
const MEDIA_URL_RE = /\.(mp4|webm|mkv|mov|m4v|mp3|m4a|aac|flac|ogg|opus|wav|gif)([?#]|$)/i;
// HLS/DASH fragments — noise, the manifest is what we want
const FRAGMENT_URL_RE = /\.(ts|m4s)([?#]|$)|\/(seg|frag|chunk)[-_]?\d+/i;

// In-memory cache; chrome.storage.session is the source of truth across SW restarts.
const tabMedia = new Map();

function header(headers, name) {
  const h = (headers || []).find((x) => x.name.toLowerCase() === name);
  return h ? h.value : "";
}

function classify(url, contentType) {
  if (MANIFEST_URL_RE.test(url) || /mpegurl|dash\+xml/i.test(contentType)) return "manifest";
  if (/^(video|audio)\//i.test(contentType) || MEDIA_URL_RE.test(url)) return "media";
  if (/^image\//i.test(contentType) && /\.(jpg|jpeg|png|webp|gif)([?#]|$)/i.test(url)) return "image";
  return null;
}

async function loadTab(tabId) {
  if (tabMedia.has(tabId)) return tabMedia.get(tabId);
  const stored = await chrome.storage.session.get(`tab_${tabId}`);
  const list = stored[`tab_${tabId}`] || [];
  tabMedia.set(tabId, list);
  return list;
}

async function saveTab(tabId, list) {
  tabMedia.set(tabId, list);
  await chrome.storage.session.set({ [`tab_${tabId}`]: list });
  const text = list.length ? String(list.length) : "";
  try {
    await chrome.action.setBadgeText({ tabId, text });
    await chrome.action.setBadgeBackgroundColor({ tabId, color: "#00E5FF" });
  } catch (_) {
    /* tab may be gone */
  }
}

chrome.webRequest.onResponseStarted.addListener(
  async (details) => {
    if (details.tabId < 0) return;
    const contentType = header(details.responseHeaders, "content-type");
    if (FRAGMENT_URL_RE.test(details.url)) return;

    const kind = classify(details.url, contentType);
    if (!kind) return;

    const size = parseInt(header(details.responseHeaders, "content-length") || "0", 10);
    if (kind !== "manifest" && size > 0 && size < MIN_MEDIA_BYTES) return;

    const list = await loadTab(details.tabId);
    if (list.some((item) => item.url === details.url)) return;

    list.push({
      url: details.url,
      kind,
      contentType,
      size,
      ts: Date.now(),
    });
    if (list.length > MAX_ITEMS_PER_TAB) list.shift();
    await saveTab(details.tabId, list);
  },
  { urls: ["<all_urls>"], types: ["media", "xmlhttprequest", "other", "object"] },
  ["responseHeaders"]
);

// New top-level navigation → reset the tab's catch list
chrome.tabs.onUpdated.addListener((tabId, changeInfo) => {
  if (changeInfo.status === "loading" && changeInfo.url) {
    tabMedia.delete(tabId);
    chrome.storage.session.remove(`tab_${tabId}`);
    chrome.action.setBadgeText({ tabId, text: "" }).catch(() => {});
  }
});

chrome.tabs.onRemoved.addListener((tabId) => {
  tabMedia.delete(tabId);
  chrome.storage.session.remove(`tab_${tabId}`);
});

async function getServerUrl() {
  const stored = await chrome.storage.sync.get("serverUrl");
  return (stored.serverUrl || DEFAULT_SERVER).replace(/\/+$/, "");
}

// Build a Cookie header for the media URL — login-gated streams (m3u8 behind a
// session) need the same cookies the browser would send. Scoped to the target
// domain only; merges page-domain cookies when the page differs (CDN subdomains).
async function gatherCookies(payload) {
  const urls = [];
  if (payload.url && payload.url.startsWith("http")) urls.push(payload.url);
  if (payload.page_url && payload.page_url !== payload.url) urls.push(payload.page_url);

  const jar = new Map();
  for (const url of urls) {
    try {
      const cookies = await chrome.cookies.getAll({ url });
      for (const c of cookies) {
        if (!jar.has(c.name)) jar.set(c.name, c.value);
      }
    } catch (_) {
      /* host may be restricted */
    }
  }
  if (!jar.size) return null;
  return Array.from(jar, ([name, value]) => `${name}=${value}`).join("; ");
}

async function sendToServer(payload) {
  const server = await getServerUrl();
  if (!payload.cookies) {
    const cookies = await gatherCookies(payload);
    if (cookies) payload = { ...payload, cookies };
  }
  try {
    const res = await fetch(`${server}/api/extension/download`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    if (!res.ok) {
      return { ok: false, error: `Server error ${res.status}` };
    }
    const data = await res.json();
    return { ok: true, jobId: data.job_id, kind: data.kind, watchUrl: server + data.watch_url, server };
  } catch (e) {
    return { ok: false, error: "MediaStrip server not reachable — is it running?" };
  }
}

async function pingServer() {
  const server = await getServerUrl();
  try {
    const res = await fetch(`${server}/api/extension/ping`);
    const data = await res.json();
    return { ok: !!data.ok, server };
  } catch (_) {
    return { ok: false, server };
  }
}

chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
  if (msg.type === "get-media") {
    const tabId = msg.tabId ?? sender.tab?.id;
    loadTab(tabId).then((list) => sendResponse({ items: list }));
    return true;
  }
  if (msg.type === "download") {
    sendToServer(msg.payload).then(sendResponse);
    return true;
  }
  if (msg.type === "ping-server") {
    pingServer().then(sendResponse);
    return true;
  }
});
