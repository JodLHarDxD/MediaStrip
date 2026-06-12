// MediaStrip Catcher — content script (IDM-style integrated catcher)
//
// Goes *inside* the page: actively scans the DOM for media elements AND merges
// the streams the player loaded (sniffed by the background service worker via
// webRequest). Surfaces everything in a floating panel — no URL pasting.
//
// UI runs only in the top frame; embedded players (iframes) are covered by the
// network sniffer, so their streams still show up in the panel.

(() => {
  if (window.top !== window.self) return; // sub-frames: sniffer handles them
  if (window.__mediastripInjected) return; // already injected (manifest + programmatic)
  window.__mediastripInjected = true;

  const items = new Map(); // url -> {url, kind, label, page_url, size, source}
  let panel = null;
  let launcher = null;
  let panelOpen = false;
  let pollTimer = null;
  let mainTimer = null;
  let observer = null;
  let destroyed = false;

  // Extension reloads orphan this script: chrome.runtime dies but timers keep
  // firing. Detect that, go silent, and remove our UI — the background worker
  // re-injects a fresh copy of this script into the tab.
  function alive() {
    try {
      return !destroyed && !!(chrome.runtime && chrome.runtime.id);
    } catch (_) {
      return false;
    }
  }

  function destroy() {
    if (destroyed) return;
    destroyed = true;
    clearInterval(pollTimer);
    clearInterval(mainTimer);
    if (observer) observer.disconnect();
    document.removeEventListener("play", debouncedRefresh, true);
    launcher?.remove();
    panel?.remove();
    window.__mediastripInjected = false;
  }

  const pageUrl = () => location.href;

  // ── classification ──────────────────────────────────────────────────────────
  const isManifest = (u) => /\.(m3u8|mpd)([?#]|$)/i.test(u);
  const isDirectMedia = (u) =>
    /\.(mp4|webm|mkv|mov|m4v|avi|mp3|m4a|aac|flac|ogg|opus|wav|gif)([?#]|$)/i.test(u);
  const isImage = (u) => /\.(jpe?g|png|webp|gif|bmp|tiff?)([?#]|$)/i.test(u);

  function fileLabel(url, fallback) {
    try {
      const p = new URL(url).pathname.split("/").filter(Boolean).pop();
      return p ? decodeURIComponent(p) : fallback;
    } catch (_) {
      return fallback;
    }
  }

  function addItem(url, kind, label, source, size = 0) {
    if (!url || items.has(url)) return false;
    items.set(url, { url, kind, label: label || fileLabel(url, kind), page_url: pageUrl(), size, source });
    return true;
  }

  // ── DOM scan ─────────────────────────────────────────────────────────────────
  function scanDom() {
    let changed = false;

    document.querySelectorAll("video").forEach((v) => {
      const src = v.currentSrc || v.src || "";
      if (src.startsWith("http")) {
        const kind = isManifest(src) ? "manifest" : "direct";
        changed = addItem(src, kind, fileLabel(src, "video"), "page-video") || changed;
      } else if (src.startsWith("blob:") || v.querySelector("source") || v.readyState > 0) {
        // MSE / blob player — let the server extract from the page URL (yt-dlp)
        changed = addItem(pageUrl() + "#page", "page", "This page (player extraction)", "page-player") || changed;
      }
      v.querySelectorAll("source").forEach((s) => {
        const ss = s.src || s.getAttribute("src") || "";
        if (ss.startsWith("http")) {
          changed = addItem(ss, isManifest(ss) ? "manifest" : "direct", fileLabel(ss, "video"), "page-source") || changed;
        }
      });
    });

    document.querySelectorAll("audio").forEach((a) => {
      const src = a.currentSrc || a.src || "";
      if (src.startsWith("http")) changed = addItem(src, "direct", fileLabel(src, "audio"), "page-audio") || changed;
    });

    document.querySelectorAll("img").forEach((img) => {
      const big = (img.naturalWidth >= 400 && img.naturalHeight >= 400);
      const src = img.currentSrc || img.src || "";
      if (big && src.startsWith("http") && isImage(src)) {
        changed = addItem(src, "direct", fileLabel(src, "image"), "page-image") || changed;
      }
    });

    // og:video / twitter:player meta (some sites expose the real file here)
    document
      .querySelectorAll('meta[property="og:video"], meta[property="og:video:url"], meta[property="og:video:secure_url"]')
      .forEach((m) => {
        const u = m.content || "";
        if (u.startsWith("http")) changed = addItem(u, isManifest(u) ? "manifest" : "direct", fileLabel(u, "video"), "og-meta") || changed;
      });

    return changed;
  }

  // ── merge sniffed streams from background ────────────────────────────────────
  function pullSniffed() {
    if (!alive()) return destroy();
    try {
      chrome.runtime.sendMessage({ type: "get-media" }, (res) => {
        if (chrome.runtime.lastError || !res) return;
        let changed = false;
        for (const it of res.items || []) {
          const kind = it.kind === "manifest" ? "manifest" : it.kind === "image" ? "direct" : "direct";
          changed = addItem(it.url, kind, fileLabel(it.url, it.kind), "network", it.size || 0) || changed;
        }
        if (changed) render();
        updateLauncher();
      });
    } catch (_) {
      destroy(); // sendMessage throws synchronously once the context is gone
    }
  }

  function refresh() {
    if (!alive()) return destroy();
    const domChanged = scanDom();
    if (domChanged) render();
    updateLauncher();
    pullSniffed();
  }

  // ── download ─────────────────────────────────────────────────────────────────
  function download(item, btn) {
    if (!alive()) {
      toast("MediaStrip was updated — refresh this page", false);
      return destroy();
    }
    if (btn) btn.classList.add("__ms_busy");
    const payload = {
      url: item.kind === "page" ? item.page_url : item.url,
      kind: item.kind,
      page_url: item.page_url,
    };
    try {
      chrome.runtime.sendMessage({ type: "download", payload }, (res) => {
        if (btn) btn.classList.remove("__ms_busy");
        if (chrome.runtime.lastError) {
          toast("MediaStrip extension error — try reloading the page", false);
          return;
        }
        if (res && res.ok) {
          toast("Sent to MediaStrip ✓ (" + res.kind + ")", true);
          if (btn) { btn.textContent = "✓"; setTimeout(() => (btn.textContent = "⬇"), 1500); }
        } else {
          toast(res?.error || "MediaStrip server not reachable", false);
        }
      });
    } catch (_) {
      if (btn) btn.classList.remove("__ms_busy");
      toast("MediaStrip was updated — refresh this page", false);
      destroy();
    }
  }

  // ── UI: launcher + panel ─────────────────────────────────────────────────────
  function fmtSize(b) {
    if (!b) return "";
    if (b >= 1e9) return (b / 1e9).toFixed(1) + " GB";
    if (b >= 1e6) return (b / 1e6).toFixed(1) + " MB";
    return Math.round(b / 1e3) + " KB";
  }
  const KIND_ICON = { manifest: "🎞", direct: "🎬", page: "📄", image: "🖼" };

  function ensureLauncher() {
    if (launcher) return launcher;
    launcher = document.createElement("div");
    launcher.className = "__ms_launcher";
    launcher.innerHTML =
      '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="7 10 12 15 17 10"/><line x1="12" y1="15" x2="12" y2="3"/></svg>' +
      '<span class="__ms_count">0</span>';
    launcher.addEventListener("click", togglePanel);
    document.documentElement.appendChild(launcher);
    return launcher;
  }

  function updateLauncher() {
    const n = items.size;
    const l = ensureLauncher();
    l.querySelector(".__ms_count").textContent = String(n);
    l.classList.toggle("__ms_visible", n > 0);
  }

  function togglePanel() {
    panelOpen ? closePanel() : openPanel();
  }
  function openPanel() {
    panelOpen = true;
    render();
    panel?.classList.add("__ms_open");
    clearInterval(pollTimer);
    pollTimer = setInterval(refresh, 2500);
  }
  function closePanel() {
    panelOpen = false;
    panel?.classList.remove("__ms_open");
    clearInterval(pollTimer);
  }

  function ensurePanel() {
    if (panel) return panel;
    panel = document.createElement("div");
    panel.className = "__ms_panel";
    panel.innerHTML =
      '<div class="__ms_head"><span class="__ms_title">Media<b>Strip</b> · on this page</span>' +
      '<span class="__ms_x">✕</span></div><div class="__ms_list"></div>' +
      '<div class="__ms_foot"><button class="__ms_pagebtn">⬇ Download whole page (auto-extract)</button></div>';
    panel.querySelector(".__ms_x").addEventListener("click", closePanel);
    panel.querySelector(".__ms_pagebtn").addEventListener("click", () =>
      download({ url: pageUrl(), kind: "page", page_url: pageUrl() })
    );
    document.documentElement.appendChild(panel);
    return panel;
  }

  function render() {
    const p = ensurePanel();
    const list = p.querySelector(".__ms_list");
    list.innerHTML = "";
    if (!items.size) {
      list.innerHTML = '<div class="__ms_empty">No media detected yet. Play the video, then re-open.</div>';
      return;
    }
    // network-sniffed manifests first (the real streams), then page media
    const order = { manifest: 0, direct: 1, image: 2, page: 3 };
    [...items.values()]
      .sort((a, b) => (order[a.kind] - order[b.kind]) || a.label.localeCompare(b.label))
      .forEach((item) => {
        const row = document.createElement("div");
        row.className = "__ms_row";
        const meta = [item.kind, item.source === "network" ? "stream" : "page", fmtSize(item.size)]
          .filter(Boolean).join(" · ");
        row.innerHTML =
          '<div class="__ms_ic">' + (KIND_ICON[item.kind] || "📄") + "</div>" +
          '<div class="__ms_info"><div class="__ms_name"></div><div class="__ms_meta"></div></div>' +
          '<button class="__ms_dl">⬇</button>';
        row.querySelector(".__ms_name").textContent = item.label;
        row.querySelector(".__ms_name").title = item.url;
        row.querySelector(".__ms_meta").textContent = meta;
        const btn = row.querySelector(".__ms_dl");
        btn.addEventListener("click", () => download(item, btn));
        list.appendChild(row);
      });
  }

  function toast(text, ok) {
    const el = document.createElement("div");
    el.className = "__mediastrip_toast" + (ok ? "" : " __mediastrip_toast_err");
    el.textContent = text;
    document.documentElement.appendChild(el);
    requestAnimationFrame(() => el.classList.add("__mediastrip_toast_show"));
    setTimeout(() => {
      el.classList.remove("__mediastrip_toast_show");
      setTimeout(() => el.remove(), 350);
    }, 3000);
  }

  // ── lifecycle ────────────────────────────────────────────────────────────────
  const debounce = (fn, ms) => { let t; return () => { clearTimeout(t); t = setTimeout(fn, ms); }; };
  const debouncedRefresh = debounce(refresh, 800);

  function start() {
    refresh();
    observer = new MutationObserver(debouncedRefresh);
    observer.observe(document.documentElement, { childList: true, subtree: true });
    // catch lazily-attached players / src swaps
    document.addEventListener("play", debouncedRefresh, true);
    mainTimer = setInterval(refresh, 5000);
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", start);
  } else {
    start();
  }
})();
