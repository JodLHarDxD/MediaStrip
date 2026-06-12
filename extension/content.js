// MediaStrip Catcher — IDM-style per-media download buttons
//
// A pill button is pinned to the top-right corner of every real media element
// (not the screen corner). Hover the video → button appears → click → a small
// panel anchored to the media offers the actual downloadable source(s) and
// shows live download progress. Junk (thumbnails, page images) is ignored.
//
// Runs in every frame: embedded players (movie sites, iframes) get their own
// buttons. Network-sniffed HLS/DASH streams are merged in from the background
// service worker per tab.

(() => {
  if (window.__mediastripInjected) return; // manifest + programmatic injection
  window.__mediastripInjected = true;

  const MIN_VIDEO_W = 240, MIN_VIDEO_H = 120; // ignore preview thumbs / ad slivers
  const MIN_AUDIO_W = 180;
  const YT_RE = /(^|\.)youtube\.com$|(^|\.)youtu\.be$/;
  const isTop = window.top === window.self;

  const attached = new Map(); // media el -> {btn, panel, dismissed}
  let destroyed = false;
  let scanTimer = null;
  let posTimer = null;
  let observer = null;

  // Extension reloads orphan this script: chrome.runtime dies but timers keep
  // firing. Detect that, go silent, remove our UI — background re-injects.
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
    clearInterval(scanTimer);
    clearInterval(posTimer);
    if (observer) observer.disconnect();
    document.removeEventListener("play", debouncedScan, true);
    for (const entry of attached.values()) {
      entry.btn.remove();
      entry.panel?.remove();
    }
    attached.clear();
    window.__mediastripInjected = false;
  }

  // ── helpers ──────────────────────────────────────────────────────────────────
  const isManifest = (u) => /\.(m3u8|mpd)([?#]|$)/i.test(u);

  function fileLabel(url, fallback) {
    try {
      const p = new URL(url).pathname.split("/").filter(Boolean).pop();
      return p ? decodeURIComponent(p) : fallback;
    } catch (_) {
      return fallback;
    }
  }

  function fmtSize(b) {
    if (!b) return "";
    if (b >= 1e9) return (b / 1e9).toFixed(1) + " GB";
    if (b >= 1e6) return (b / 1e6).toFixed(1) + " MB";
    return Math.round(b / 1e3) + " KB";
  }

  // Best page URL for server-side extraction (yt-dlp) from this frame
  function extractionUrl() {
    if (location.href.startsWith("http")) return location.href; // top page or embed URL
    return document.referrer && document.referrer.startsWith("http") ? document.referrer : null;
  }

  function getSniffed() {
    return new Promise((resolve) => {
      if (!alive()) return resolve([]);
      try {
        chrome.runtime.sendMessage({ type: "get-media" }, (res) => {
          if (chrome.runtime.lastError || !res) return resolve([]);
          resolve(res.items || []);
        });
      } catch (_) {
        destroy();
        resolve([]);
      }
    });
  }

  // ── scanning: attach a button to every real media element ───────────────────
  function mediaBigEnough(el) {
    const r = el.getBoundingClientRect();
    if (el.tagName === "AUDIO") return r.width >= MIN_AUDIO_W;
    return r.width >= MIN_VIDEO_W && r.height >= MIN_VIDEO_H;
  }

  function scan() {
    if (!alive()) return destroy();
    document.querySelectorAll("video, audio").forEach((el) => {
      if (!attached.has(el) && mediaBigEnough(el)) attach(el);
    });
    for (const [el, entry] of attached) {
      if (!el.isConnected) {
        entry.btn.remove();
        entry.panel?.remove();
        attached.delete(el);
      }
    }
    positionAll();
  }

  const DL_SVG =
    '<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.6" stroke-linecap="round" stroke-linejoin="round"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="7 10 12 15 17 10"/><line x1="12" y1="15" x2="12" y2="3"/></svg>';

  function attach(media) {
    const btn = document.createElement("div");
    btn.className = "__ms_vbtn";
    btn.innerHTML = DL_SVG + "<span>Download</span><span class='__ms_vx' title='Hide for this video'>✕</span>";
    document.documentElement.appendChild(btn);

    const entry = { btn, panel: null, dismissed: false };
    attached.set(media, entry);

    const show = () => {
      if (!entry.dismissed && alive()) btn.classList.add("__ms_vshow");
    };
    const hide = () => {
      if (!entry.panel) btn.classList.remove("__ms_vshow");
    };

    btn.querySelector(".__ms_vx").addEventListener("click", (e) => {
      e.stopPropagation();
      entry.dismissed = true;
      btn.classList.remove("__ms_vshow");
      entry.panel?.remove();
      entry.panel = null;
    });
    btn.addEventListener("click", (e) => {
      if (e.target.classList.contains("__ms_vx")) return;
      openPanel(media, entry);
    });

    media.addEventListener("mouseenter", show);
    media.addEventListener("mousemove", show);
    media.addEventListener("mouseleave", () => {
      setTimeout(() => {
        if (!btn.matches(":hover")) hide();
      }, 400);
    });
    btn.addEventListener("mouseleave", () => {
      if (!media.matches(":hover")) hide();
    });

    position(media, entry);
    show(); // brief reveal so the user knows it's there
    setTimeout(hide, 4000);
  }

  // Pin button to the media element's top-right corner (viewport coordinates)
  function position(media, entry) {
    const r = media.getBoundingClientRect();
    const offscreen =
      r.bottom < 0 || r.top > innerHeight || r.right < 0 || r.left > innerWidth || !mediaBigEnough(media);
    entry.btn.classList.toggle("__ms_voff", offscreen);
    if (offscreen) {
      if (entry.panel) entry.panel.classList.add("__ms_voff");
      return;
    }
    const bw = entry.btn.offsetWidth || 110;
    entry.btn.style.top = Math.max(2, r.top + 10) + "px";
    entry.btn.style.left = Math.max(2, r.right - bw - 10) + "px";
    if (entry.panel) {
      entry.panel.classList.remove("__ms_voff");
      const pw = entry.panel.offsetWidth || 320;
      entry.panel.style.top = Math.max(2, r.top + 46) + "px";
      entry.panel.style.left = Math.min(Math.max(8, r.right - pw - 10), innerWidth - pw - 8) + "px";
    }
  }

  function positionAll() {
    for (const [el, entry] of attached) position(el, entry);
  }

  // ── source resolution: what would we actually download? ─────────────────────
  async function resolveOptions(media) {
    const src = media.currentSrc || media.src || "";
    const opts = [];

    // YouTube: the only correct download is the extractor on the page URL —
    // everything else (thumbnails, range-chunks) is junk
    if (YT_RE.test(location.hostname) && isTop) {
      return [{ label: "Full video — best quality", note: "server extractor", kind: "page", url: location.href, page_url: location.href }];
    }

    if (src.startsWith("http")) {
      opts.push({
        label: fileLabel(src, "Media file"),
        note: isManifest(src) ? "HLS/DASH stream" : "direct file",
        kind: isManifest(src) ? "manifest" : "direct",
        url: src,
        page_url: extractionUrl() || location.href,
      });
    }

    // blob/MSE player → the real streams were sniffed at network level
    if (!src.startsWith("http")) {
      const sniffed = await getSniffed();
      const manifests = sniffed
        .filter((it) => it.kind === "manifest")
        .sort((a, b) => (b.ts || 0) - (a.ts || 0))
        .slice(0, 3);
      manifests.forEach((m, i) => {
        opts.push({
          label: fileLabel(m.url, "Stream") + (manifests.length > 1 ? ` (${i + 1})` : ""),
          note: "sniffed HLS/DASH" + (m.size ? " · " + fmtSize(m.size) : ""),
          kind: "manifest",
          url: m.url,
          page_url: extractionUrl() || location.href,
        });
      });
      const pu = extractionUrl();
      if (pu) {
        opts.push({ label: "Auto-extract from this page", note: "best quality", kind: "page", url: pu, page_url: pu });
      }
    }

    // dedupe by target URL
    return opts.filter((o, i) => opts.findIndex((x) => x.url === o.url) === i);
  }

  // ── panel: source choice → destination → download with live progress ────────
  async function openPanel(media, entry) {
    if (!alive()) {
      toast("MediaStrip was updated — refresh this page", false);
      return destroy();
    }
    if (entry.panel) {
      entry.panel.remove();
      entry.panel = null;
      return;
    }

    const opts = await resolveOptions(media);
    const panel = document.createElement("div");
    panel.className = "__ms_vpanel";

    if (!opts.length) {
      panel.innerHTML =
        "<div class='__ms_vhead'><span>Media<b>Strip</b></span><span class='__ms_vclose'>✕</span></div>" +
        "<div class='__ms_vempty'>No downloadable source detected yet.<br>Play the video for a few seconds, then try again.</div>";
    } else {
      const rows = opts
        .map(
          (o, i) =>
            `<label class='__ms_vopt'><input type='radio' name='__ms_src' value='${i}' ${i === 0 ? "checked" : ""}/>` +
            `<span class='__ms_vopt_info'><span class='__ms_vopt_label'></span><span class='__ms_vopt_note'>${o.note}</span></span></label>`
        )
        .join("");
      panel.innerHTML =
        "<div class='__ms_vhead'><span>Media<b>Strip</b></span><span class='__ms_vclose'>✕</span></div>" +
        `<div class='__ms_vopts'>${rows}</div>` +
        "<div class='__ms_vdest'>Saves to <b>MediaStrip downloads</b> <span class='__ms_vdest_srv'></span></div>" +
        "<button class='__ms_vgo'>⬇ Download</button>" +
        "<div class='__ms_vprog'><div class='__ms_vbar'><div class='__ms_vbar_fill'></div></div><div class='__ms_vstatus'></div><a class='__ms_vopen' target='_blank'>Open in MediaStrip ↗</a></div>";
      // labels via textContent — URLs/filenames are untrusted page data
      panel.querySelectorAll(".__ms_vopt_label").forEach((el, i) => {
        el.textContent = opts[i].label;
        el.title = opts[i].url;
      });
    }

    panel.querySelector(".__ms_vclose").addEventListener("click", () => {
      panel.remove();
      entry.panel = null;
    });

    const goBtn = panel.querySelector(".__ms_vgo");
    if (goBtn) {
      goBtn.addEventListener("click", () => {
        const sel = panel.querySelector("input[name='__ms_src']:checked");
        const opt = opts[parseInt(sel?.value || "0", 10)] || opts[0];
        startDownload(panel, opt);
      });
    }

    document.documentElement.appendChild(panel);
    entry.panel = panel;
    position(media, entry);
  }

  function startDownload(panel, opt) {
    if (!alive()) {
      toast("MediaStrip was updated — refresh this page", false);
      return destroy();
    }
    const goBtn = panel.querySelector(".__ms_vgo");
    goBtn.disabled = true;
    goBtn.textContent = "Starting…";
    const payload = { url: opt.url, kind: opt.kind, page_url: opt.page_url };
    try {
      chrome.runtime.sendMessage({ type: "download", payload }, (res) => {
        if (chrome.runtime.lastError || !res || !res.ok) {
          goBtn.disabled = false;
          goBtn.textContent = "⬇ Download";
          toast(res?.error || "MediaStrip server not reachable", false);
          return;
        }
        const srv = panel.querySelector(".__ms_vdest_srv");
        if (srv) {
          try {
            srv.textContent = "· " + new URL(res.server).host;
          } catch (_) {}
        }
        goBtn.style.display = "none";
        panel.querySelector(".__ms_vprog").classList.add("__ms_von");
        const link = panel.querySelector(".__ms_vopen");
        link.href = res.watchUrl;
        watchJob(panel, res.jobId, res.watchUrl);
      });
    } catch (_) {
      toast("MediaStrip was updated — refresh this page", false);
      destroy();
    }
  }

  // Live progress: background relays the server's SSE stream over a port
  // (content scripts can't fetch cross-origin; the service worker can)
  function watchJob(panel, jobId) {
    const fill = panel.querySelector(".__ms_vbar_fill");
    const status = panel.querySelector(".__ms_vstatus");
    status.textContent = "Starting…";
    let port;
    try {
      port = chrome.runtime.connect({ name: "job-progress" });
    } catch (_) {
      status.textContent = "Running on server — open MediaStrip to watch";
      return;
    }
    port.postMessage({ type: "watch", jobId });
    port.onMessage.addListener((ev) => {
      if (ev.type === "progress") {
        const pct = Math.max(0, Math.min(100, ev.percent || 0));
        fill.style.width = pct + "%";
        status.textContent = pct.toFixed(0) + "%" + (ev.speed ? " · " + ev.speed : "") + (ev.eta ? " · ETA " + ev.eta : "");
      } else if (ev.type === "filename") {
        status.textContent = ev.value;
      } else if (ev.type === "done") {
        fill.style.width = "100%";
        status.textContent = "Saved ✓";
        panel.querySelector(".__ms_vopen").classList.add("__ms_von");
      } else if (ev.type === "error") {
        status.textContent = ev.message || "Download failed";
        status.classList.add("__ms_verr");
      } else if (ev.type === "disconnected") {
        status.textContent = "Running on server — open MediaStrip to watch";
        panel.querySelector(".__ms_vopen").classList.add("__ms_von");
      }
    });
    port.onDisconnect.addListener(() => {
      if (status.textContent !== "Saved ✓" && !status.classList.contains("__ms_verr")) {
        panel.querySelector(".__ms_vopen").classList.add("__ms_von");
      }
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
  const debounce = (fn, ms) => {
    let t;
    return () => {
      clearTimeout(t);
      t = setTimeout(fn, ms);
    };
  };
  const debouncedScan = debounce(scan, 600);

  function start() {
    scan();
    observer = new MutationObserver(debouncedScan);
    observer.observe(document.documentElement, { childList: true, subtree: true });
    document.addEventListener("play", debouncedScan, true); // lazily-attached players
    addEventListener("scroll", positionAll, { passive: true, capture: true });
    addEventListener("resize", positionAll, { passive: true });
    scanTimer = setInterval(scan, 5000);
    posTimer = setInterval(positionAll, 500);
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", start);
  } else {
    start();
  }
})();
