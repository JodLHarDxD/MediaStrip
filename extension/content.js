// MediaStrip Catcher — content script
// Floating download button over <video> and large <img> elements (IDM-style).

(() => {
  let button = null;
  let currentTarget = null;
  let hideTimer = null;

  function topPageUrl() {
    try {
      return window.top.location.href;
    } catch (_) {
      return document.referrer || location.href;
    }
  }

  function payloadFor(el) {
    if (el.tagName === "VIDEO") {
      const src = el.currentSrc || el.src || "";
      if (src.startsWith("http")) {
        const isManifest = /\.(m3u8|mpd)([?#]|$)/i.test(src);
        return {
          url: src,
          kind: isManifest ? "manifest" : "direct",
          page_url: topPageUrl(),
        };
      }
      // blob:/MSE stream — server-side yt-dlp extracts from the page URL instead
      return { url: topPageUrl(), kind: "page", page_url: topPageUrl() };
    }
    return {
      url: el.currentSrc || el.src,
      kind: "direct",
      page_url: topPageUrl(),
    };
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

  function ensureButton() {
    if (button) return button;
    button = document.createElement("div");
    button.className = "__mediastrip_btn";
    button.innerHTML =
      '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="7 10 12 15 17 10"/><line x1="12" y1="15" x2="12" y2="3"/></svg><span>MediaStrip</span>';
    button.addEventListener("mouseenter", () => clearTimeout(hideTimer));
    button.addEventListener("mouseleave", scheduleHide);
    button.addEventListener("click", (e) => {
      e.preventDefault();
      e.stopPropagation();
      if (!currentTarget) return;
      const payload = payloadFor(currentTarget);
      if (!payload.url || !payload.url.startsWith("http")) {
        toast("MediaStrip: no downloadable source found", false);
        return;
      }
      button.classList.add("__mediastrip_btn_busy");
      chrome.runtime.sendMessage({ type: "download", payload }, (res) => {
        button.classList.remove("__mediastrip_btn_busy");
        if (res && res.ok) {
          toast("Sent to MediaStrip ✓ (" + res.kind + ")", true);
        } else {
          toast(res?.error || "MediaStrip server not reachable", false);
        }
      });
    });
    document.documentElement.appendChild(button);
    return button;
  }

  function showFor(el) {
    currentTarget = el;
    const btn = ensureButton();
    const rect = el.getBoundingClientRect();
    if (rect.width < 100 || rect.height < 60) return;
    btn.style.top = Math.max(8, rect.top + 10) + "px";
    btn.style.left = Math.max(8, rect.right - 130) + "px";
    btn.classList.add("__mediastrip_btn_visible");
    clearTimeout(hideTimer);
  }

  function scheduleHide() {
    clearTimeout(hideTimer);
    hideTimer = setTimeout(() => {
      button?.classList.remove("__mediastrip_btn_visible");
      currentTarget = null;
    }, 600);
  }

  function isCandidate(el) {
    if (!el || !el.tagName) return false;
    if (el.tagName === "VIDEO") return true;
    if (el.tagName === "IMG") {
      return (el.naturalWidth >= 256 && el.naturalHeight >= 256) ||
             (el.width >= 256 && el.height >= 256);
    }
    return false;
  }

  document.addEventListener(
    "mouseover",
    (e) => {
      if (isCandidate(e.target)) {
        showFor(e.target);
      } else if (button && !button.contains(e.target)) {
        scheduleHide();
      }
    },
    { passive: true }
  );

  // Keep button glued to the element while scrolling
  document.addEventListener(
    "scroll",
    () => {
      if (currentTarget && button?.classList.contains("__mediastrip_btn_visible")) {
        const rect = currentTarget.getBoundingClientRect();
        button.style.top = Math.max(8, rect.top + 10) + "px";
        button.style.left = Math.max(8, rect.right - 130) + "px";
      }
    },
    { passive: true, capture: true }
  );
})();
