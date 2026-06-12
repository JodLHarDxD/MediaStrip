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
// Byte-range chunk requests (YouTube videoplayback&range=..., MSE players) —
// each one is a partial slice, useless as a download target
const RANGE_PARAM_RE = /[?&]range=\d+-\d+/i;

// Content scripts only auto-inject on navigation. After install/update/reload,
// already-open tabs would have a dead (or no) catcher — inject a fresh one.
chrome.runtime.onInstalled.addListener(async () => {
  const tabs = await chrome.tabs.query({ url: ["http://*/*", "https://*/*"] });
  for (const tab of tabs) {
    try {
      await chrome.scripting.insertCSS({ target: { tabId: tab.id, allFrames: true }, files: ["content.css"] });
      await chrome.scripting.executeScript({ target: { tabId: tab.id, allFrames: true }, files: ["content.js"] });
    } catch (_) {
      /* unscriptable tab (store, chrome://, discarded) — skip */
    }
  }
});

// In-memory cache; chrome.storage.session is the source of truth across SW restarts.
const tabMedia = new Map();

function header(headers, name) {
  const h = (headers || []).find((x) => x.name.toLowerCase() === name);
  return h ? h.value : "";
}

function classify(url, contentType) {
  if (MANIFEST_URL_RE.test(url) || /mpegurl|dash\+xml/i.test(contentType)) return "manifest";
  if (/^(video|audio)\//i.test(contentType) || MEDIA_URL_RE.test(url)) return "media";
  // images deliberately not caught — thumbnails/page chrome are junk, the user
  // wants the actual media (IDM behavior)
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
    if (FRAGMENT_URL_RE.test(details.url) || RANGE_PARAM_RE.test(details.url)) return;

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

// Gather structured cookies for the media + page URLs — login-gated streams
// (YouTube bot-check, m3u8 behind a session) need the same cookies the browser
// would send. Structured (with domain/path) so the server can build a real
// yt-dlp cookie jar. Scoped to the target/page domains only — no blanket dump.
async function gatherCookies(payload) {
  const urls = [];
  if (payload.url && payload.url.startsWith("http")) urls.push(payload.url);
  if (payload.page_url && payload.page_url !== payload.url) urls.push(payload.page_url);

  const jar = new Map();
  for (const url of urls) {
    try {
      const cookies = await chrome.cookies.getAll({ url });
      for (const c of cookies) {
        const key = `${c.domain}|${c.path}|${c.name}`;
        if (!jar.has(key)) {
          jar.set(key, {
            name: c.name,
            value: c.value,
            domain: c.domain,
            path: c.path,
            secure: c.secure,
            expirationDate: c.expirationDate || 0,
          });
        }
      }
    } catch (_) {
      /* host may be restricted */
    }
  }
  return jar.size ? Array.from(jar.values()) : null;
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

// Relay the server's SSE progress stream to the content script over a port —
// content scripts are bound by page CORS, the service worker is not.
chrome.runtime.onConnect.addListener((port) => {
  if (port.name !== "job-progress") return;
  let aborter = null;

  port.onMessage.addListener(async (msg) => {
    if (msg.type !== "watch" || !msg.jobId) return;
    aborter = new AbortController();
    const server = await getServerUrl();
    try {
      const res = await fetch(`${server}/stream/${encodeURIComponent(msg.jobId)}`, {
        signal: aborter.signal,
      });
      const reader = res.body.getReader();
      const decoder = new TextDecoder();
      let buf = "";
      for (;;) {
        const { done, value } = await reader.read();
        if (done) break;
        buf += decoder.decode(value, { stream: true });
        let sep;
        while ((sep = buf.indexOf("\n\n")) !== -1) {
          const chunk = buf.slice(0, sep);
          buf = buf.slice(sep + 2);
          const data = chunk.split("\n").find((l) => l.startsWith("data:"));
          if (!data) continue;
          let ev;
          try {
            ev = JSON.parse(data.slice(5));
          } catch (_) {
            continue;
          }
          if (["progress", "filename", "done", "error", "part"].includes(ev.type)) {
            try {
              port.postMessage(ev);
            } catch (_) {
              aborter.abort();
              return; // content script went away
            }
          }
          if (ev.type === "done" || ev.type === "error") {
            aborter.abort();
            return;
          }
        }
      }
    } catch (_) {
      try {
        port.postMessage({ type: "disconnected" });
      } catch (_) {
        /* port already closed */
      }
    }
  });

  port.onDisconnect.addListener(() => aborter?.abort());
});

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
  if (msg.type === "cancel-job") {
    (async () => {
      const server = await getServerUrl();
      try {
        const res = await fetch(`${server}/api/job/${encodeURIComponent(msg.jobId)}/cancel`, {
          method: "POST",
        });
        sendResponse({ ok: res.ok });
      } catch (_) {
        sendResponse({ ok: false });
      }
    })();
    return true;
  }
});
