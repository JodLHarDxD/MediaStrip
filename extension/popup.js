// MediaStrip Catcher — popup logic

const DEFAULT_SERVER = "https://mediastrip.jodlx.in";
let activeTab = null;
let serverUrl = DEFAULT_SERVER;

const $ = (id) => document.getElementById(id);

function fmtSize(bytes) {
  if (!bytes) return "";
  if (bytes >= 1024 * 1024 * 1024) return (bytes / 1024 ** 3).toFixed(1) + " GB";
  if (bytes >= 1024 * 1024) return (bytes / 1024 ** 2).toFixed(1) + " MB";
  return Math.round(bytes / 1024) + " KB";
}

function shortName(url) {
  try {
    const path = new URL(url).pathname;
    const name = path.split("/").filter(Boolean).pop() || url;
    return decodeURIComponent(name);
  } catch (_) {
    return url;
  }
}

const KIND_ICON = { manifest: "🎞", media: "🎬", image: "🖼" };

function feedback(html) {
  $("feedback").innerHTML = html;
}

function sendDownload(payload, label) {
  feedback("Starting " + label + "…");
  chrome.runtime.sendMessage({ type: "download", payload }, (res) => {
    if (res && res.ok) {
      feedback(
        'Started ✓ — <a href="' + res.watchUrl + '" target="_blank">watch progress in MediaStrip ↗</a>'
      );
    } else {
      feedback('<span style="color:#FF4D6A">' + (res?.error || "Failed") + "</span>");
    }
  });
}

function renderList(items) {
  const list = $("media-list");
  $("count").textContent = String(items.length);
  if (!items.length) {
    list.innerHTML = '<div class="empty">No media sniffed yet — play a video or browse the page.</div>';
    return;
  }
  list.innerHTML = "";
  for (const item of items.slice().reverse()) {
    const row = document.createElement("div");
    row.className = "item";

    const kind = document.createElement("div");
    kind.className = "kind";
    kind.textContent = KIND_ICON[item.kind] || "📄";

    const info = document.createElement("div");
    info.className = "info";
    const name = document.createElement("div");
    name.className = "name";
    name.textContent = shortName(item.url);
    name.title = item.url;
    const meta = document.createElement("div");
    meta.className = "meta";
    meta.textContent = [item.kind, fmtSize(item.size)].filter(Boolean).join(" · ");
    info.append(name, meta);

    const btn = document.createElement("button");
    btn.className = "dl";
    btn.textContent = "⬇";
    btn.title = "Download via MediaStrip";
    btn.addEventListener("click", () => {
      sendDownload(
        {
          url: item.url,
          kind: item.kind === "manifest" ? "manifest" : "direct",
          page_url: activeTab?.url || null,
        },
        shortName(item.url)
      );
    });

    row.append(kind, info, btn);
    list.appendChild(row);
  }
}

async function refreshStatus() {
  chrome.runtime.sendMessage({ type: "ping-server" }, (res) => {
    const on = !!(res && res.ok);
    $("server-dot").classList.toggle("on", on);
    $("server-status").textContent = on ? "connected" : "offline";
  });
}

async function init() {
  const stored = await chrome.storage.sync.get("serverUrl");
  serverUrl = stored.serverUrl || DEFAULT_SERVER;
  $("server-url").value = serverUrl;

  const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
  activeTab = tab;

  chrome.runtime.sendMessage({ type: "get-media", tabId: tab.id }, (res) => {
    renderList(res?.items || []);
  });

  refreshStatus();
}

$("server-save").addEventListener("click", async () => {
  serverUrl = ($("server-url").value.trim() || DEFAULT_SERVER).replace(/\/+$/, "");
  $("server-url").value = serverUrl;
  await chrome.storage.sync.set({ serverUrl });
  feedback("Server saved.");
  refreshStatus();
});

$("download-page").addEventListener("click", () => {
  if (!activeTab?.url || !activeTab.url.startsWith("http")) {
    feedback('<span style="color:#FF4D6A">This page cannot be downloaded.</span>');
    return;
  }
  sendDownload({ url: activeTab.url, kind: "page", page_url: activeTab.url }, "page extraction");
});

$("open-app").addEventListener("click", (e) => {
  e.preventDefault();
  chrome.tabs.create({ url: serverUrl + "/" });
});

init();
