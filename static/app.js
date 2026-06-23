// ─── State ─────────────────────────────────────────────────────────────────
let selectedPlatform = 'tiktok';
let selectedFile = null;
let wmRegions = [];   // user-marked watermark boxes (normalized 0..1) for images
let _wmDraw = null;   // in-progress drag
let catalogBatchId = 0;
let catalogVideoObserver = null;
const activeEventSources = { dl: null, wm: null };

// ─── Smooth Scroll ─────────────────────────────────────────────────────────
function scrollToSection(id, event) {
  if (event) event.preventDefault();
  const el = document.getElementById(id);
  if (!el) return;
  
  if (window.lenis) {
    window.lenis.scrollTo(el, { duration: 1.2, easing: (t) => Math.min(1, 1.001 - Math.pow(2, -10 * t)) });
  } else {
    el.scrollIntoView({ behavior: 'smooth', block: 'start' });
  }
  
  // Close mobile nav if open
  document.getElementById('top-nav')?.classList.remove('mobile-open');
  document.getElementById('mobile-nav-toggle')?.classList.remove('open');
}

function toggleMobileNav() {
  const nav = document.getElementById('top-nav');
  const toggle = document.getElementById('mobile-nav-toggle');
  nav.classList.toggle('mobile-open');
  toggle.classList.toggle('open');
  if (nav.classList.contains('mobile-open')) {
    nav.classList.add('nav-visible');
  }
}

function scrollToCatalog(event) {
  scrollToSection('result-catalog', event);
}

// ─── Hero terminal: paste a URL up top, run the real download below ───────────
function heroDownload(event) {
  if (event) event.preventDefault();
  const url = (document.getElementById('hero-url-input')?.value || '').trim();
  const mainInput = document.getElementById('url-input');
  if (url && mainInput) mainInput.value = url;
  scrollToSection('tool-download');
  if (url) setTimeout(() => startDownload(), 650); // let the scroll settle first
  else document.getElementById('url-input')?.focus();
}

// ─── Paste ──────────────────────────────────────────────────────────────────
async function pasteUrl() {
  try {
    const text = await navigator.clipboard.readText();
    document.getElementById('url-input').value = text;
    showToast('URL pasted', 'info');
  } catch {
    showToast('Allow clipboard access to paste', 'error');
  }
}

function browseFolder() {
  scrollToCatalog();
  showToast('Finished files land in the output catalog below', 'info');
}

// ─── Download ───────────────────────────────────────────────────────────────
async function startDownload() {
  const url = document.getElementById('url-input').value.trim();
  if (!url) { showToast('Paste a URL first', 'error'); return; }

  resetDownloadUI();

  const btn = document.getElementById('download-btn');
  btn.disabled = true;
  btn.querySelector('span').textContent = 'Starting...';

  try {
    const res = await fetch('/download', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ url }),
    });
    if (!res.ok) throw new Error(await res.text());
    const { job_id } = await res.json();
    streamProgress(job_id, 'dl');
  } catch (e) {
    showToast('Failed to start: ' + e.message, 'error');
    resetBtn('dl');
  }
}

function resetDownloadUI() {
  document.getElementById('dl-progress').classList.remove('visible');
  document.getElementById('dl-success').classList.remove('visible');
  document.getElementById('dl-bar').style.width = '0%';
  document.getElementById('dl-log').innerHTML = '';
  document.getElementById('dl-pct').textContent = '0%';
  document.getElementById('dl-speed').textContent = '—';
  document.getElementById('dl-eta').textContent = '—';
  document.getElementById('dl-filename').textContent = 'Detecting...';
}

// ─── Watermark Removal ──────────────────────────────────────────────────────
async function startWatermarkRemoval() {
  if (!selectedFile) { showToast('Drop a file first', 'error'); return; }

  const btn = document.getElementById('wm-btn');
  btn.disabled = true;
  btn.querySelector('span').textContent = 'Processing...';

  document.getElementById('wm-progress').classList.remove('visible');
  document.getElementById('wm-success').classList.remove('visible');
  document.getElementById('wm-bar').style.width = '0%';
  document.getElementById('wm-log').innerHTML = '';

  const fd = new FormData();
  fd.append('file', selectedFile);
  fd.append('platform', selectedPlatform);
  if (wmRegions.length) fd.append('regions', JSON.stringify(wmRegions));

  try {
    const res = await fetch('/remove-watermark', { method: 'POST', body: fd });
    if (!res.ok) throw new Error(await res.text());
    const { job_id } = await res.json();
    streamProgress(job_id, 'wm');
  } catch (e) {
    showToast('Failed to start: ' + e.message, 'error');
    resetBtn('wm');
  }
}

// ─── SSE Progress ────────────────────────────────────────────────────────────
const activeJobIds = { dl: null, wm: null };

function closeEventSource(prefix) {
  activeEventSources[prefix]?.close();
  activeEventSources[prefix] = null;
  activeJobIds[prefix] = null;
  setCancelVisible(prefix, false);
  if (prefix === 'dl') document.getElementById('dl-part')?.setAttribute('hidden', '');
}

function setCancelVisible(prefix, on) {
  const btn = document.getElementById(`${prefix}-cancel`);
  if (btn) {
    btn.style.display = on ? '' : 'none';
    btn.disabled = false;
    btn.textContent = '✕ Cancel';
  }
}

async function cancelJob(prefix) {
  const jobId = activeJobIds[prefix];
  if (!jobId) return;
  const btn = document.getElementById(`${prefix}-cancel`);
  if (btn) { btn.disabled = true; btn.textContent = 'Cancelling…'; }
  try {
    await fetch(`/api/job/${jobId}/cancel`, { method: 'POST' });
    // the job emits its error event ("Download cancelled.") through the stream
  } catch (_) {
    if (btn) { btn.disabled = false; btn.textContent = '✕ Cancel'; }
    showToast('Could not reach the server to cancel', 'error');
  }
}

document.getElementById('dl-cancel')?.addEventListener('click', () => cancelJob('dl'));

function streamProgress(jobId, prefix) {
  closeEventSource(prefix);

  activeJobIds[prefix] = jobId;
  setCancelVisible(prefix, true);
  document.getElementById(`${prefix}-progress`).classList.add('visible');

  const es = new EventSource(`/stream/${jobId}`);
  activeEventSources[prefix] = es;

  es.onmessage = (e) => handleProgressEvent(JSON.parse(e.data), prefix);
  es.onerror = () => {
    closeEventSource(prefix);
    showToast('Connection lost', 'error');
    resetBtn(prefix);
  };
}

function handleProgressEvent(data, p) {
  const bar = document.getElementById(`${p}-bar`);
  const log = document.getElementById(`${p}-log`);

  switch (data.type) {
    case 'filename':
      document.getElementById(`${p}-filename`).textContent = data.value || 'Processing...';
      break;

    case 'progress': {
      const pct = Math.min(100, Math.round(data.percent || 0));
      bar.style.width = `${pct}%`;
      if (p === 'dl') {
        document.getElementById('dl-pct').textContent = `${pct}%`;
        if (data.speed) document.getElementById('dl-speed').textContent = data.speed;
        if (data.eta)   document.getElementById('dl-eta').textContent   = data.eta;
      }
      break;
    }

    case 'log':
      if (data.value && data.value.trim()) appendLog(log, data.value);
      break;

    case 'part': {
      // chunked delivery: a part is ready — user downloads it, confirms, next part starts
      if (p !== 'dl') break;
      const box = document.getElementById('dl-part');
      const title = document.getElementById('dl-part-title');
      const dlLink = document.getElementById('dl-part-download');
      const cont = document.getElementById('dl-part-continue');
      box.hidden = false;
      title.textContent = `Part ${data.index} of ${data.total} ready — ${data.name}`;
      if (data.artifact?.download_url) dlLink.href = data.artifact.download_url;
      cont.hidden = !!data.last; // last part: nothing to continue to
      cont.disabled = false;
      cont.textContent = '✓ Delete part & continue';
      cont.onclick = async () => {
        cont.disabled = true;
        cont.textContent = 'Continuing…';
        try {
          await fetch(`/api/job/${activeJobIds.dl}/continue`, { method: 'POST' });
          box.hidden = true;
        } catch (_) {
          cont.disabled = false;
          cont.textContent = '✓ Delete part & continue';
          showToast('Could not reach the server', 'error');
        }
      };
      if (data.last) {
        appendLog(log, 'All parts delivered — rejoin them on your device (see log above).');
      }
      break;
    }

    case 'done':
      bar.style.width = '100%';
      {
        const artifacts = normalizeArtifacts(data);
        addArtifactsToCatalog(p, artifacts);
        showSuccess(p, artifacts, data.filename || '');
        closeEventSource(p);
        setTimeout(() => scrollToCatalog(), 260);
      }
      break;

    case 'error':
      appendLog(log, `ERROR: ${data.message}`, 'log-error');
      showToast(data.message || 'An error occurred', 'error');
      closeEventSource(p);
      resetBtn(p);
      break;
  }
}

function appendLog(el, text, cls = '') {
  const d = document.createElement('div');
  d.className = `log-line ${cls}`;
  d.textContent = text;
  el.appendChild(d);
  el.scrollTop = el.scrollHeight;
  while (el.children.length > 300) el.removeChild(el.firstChild);
}

function inferKindFromName(name = '') {
  const ext = basename(name).split('.').pop()?.toLowerCase() || '';
  if (['jpg', 'jpeg', 'png', 'webp', 'gif', 'bmp', 'tiff'].includes(ext)) return 'image';
  if (['mp4', 'mov', 'avi', 'mkv', 'webm', 'm4v'].includes(ext)) return 'video';
  return 'file';
}

function basename(path) {
  return (path || '').split(/[/\\]/).pop();
}

function normalizeArtifacts(data) {
  if (Array.isArray(data?.artifacts) && data.artifacts.length) {
    return data.artifacts.filter(item => item && item.url && item.download_url);
  }

  const rawFiles = Array.isArray(data?.files) ? data.files.filter(Boolean) : [];
  const fallbackFiles = rawFiles.length ? rawFiles : (data?.filename ? [data.filename] : []);
  return fallbackFiles.map(filePath => ({
    name: basename(filePath),
    kind: inferKindFromName(filePath),
    url: filePath,
    download_url: filePath,
    poster_url: null,
    size_bytes: 0,
  }));
}

function formatCatalogTimestamp(date = new Date()) {
  return date.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
}

function createCatalogCard(artifact, index) {
  const item = {
    kind: 'file',
    poster_url: null,
    size_bytes: 0,
    ...artifact,
  };

  const card = document.createElement('article');
  card.className = 'catalog-card';
  card.style.setProperty('--card-index', index);
  card.style.setProperty('--stack-index', index);

  const media = document.createElement('div');
  media.className = 'catalog-card-media';

  if (item.kind === 'image') {
    const img = document.createElement('img');
    img.src = item.url;
    img.alt = item.name;
    img.loading = 'lazy';
    media.appendChild(img);
  } else if (item.kind === 'video') {
    const video = document.createElement('video');
    video.src = item.url;
    video.poster = item.poster_url || '';
    video.muted = true;
    video.loop = true;
    video.preload = 'metadata';
    video.playsInline = true;
    video.setAttribute('playsinline', '');
    media.appendChild(video);

    const playBadge = document.createElement('div');
    playBadge.className = 'catalog-play';
    playBadge.innerHTML = `
      <svg width="18" height="18" viewBox="0 0 24 24" fill="currentColor" aria-hidden="true">
        <path d="M8 5v14l11-7z"></path>
      </svg>
    `;
    media.appendChild(playBadge);
  } else {
    const placeholder = document.createElement('div');
    placeholder.className = 'catalog-file-placeholder';
    placeholder.textContent = item.name;
    media.appendChild(placeholder);
  }

  const top = document.createElement('div');
  top.className = 'catalog-card-top';

  const chip = document.createElement('span');
  chip.className = `catalog-chip ${item.kind}`;
  chip.textContent = item.kind;
  top.appendChild(chip);

  const openTop = document.createElement('a');
  openTop.className = 'catalog-open';
  openTop.href = item.url;
  openTop.target = '_blank';
  openTop.rel = 'noopener';
  openTop.setAttribute('aria-label', `Open ${item.name}`);
  openTop.innerHTML = `
    <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
      <path d="M14 3h7v7"></path>
      <path d="M10 14L21 3"></path>
      <path d="M21 14v7h-7"></path>
      <path d="M3 10V3h7"></path>
      <path d="M3 21l11-11"></path>
    </svg>
  `;
  top.appendChild(openTop);
  media.appendChild(top);

  const body = document.createElement('div');
  body.className = 'catalog-card-body';

  const name = document.createElement('div');
  name.className = 'catalog-card-name';
  name.textContent = item.name;

  const meta = document.createElement('div');
  meta.className = 'catalog-card-meta';
  const format = document.createElement('span');
  format.textContent = basename(item.name).split('.').pop()?.toUpperCase() || 'FILE';
  const size = document.createElement('span');
  size.textContent = item.size_bytes ? fmtBytes(item.size_bytes) : 'READY';
  meta.append(format, size);

  const actions = document.createElement('div');
  actions.className = 'catalog-actions';

  const download = document.createElement('a');
  download.className = 'catalog-download';
  download.href = item.download_url || item.url;
  download.setAttribute('download', item.name);
  download.textContent = 'Download';

  const openInline = document.createElement('a');
  openInline.className = 'catalog-open-inline';
  openInline.href = item.url;
  openInline.target = '_blank';
  openInline.rel = 'noopener';
  openInline.setAttribute('aria-label', `Preview ${item.name}`);
  openInline.innerHTML = `
    <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
      <path d="M2 12s3.5-6 10-6 10 6 10 6-3.5 6-10 6S2 12 2 12z"></path>
      <circle cx="12" cy="12" r="3"></circle>
    </svg>
  `;

  actions.append(download, openInline);
  body.append(name, meta, actions);
  card.append(media, body);
  return card;
}

function registerCatalogVideos(scope = document) {
  const videos = scope.querySelectorAll('.catalog-card video');
  videos.forEach(video => {
    if (video.dataset.catalogBound === 'true') return;
    video.dataset.catalogBound = 'true';

    video.addEventListener('mouseenter', () => {
      video.play().catch(() => {});
    });
    video.addEventListener('mouseleave', () => {
      video.pause();
    });

    if (catalogVideoObserver) {
      catalogVideoObserver.observe(video);
    }
  });
}

const catalogSeenUrls = new Set();

function addArtifactsToCatalog(prefix, artifacts, when = new Date()) {
  if (!Array.isArray(artifacts) || !artifacts.length) return;
  // a job can arrive twice (history load + live SSE replay) — show each file once
  artifacts = artifacts.filter(a => a?.url && !catalogSeenUrls.has(a.url));
  if (!artifacts.length) return;
  artifacts.forEach(a => catalogSeenUrls.add(a.url));

  const stream = document.getElementById('results-stream');
  const empty = document.getElementById('results-empty');
  const clear = document.getElementById('results-clear');
  if (!stream || !empty || !clear) return;

  empty.hidden = true;
  clear.hidden = false;

  const batch = document.createElement('article');
  batch.className = 'catalog-group is-entering';
  batch.dataset.batchId = String(++catalogBatchId);

  const head = document.createElement('div');
  head.className = 'catalog-group-head';

  const titleWrap = document.createElement('div');
  const kicker = document.createElement('div');
  kicker.className = 'catalog-group-kicker';
  kicker.textContent = prefix === 'dl' ? 'Download delivery' : 'Processed delivery';
  const title = document.createElement('div');
  title.className = 'catalog-group-title';
  title.textContent = prefix === 'dl' ? 'Fresh media ready' : 'Clean render ready';
  titleWrap.append(kicker, title);

  const meta = document.createElement('div');
  meta.className = 'catalog-group-meta';
  meta.textContent = `${artifacts.length} ${artifacts.length === 1 ? 'file' : 'files'} · ${formatCatalogTimestamp(when)}`;

  head.append(titleWrap, meta);

  const grid = document.createElement('div');
  grid.className = 'catalog-group-grid';
  artifacts.forEach((artifact, index) => {
    grid.appendChild(createCatalogCard(artifact, index));
  });

  batch.append(head, grid);
  stream.prepend(batch);
  registerCatalogVideos(batch);

  requestAnimationFrame(() => {
    requestAnimationFrame(() => {
      batch.classList.remove('is-entering');
      batch.classList.add('spread');
    });
  });
}

function clearCatalog() {
  const stream = document.getElementById('results-stream');
  const empty = document.getElementById('results-empty');
  const clear = document.getElementById('results-clear');
  if (!stream || !empty || !clear) return;

  stream.querySelectorAll('video').forEach(video => {
    video.pause();
    if (catalogVideoObserver) catalogVideoObserver.unobserve(video);
  });
  stream.innerHTML = '';
  empty.hidden = false;
  clear.hidden = true;
  showToast('Catalog cleared from this session', 'info');
}

function showSuccess(prefix, artifacts, filename) {
  const el   = document.getElementById(`${prefix}-success`);
  const file = document.getElementById(`${prefix}-success-file`);
  const savedArtifacts = Array.isArray(artifacts) ? artifacts.filter(Boolean) : [];

  if (savedArtifacts.length === 1) {
    file.textContent = `Ready in catalog: ${savedArtifacts[0].name}`;
  } else if (savedArtifacts.length > 1) {
    file.textContent = `Ready in catalog: ${savedArtifacts.length} files waiting to download`;
  } else if (filename) {
    file.textContent = `Ready in catalog: ${basename(filename)}`;
  } else {
    file.textContent = 'Files are ready in the catalog';
  }

  el.classList.add('visible');
  showToast(prefix === 'dl' ? 'Download delivered to catalog' : 'Processing delivered to catalog', 'success');
  resetBtn(prefix);
}

function resetBtn(prefix) {
  const btn   = document.getElementById(prefix === 'dl' ? 'download-btn' : 'wm-btn');
  const label = prefix === 'dl' ? 'Download' : 'Process File';
  btn.disabled = false;
  btn.querySelector('span').textContent = label;
}

function openFolder(type) {
  scrollToCatalog();
  showToast('Use the output catalog to preview and download finished files', 'info');
}

// ─── Platform Select ─────────────────────────────────────────────────────────
function selectPlatform(platform) {
  selectedPlatform = platform;
  document.querySelectorAll('.platform-card').forEach(el =>
    el.classList.toggle('selected', el.dataset.platform === platform)
  );
}

// ─── Watermark region marker (image) ─────────────────────────────────────────
function setupWmMarker(dataUrl) {
  const marker = document.getElementById('wm-marker');
  if (!marker) return;
  document.getElementById('wm-marker-img').src = dataUrl;
  marker.hidden = false;
  wmRegions = [];
  renderWmBoxes();
  bindWmMarker();
}

function hideWmMarker() {
  const marker = document.getElementById('wm-marker');
  if (marker) marker.hidden = true;
  wmRegions = [];
}

function clearWmRegions() {
  wmRegions = [];
  renderWmBoxes();
}

function renderWmBoxes(live) {
  const boxesEl = document.getElementById('wm-marker-boxes');
  if (!boxesEl) return;
  boxesEl.innerHTML = '';
  const make = (xf, yf, wf, hf, idx) => {
    const el = document.createElement('div');
    el.className = 'wm-marker-box';
    el.style.left = (xf * 100) + '%';
    el.style.top = (yf * 100) + '%';
    el.style.width = (wf * 100) + '%';
    el.style.height = (hf * 100) + '%';
    if (idx != null) {
      const x = document.createElement('div');
      x.className = 'wm-marker-box-x';
      x.textContent = '×';
      x.onclick = (ev) => { ev.stopPropagation(); wmRegions.splice(idx, 1); renderWmBoxes(); };
      el.appendChild(x);
    }
    boxesEl.appendChild(el);
  };
  wmRegions.forEach((b, i) => make(b.xf, b.yf, b.wf, b.hf, i));
  if (live) {
    make(Math.min(live.x0, live.x1), Math.min(live.y0, live.y1),
         Math.abs(live.x1 - live.x0), Math.abs(live.y1 - live.y0), null);
  }
  const n = wmRegions.length;
  document.getElementById('wm-marker-count').textContent = `${n} region${n === 1 ? '' : 's'} marked`;
}

function bindWmMarker() {
  const stage = document.getElementById('wm-marker-stage');
  if (!stage || stage._wmBound) return;
  stage._wmBound = true;

  const rel = (e) => {
    const r = stage.getBoundingClientRect();
    const cx = e.touches ? e.touches[0].clientX : e.clientX;
    const cy = e.touches ? e.touches[0].clientY : e.clientY;
    return {
      x: Math.min(Math.max((cx - r.left) / r.width, 0), 1),
      y: Math.min(Math.max((cy - r.top) / r.height, 0), 1),
    };
  };
  const onDown = (e) => {
    if (e.target.classList.contains('wm-marker-box-x')) return; // let delete handle it
    e.preventDefault();
    const p = rel(e);
    _wmDraw = { x0: p.x, y0: p.y, x1: p.x, y1: p.y };
  };
  const onMove = (e) => {
    if (!_wmDraw) return;
    e.preventDefault();
    const p = rel(e);
    _wmDraw.x1 = p.x; _wmDraw.y1 = p.y;
    renderWmBoxes(_wmDraw);
  };
  const onUp = () => {
    if (!_wmDraw) return;
    const d = _wmDraw; _wmDraw = null;
    const wf = Math.abs(d.x1 - d.x0), hf = Math.abs(d.y1 - d.y0);
    if (wf > 0.01 && hf > 0.01) {
      wmRegions.push({ xf: Math.min(d.x0, d.x1), yf: Math.min(d.y0, d.y1), wf, hf });
    }
    renderWmBoxes();
  };

  stage.addEventListener('mousedown', onDown);
  window.addEventListener('mousemove', onMove);
  window.addEventListener('mouseup', onUp);
  stage.addEventListener('touchstart', onDown, { passive: false });
  window.addEventListener('touchmove', onMove, { passive: false });
  window.addEventListener('touchend', onUp);
}

// ─── Drag & Drop ─────────────────────────────────────────────────────────────
function handleDragOver(e) {
  e.preventDefault();
  e.stopPropagation();
  document.getElementById('drop-zone').classList.add('drag-over');
}

function handleDragLeave(e) {
  e.preventDefault();
  document.getElementById('drop-zone').classList.remove('drag-over');
}

function handleDrop(e) {
  e.preventDefault();
  e.stopPropagation();
  document.getElementById('drop-zone').classList.remove('drag-over');
  const file = e.dataTransfer.files[0];
  if (file) processFile(file);
}

function handleFileSelect(e) {
  const file = e.target.files[0];
  if (file) processFile(file);
}

function processFile(file) {
  const validTypes = [
    'video/mp4','video/quicktime','video/x-msvideo','video/x-matroska','video/webm',
    'image/jpeg','image/png','image/webp','image/tiff',
  ];
  const validExts = /\.(mp4|mov|avi|mkv|webm|jpg|jpeg|png|webp|tiff)$/i;

  if (!validTypes.includes(file.type) && !validExts.test(file.name)) {
    showToast('Unsupported file type', 'error');
    return;
  }

  selectedFile = file;
  const isImage = file.type.startsWith('image/');

  document.getElementById('drop-idle').style.display = 'none';
  const preview = document.getElementById('drop-preview');
  preview.classList.add('visible');
  document.getElementById('preview-name').textContent = file.name;
  document.getElementById('preview-meta').textContent = `${isImage ? 'Image' : 'Video'} · ${fmtBytes(file.size)}`;

  const reader = new FileReader();

  if (isImage) {
    reader.onload = (ev) => {
      const img = new Image();
      img.onload = () => {
        document.getElementById('preview-thumb').src = ev.target.result;
        document.getElementById('preview-meta').textContent =
          `Image · ${img.width}×${img.height} · ${fmtBytes(file.size)}`;
        setupWmMarker(ev.target.result);   // show the mark-the-watermark stage
      };
      img.src = ev.target.result;
    };
    reader.readAsDataURL(file);
  } else {
    hideWmMarker();   // marking is image-only for now
    reader.onload = (ev) => {
      const vid = document.createElement('video');
      vid.src = ev.target.result;
      vid.currentTime = 0.5;
      vid.onloadedmetadata = () => {
        document.getElementById('preview-meta').textContent =
          `Video · ${fmtDuration(vid.duration)} · ${fmtBytes(file.size)}`;
      };
      vid.onseeked = () => {
        const c = document.createElement('canvas');
        c.width = 160; c.height = 90;
        c.getContext('2d').drawImage(vid, 0, 0, 160, 90);
        document.getElementById('preview-thumb').src = c.toDataURL();
      };
    };
    reader.readAsDataURL(file);
  }

  showToast(`${file.name} loaded`, 'success');
}

// ─── Utilities ───────────────────────────────────────────────────────────────
function fmtBytes(b) {
  if (b < 1024)           return `${b} B`;
  if (b < 1048576)        return `${(b/1024).toFixed(1)} KB`;
  if (b < 1073741824)     return `${(b/1048576).toFixed(1)} MB`;
  return `${(b/1073741824).toFixed(2)} GB`;
}

function fmtDuration(s) {
  const m = Math.floor(s / 60), sec = Math.floor(s % 60);
  return `${m}:${sec.toString().padStart(2,'0')}`;
}

// ─── Toast ────────────────────────────────────────────────────────────────────
function showToast(message, type = 'info') {
  const container = document.getElementById('toast-container');
  const toast = document.createElement('div');
  toast.className = `toast ${type}`;
  const icons = { success: '✓', error: '✕', info: 'ℹ' };
  toast.innerHTML = `<span>${icons[type] || '•'}</span><span>${message}</span>`;
  container.appendChild(toast);
  setTimeout(() => {
    toast.classList.add('out');
    setTimeout(() => toast.remove(), 320);
  }, 3500);
}

// ─── Scroll Nav (fade in after scroll) ──────────────────────────────────────
function initScrollNav() {
  const nav = document.getElementById('top-nav');
  if (!nav) return;
  let ticking = false;
  const onScroll = () => {
    if (window.scrollY > 80) nav.classList.add('nav-visible');
    else if (!nav.classList.contains('mobile-open')) nav.classList.remove('nav-visible');
    ticking = false;
  };
  window.addEventListener('scroll', () => {
    if (!ticking) { requestAnimationFrame(onScroll); ticking = true; }
  }, { passive: true });
}

// ─── Intersection Reveal ────────────────────────────────────────────────────
function initReveal() {
  const els = document.querySelectorAll('.reveal');
  if (!('IntersectionObserver' in window)) {
    els.forEach(el => el.classList.add('visible'));
    return;
  }
  const io = new IntersectionObserver((entries) => {
    entries.forEach(entry => {
      if (entry.isIntersecting) {
        entry.target.classList.add('visible');
        io.unobserve(entry.target);
      }
    });
  }, { threshold: 0.15, rootMargin: '0px 0px -60px 0px' });
  els.forEach(el => io.observe(el));
}

function initCatalogVideoObserver() {
  if (!('IntersectionObserver' in window)) return;

  catalogVideoObserver = new IntersectionObserver((entries) => {
    entries.forEach(entry => {
      const video = entry.target;
      if (entry.isIntersecting) {
        video.play().catch(() => {});
      } else {
        video.pause();
      }
    });
  }, { threshold: 0.45 });
}

// ─── Coordinates Ticker ─────────────────────────────────────────────────────
function initCoordsTicker() {
  const track = document.getElementById('coords-track');
  if (!track) return;

  const coords = [
    { lat: '47.3128°N', lon: '13.5551°E', tag: 'KRNRSK',  frame: '0042' },
    { lat: '35.6895°N', lon: '139.6917°E', tag: 'TKY-SHBY', frame: '0118' },
    { lat: '40.7128°N', lon: '74.0060°W',  tag: 'NYC-MH',   frame: '0206' },
    { lat: '55.7558°N', lon: '37.6173°E',  tag: 'MSK-CENT', frame: '0289' },
    { lat: '51.5074°N', lon: '0.1278°W',   tag: 'LDN-WMS',  frame: '0334' },
    { lat: '48.8566°N', lon: '2.3522°E',   tag: 'PRS-NEFL', frame: '0402' },
    { lat: '34.0522°N', lon: '118.2437°W', tag: 'LAX-DTLA', frame: '0455' },
    { lat: '25.7617°N', lon: '80.1918°W',  tag: 'MIA-SOBE', frame: '0517' },
    { lat: '37.7749°N', lon: '122.4194°W', tag: 'SFO-SOMA', frame: '0588' },
    { lat: '41.8781°N', lon: '87.6298°W',  tag: 'CHI-LPGS', frame: '0632' },
    { lat: '52.5200°N', lon: '13.4050°E',  tag: 'BER-MTTE', frame: '0701' },
    { lat: '59.3293°N', lon: '18.0686°E',  tag: 'STK-NRMM', frame: '0759' },
    { lat: '60.1699°N', lon: '24.9384°E',  tag: 'HLS-KMPL', frame: '0814' },
    { lat: '13.7563°N', lon: '100.5018°E', tag: 'BKK-SIOM', frame: '0878' },
    { lat: '22.3193°N', lon: '114.1694°E', tag: 'HKG-CNTR', frame: '0932' },
    { lat: '19.0760°N', lon: '72.8777°E',  tag: 'BOM-BKC',  frame: '0984' },
    { lat: '28.6139°N', lon: '77.2090°E',  tag: 'DEL-CP',   frame: '1021' },
    { lat: '01.3521°N', lon: '103.8198°E', tag: 'SIN-MBAY', frame: '1082' },
  ];

  const buildItem = (c) => `
    <span class="coord-item">
      <span class="coord-dot"></span>
      <span>${c.lat} · ${c.lon}</span>
      <span class="coord-sep">//</span>
      <span>${c.tag}</span>
      <span class="coord-sep">//</span>
      <span>FRAME ${c.frame}</span>
    </span>
  `;

  // Duplicate sequence for seamless loop
  track.innerHTML = coords.map(buildItem).join('') + coords.map(buildItem).join('');
}

// ─── Stat Counters ──────────────────────────────────────────────────────────
function initStatsCounters() {
  const stats = document.querySelectorAll('.stat-num');
  if (!stats.length) return;

  const easeOut = (t) => 1 - Math.pow(1 - t, 3);

  const animate = (el) => {
    const target = parseFloat(el.dataset.count || '0');
    const prefix = el.dataset.prefix || '';
    const suffix = el.dataset.suffix || '';
    const duration = 1600;
    const start = performance.now();

    if (target === 0) { el.textContent = `${prefix}0${suffix}`; return; }

    const step = (now) => {
      const t = Math.min(1, (now - start) / duration);
      const v = Math.round(target * easeOut(t));
      const display = target >= 1000 ? v.toLocaleString() : v;
      el.textContent = `${prefix}${display}${suffix}`;
      if (t < 1) requestAnimationFrame(step);
    };
    requestAnimationFrame(step);
  };

  if (!('IntersectionObserver' in window)) { stats.forEach(animate); return; }

  const io = new IntersectionObserver((entries) => {
    entries.forEach(entry => {
      if (entry.isIntersecting) {
        animate(entry.target);
        io.unobserve(entry.target);
      }
    });
  }, { threshold: 0.4 });
  stats.forEach(el => io.observe(el));
}

// ─── Feature Card Cursor Glow ───────────────────────────────────────────────
function initFeatureCards() {
  document.querySelectorAll('.feature-card').forEach(card => {
    card.addEventListener('mousemove', (e) => {
      const r = card.getBoundingClientRect();
      card.style.setProperty('--x', `${e.clientX - r.left}px`);
      card.style.setProperty('--y', `${e.clientY - r.top}px`);
    });
  });
}

// ─── Site Loader ──────────────────────────────────────────────────────────────
function initLoader() {
  // Preloader disabled
  document.body.classList.remove('is-preloading');
  document.body.classList.add('app-ready');
}

// ─── Init ─────────────────────────────────────────────────────────────────────
document.addEventListener('DOMContentLoaded', () => {
  initLoader();
  initMotionEngine();

  document.getElementById('url-input')?.addEventListener('keydown', e => {
    if (e.key === 'Enter') startDownload();
  });

  initScrollNav();
  initReveal();
  initCatalogVideoObserver();
  initCoordsTicker();
  initStatsCounters();
  initFeatureCards();

  // Everything already on the server — so a fresh visit shows the catalog
  loadCatalogHistory();

  // Extension-triggered jobs land here via /?job=<id> — attach to its SSE stream
  const extJob = new URLSearchParams(location.search).get('job');
  if (extJob) {
    document.getElementById('tool-download')?.scrollIntoView({ behavior: 'smooth' });
    streamProgress(extJob, 'dl');
  }
});

async function freeServerStorage(btn) {
  btn.disabled = true;
  btn.textContent = 'Freeing…';
  try {
    await fetch('/api/cleanup', { method: 'POST' });
    showToast('Server storage freed', 'info');
    clearCatalog();
    catalogSeenUrls.clear();
  } catch (_) {
    showToast('Could not reach the server', 'error');
  }
  btn.disabled = false;
  btn.textContent = 'Free Server Storage';
}

async function loadCatalogHistory() {
  try {
    const res = await fetch('/api/catalog');
    if (!res.ok) return;
    const data = await res.json();
    for (const group of data.groups || []) {
      addArtifactsToCatalog(
        group.source === 'output' ? 'wm' : 'dl',
        group.artifacts,
        new Date(group.ts * 1000)
      );
    }
  } catch (_) {
    /* server unreachable — catalog stays empty */
  }
}

// ─── WebGL Fluid Background ──────────────────────────────────────────────────
function initFluidBackground() {
  const canvas = document.getElementById('gradient-canvas');
  if (!canvas) return;
  const gl = canvas.getContext('webgl') || canvas.getContext('experimental-webgl');
  if (!gl) return;

  const vsSource = `
    attribute vec2 position;
    void main() {
      gl_Position = vec4(position, 0.0, 1.0);
    }
  `;

  // Fluid noise shader (cyan/blue aurora style) + Mouse Ripples
  const fsSource = `
    precision mediump float;
    uniform float u_time;
    uniform vec2 u_resolution;
    uniform vec2 u_mouse;

    vec3 mod289(vec3 x) { return x - floor(x * (1.0 / 289.0)) * 289.0; }
    vec2 mod289(vec2 x) { return x - floor(x * (1.0 / 289.0)) * 289.0; }
    vec3 permute(vec3 x) { return mod289(((x*34.0)+1.0)*x); }
    float snoise(vec2 v) {
      const vec4 C = vec4(0.211324865405187, 0.366025403784439, -0.577350269189626, 0.024390243902439);
      vec2 i  = floor(v + dot(v, C.yy) );
      vec2 x0 = v -   i + dot(i, C.xx);
      vec2 i1 = (x0.x > x0.y) ? vec2(1.0, 0.0) : vec2(0.0, 1.0);
      vec4 x12 = x0.xyxy + C.xxzz;
      x12.xy -= i1;
      i = mod289(i);
      vec3 p = permute( permute( i.y + vec3(0.0, i1.y, 1.0 )) + i.x + vec3(0.0, i1.x, 1.0 ));
      vec3 m = max(0.5 - vec3(dot(x0,x0), dot(x12.xy,x12.xy), dot(x12.zw,x12.zw)), 0.0);
      m = m*m;
      m = m*m;
      vec3 x = 2.0 * fract(p * C.www) - 1.0;
      vec3 h = abs(x) - 0.5;
      vec3 ox = floor(x + 0.5);
      vec3 a0 = x - ox;
      m *= 1.79284291400159 - 0.85373472095314 * ( a0*a0 + h*h );
      vec3 g;
      g.x  = a0.x  * x0.x  + h.x  * x0.y;
      g.yz = a0.yz * x12.xz + h.yz * x12.yw;
      return 130.0 * dot(m, g);
    }

    void main() {
      vec2 st = gl_FragCoord.xy/u_resolution.xy;
      st.x *= u_resolution.x/u_resolution.y;
      
      vec2 mouse = u_mouse / u_resolution.xy;
      mouse.x *= u_resolution.x/u_resolution.y;
      // invert Y for WebGL coords
      mouse.y = (u_resolution.y - u_mouse.y) / u_resolution.y; 
      
      vec2 pos = vec2(st*1.5);
      
      // Mouse interaction (Fluid displacement)
      float dist = distance(st, mouse);
      float ripple = smoothstep(0.4, 0.0, dist);
      pos += (st - mouse) * ripple * 0.5;

      float df = snoise(pos - u_time * 0.05);
      float noise = snoise(pos + df + u_time * 0.1);
      
      // Electric-blue aurora — green channel pulled down to kill the teal/green wash
      vec3 color = mix(vec3(0.02, 0.10, 0.45), vec3(0.10, 0.45, 1.0), smoothstep(-0.2, 0.8, noise));
      color += mix(vec3(0.0), vec3(0.42, 0.10, 0.95), smoothstep(0.4, 0.9, snoise(pos - u_time * 0.1)));

      color += vec3(0.12, 0.40, 1.0) * max(0.0, noise*noise*noise*2.0);
      color += vec3(0.10, 0.35, 0.95) * ripple * 1.5; // Brighten around mouse

      float vignette = smoothstep(1.5, 0.0, length(st - vec2(0.5)));
      color *= vignette;

      gl_FragColor = vec4(color, 1.0);
    }
  `;

  function createShader(type, source) {
    const shader = gl.createShader(type);
    gl.shaderSource(shader, source);
    gl.compileShader(shader);
    return shader;
  }

  const program = gl.createProgram();
  gl.attachShader(program, createShader(gl.VERTEX_SHADER, vsSource));
  gl.attachShader(program, createShader(gl.FRAGMENT_SHADER, fsSource));
  gl.linkProgram(program);
  gl.useProgram(program);

  const posBuffer = gl.createBuffer();
  gl.bindBuffer(gl.ARRAY_BUFFER, posBuffer);
  gl.bufferData(gl.ARRAY_BUFFER, new Float32Array([-1,-1, 1,-1, -1,1, 1,1]), gl.STATIC_DRAW);

  const posLoc = gl.getAttribLocation(program, 'position');
  gl.enableVertexAttribArray(posLoc);
  gl.vertexAttribPointer(posLoc, 2, gl.FLOAT, false, 0, 0);

  const timeLoc = gl.getUniformLocation(program, 'u_time');
  const resLoc = gl.getUniformLocation(program, 'u_resolution');
  const mouseLoc = gl.getUniformLocation(program, 'u_mouse');

  function resize() {
    canvas.width = window.innerWidth;
    canvas.height = window.innerHeight;
    gl.viewport(0, 0, canvas.width, canvas.height);
    gl.uniform2f(resLoc, canvas.width, canvas.height);
  }
  window.addEventListener('resize', resize);
  resize();

  let targetMouse = {x: window.innerWidth/2, y: window.innerHeight/2};
  let currentMouse = {x: window.innerWidth/2, y: window.innerHeight/2};

  window.addEventListener('mousemove', (e) => {
    targetMouse.x = e.clientX;
    targetMouse.y = e.clientY;
  });

  function render(time) {
    gl.uniform1f(timeLoc, time * 0.001);
    
    // Smooth mouse follow for fluid
    currentMouse.x += (targetMouse.x - currentMouse.x) * 0.05;
    currentMouse.y += (targetMouse.y - currentMouse.y) * 0.05;
    gl.uniform2f(mouseLoc, currentMouse.x, currentMouse.y);

    gl.drawArrays(gl.TRIANGLE_STRIP, 0, 4);
    requestAnimationFrame(render);
  }
  requestAnimationFrame(render);
}

// ─── Motion Engine (GSAP + Lenis + Fluid Physics) ─────────────────────────────
function initMotionEngine() {
  if (typeof Lenis === 'undefined' || typeof gsap === 'undefined') return;

  initFluidBackground();

  // 1. Lenis Smooth Scroll
  const lenis = new Lenis({
    duration: 1.2,
    easing: (t) => Math.min(1, 1.001 - Math.pow(2, -10 * t)),
    direction: 'vertical',
    gestureDirection: 'vertical',
    smooth: true,
  });
  window.lenis = lenis;

  function raf(time) {
    lenis.raf(time);
    requestAnimationFrame(raf);
  }
  requestAnimationFrame(raf);

  if (typeof ScrollTrigger !== 'undefined') {
    lenis.on('scroll', ScrollTrigger.update);
    gsap.ticker.add((time) => {
      lenis.raf(time * 1000);
    });
    gsap.ticker.lagSmoothing(0);
  }

  // 2. Custom Fluid Cursor
  const cursor = document.getElementById('fluid-cursor');
  const follower = document.getElementById('fluid-cursor-follower');
  if (cursor && follower) {
    gsap.set(cursor, {xPercent: -50, yPercent: -50});
    gsap.set(follower, {xPercent: -50, yPercent: -50});
    
    const xTo = gsap.quickTo(cursor, "x", {duration: 0.1, ease: "power3"});
    const yTo = gsap.quickTo(cursor, "y", {duration: 0.1, ease: "power3"});
    
    const fXTo = gsap.quickTo(follower, "x", {duration: 0.4, ease: "power3.out"});
    const fYTo = gsap.quickTo(follower, "y", {duration: 0.4, ease: "power3.out"});

    window.addEventListener("mousemove", e => {
      xTo(e.clientX);
      yTo(e.clientY);
      fXTo(e.clientX);
      fYTo(e.clientY);
    });

    document.querySelectorAll('a, button, input, .showcase-card, .drop-zone').forEach(el => {
      el.addEventListener('mouseenter', () => follower.classList.add('hovering'));
      el.addEventListener('mouseleave', () => follower.classList.remove('hovering'));
    });
  }

  // 4. Magnetic Buttons
  const magneticEls = document.querySelectorAll('[data-magnetic]');
  magneticEls.forEach((btn) => {
    btn.addEventListener('mousemove', (e) => {
      const rect = btn.getBoundingClientRect();
      const x = e.clientX - rect.left - rect.width / 2;
      const y = e.clientY - rect.top - rect.height / 2;

      gsap.to(btn, {
        x: x * 0.4,
        y: y * 0.4,
        duration: 0.4,
        ease: 'power2.out',
      });
    });

    btn.addEventListener('mouseleave', () => {
      gsap.to(btn, { x: 0, y: 0, duration: 1.4, ease: 'power3.out' });
    });
  });

  // 5. 3D Globe Showcase
  const ring = document.getElementById('showcase-ring');
  const cards = document.querySelectorAll('.showcase-card');
  if (ring && cards.length > 0) {
    const numCards = cards.length;
    const radius = 600; // Radius of the 3D circle

    // Distribute cards in a circle
    cards.forEach((card, i) => {
      const angle = (i * 360) / numCards;
      gsap.set(card, {
        rotationY: angle,
        z: radius,
        transformOrigin: "50% 50% " + -radius + "px"
      });
      
      // Hover pops card forward aggressively
      card.addEventListener('mouseenter', () => {
        gsap.to(card, { z: radius + 150, scale: 1.05, duration: 0.6, ease: 'power3.out' });
      });
      card.addEventListener('mouseleave', () => {
        gsap.to(card, { z: radius, scale: 1, duration: 0.6, ease: 'power3.out' });
      });
    });

    // Spin the ring based on scroll inside .showcase-section
    gsap.to(ring, {
      rotationY: -360,
      ease: "none",
      scrollTrigger: {
        trigger: ".showcase-section",
        start: "top top",
        end: "bottom bottom",
        scrub: 0.5
      }
    });
  }

  // 6. Slot Machine Hero Animation — glyph-tight slots, replays on hero re-entry
  const heroTitle = document.getElementById('hero-title');
  if (heroTitle) {
    const text = heroTitle.getAttribute('data-slot-text') || "MEDIA STRIP";
    const seoLabel = heroTitle.querySelector('.sr-only'); // permanent crawlable/a11y heading text
    heroTitle.innerHTML = ''; // clear scramble slots only
    if (seoLabel) heroTitle.appendChild(seoLabel); // keep the real H1 text in the DOM

    const words = text.split(' ');
    const columns = [];
    const charsList = "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789@#$%&*".split('');

    words.forEach(word => {
      const wordDiv = document.createElement('div');
      wordDiv.className = 'hero-word';

      word.split('').forEach(char => {
        const slot = document.createElement('span');
        slot.className = 'hero-char-slot';

        const column = document.createElement('div');
        column.className = 'hero-char-column';

        const numRandomChars = Math.floor(Math.random() * 8) + 8; // 8–15 scramble frames
        for (let i = 0; i < numRandomChars; i++) {
          const c = document.createElement('span');
          c.className = 'hero-char';
          c.innerText = charsList[Math.floor(Math.random() * charsList.length)];
          column.appendChild(c);
        }
        const finalCharDiv = document.createElement('span');
        finalCharDiv.className = 'hero-char';
        finalCharDiv.innerText = char;
        column.appendChild(finalCharDiv);

        slot.appendChild(column);
        wordDiv.appendChild(slot);
        columns.push({ el: column, slot, finalCharDiv, totalItems: numRandomChars + 1 });
      });

      heroTitle.appendChild(wordDiv);
    });

    // Pin each slot to its FINAL glyph's width — the scramble chars (M, W, @) are
    // wider and were inflating every slot, which is what spread the wordmark.
    // Measure after fonts load so Clash Display metrics are correct.
    const tightenSlots = () => {
      columns.forEach(c => {
        c.finalCharDiv.style.width = 'auto';        // unconstrain to read the glyph's true width
        c.finalCharDiv.style.letterSpacing = 'normal'; // neutralize inherited tracking on the measure
        const w = c.finalCharDiv.getBoundingClientRect().width;
        c.finalCharDiv.style.width = '';            // back to 100% of the (about-to-be-pinned) slot
        c.finalCharDiv.style.letterSpacing = '';
        if (w) c.slot.style.width = Math.ceil(w) + 'px';
      });
    };
    if (document.fonts && document.fonts.ready) document.fonts.ready.then(tightenSlots);
    else tightenSlots();
    let resizeRaf;
    window.addEventListener('resize', () => {
      cancelAnimationFrame(resizeRaf);
      columns.forEach(c => { c.slot.style.width = ''; });
      resizeRaf = requestAnimationFrame(tightenSlots);
    });

    // Spin scramble → final. Reused on every hero entry.
    const playSlots = (delay = 0) => {
      columns.forEach((c, i) => {
        const target = -100 * (c.totalItems - 1) / c.totalItems;
        gsap.fromTo(c.el,
          { yPercent: 0 },
          { yPercent: target, duration: 1.5 + Math.random() * 0.7, ease: "power4.inOut", delay: delay + i * 0.04 }
        );
      });
    };

    playSlots(3.5); // initial cinematic play once the loader clears
    if (window.ScrollTrigger) {
      // re-decode every time the hero scrolls back into view. onEnterBack fires
      // when scrolling UP re-crosses the end (hero bottom re-entering past the
      // viewport top) — the start 'top top' is at scroll 0, so a 'top X%' start
      // would be unreachable and never fire.
      ScrollTrigger.create({
        trigger: '#hero', start: 'top top', end: 'bottom top',
        onEnterBack: () => playSlots(0),
      });
    }
  }

  gsap.to('.hero-eyebrow, .hero-tagline .word, .hero-terminal, .hero-actions, .hero-scroll-hint', {
    y: 0,
    opacity: 1,
    scale: 1,
    stagger: 0.15,
    duration: 1.2,
    ease: 'power3.out',
    delay: 4.8
  });

  
}

