// ─── State ─────────────────────────────────────────────────────────────────
let selectedPlatform = 'tiktok';
let selectedFile = null;
let catalogBatchId = 0;
let catalogVideoObserver = null;
const activeEventSources = { dl: null, wm: null };

// ─── Smooth Scroll ─────────────────────────────────────────────────────────
function scrollToSection(id, event) {
  if (event) event.preventDefault();
  const el = document.getElementById(id);
  if (!el) return;
  el.scrollIntoView({ behavior: 'smooth', block: 'start' });
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

// ─── Sound Toggle ──────────────────────────────────────────────────────────
function toggleSound() {
  const video = document.getElementById('hero-video');
  video.muted = !video.muted;
  document.getElementById('icon-muted').style.display   = video.muted ? '' : 'none';
  document.getElementById('icon-unmuted').style.display = video.muted ? 'none' : '';
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
function closeEventSource(prefix) {
  activeEventSources[prefix]?.close();
  activeEventSources[prefix] = null;
}

function streamProgress(jobId, prefix) {
  closeEventSource(prefix);

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

function addArtifactsToCatalog(prefix, artifacts) {
  if (!Array.isArray(artifacts) || !artifacts.length) return;

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
  meta.textContent = `${artifacts.length} ${artifacts.length === 1 ? 'file' : 'files'} · ${formatCatalogTimestamp()}`;

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
      };
      img.src = ev.target.result;
    };
    reader.readAsDataURL(file);
  } else {
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
  const loader = document.getElementById('site-loader');
  if (!loader) return;

  function dismiss() {
    loader.classList.add('is-leaving');
    setTimeout(() => {
      loader.hidden = true;
      document.body.classList.remove('is-preloading');
      document.body.classList.add('app-ready');
    }, 900);
  }

  // Auto-dismiss after the animation plays (~5 s)
  const timer = setTimeout(dismiss, 5000);

  // Click anywhere on the loader after 1.5 s to skip
  setTimeout(() => {
    loader.addEventListener('click', () => { clearTimeout(timer); dismiss(); }, { once: true });
  }, 1500);
}

// ─── Init ─────────────────────────────────────────────────────────────────────
document.addEventListener('DOMContentLoaded', () => {
  initLoader();

  if (window.innerWidth <= 768) {
    document.getElementById('hero-video')?.pause();
  }
  document.getElementById('url-input')?.addEventListener('keydown', e => {
    if (e.key === 'Enter') startDownload();
  });

  initScrollNav();
  initReveal();
  initCatalogVideoObserver();
  initCoordsTicker();
  initStatsCounters();
  initFeatureCards();
});
