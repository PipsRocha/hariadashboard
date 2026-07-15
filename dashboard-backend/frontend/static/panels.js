(function () {
const { useState, useEffect, useRef, useCallback } = React;
const API = window.HARIA_API ?? 'http://localhost:8000';
const _API = API;

const GREYS = ['#0a0a0a','#4a4a4a','#888','#bbb','#282828','#686868','#aaa'];
const ANN_COLORS = ['#0a0a0a','#4a4a4a','#888','#1a1a1a','#666','#aaa'];

function annColor(name, allNames) {
  const idx = [...new Set(allNames)].indexOf(name);
  return ANN_COLORS[Math.max(0, idx) % ANN_COLORS.length];
}

/* ── Chart drawing ─────────────────────────────────────────────────── */

function drawChart(canvas, entries, currentTime) {
  if (!canvas || !entries || entries.length < 2) return [];
  const ctx = canvas.getContext('2d');
  const W = canvas.width, H = canvas.height;
  ctx.clearRect(0, 0, W, H);

  const SKIP = new Set(['t', '_raw', 'type', 'frame', '__names']);
  const keys = [...new Set(entries.flatMap(e => Object.keys(e).filter(k => !SKIP.has(k))))];
  if (!keys.length) return [];

  const allVals = entries.flatMap(e =>
    keys.flatMap(k => { const v = e[k]; return Array.isArray(v) ? v : (typeof v === 'number' ? [v] : []); })
  ).filter(Number.isFinite);
  if (!allVals.length) return [];

  const min = Math.min(...allVals), max = Math.max(...allVals);
  const range = Math.max(1e-6, max - min);
  const t0 = entries[0].t, t1 = entries[entries.length - 1].t;
  const tRange = Math.max(1e-6, t1 - t0);

  ctx.strokeStyle = '#e8e8e8'; ctx.lineWidth = 1;
  for (let i = 0; i <= 4; i++) {
    const y = H - 4 - (i / 4) * (H - 8);
    ctx.beginPath(); ctx.moveTo(0, y); ctx.lineTo(W, y); ctx.stroke();
  }
  ctx.fillStyle = '#bbb'; ctx.font = '8px DM Mono, monospace';
  for (let i = 0; i <= 4; i++) {
    const v = min + (i / 4) * range;
    const y = H - 4 - (i / 4) * (H - 8);
    ctx.fillText(v.toFixed(2), 2, y - 2);
  }

  let ci = 0;
  const legend = [];
  for (const key of keys) {
    const sample = entries.find(e => e[key] !== undefined)?.[key];
    const n = Array.isArray(sample) ? sample.length : 1;
    for (let i = 0; i < n; i++) {
      const color = GREYS[ci % GREYS.length];
      ctx.strokeStyle = color; ctx.lineWidth = ci === 0 ? 1.5 : 1;
      ctx.beginPath();
      let started = false;
      for (const e of entries) {
        const raw = e[key];
        const v = Array.isArray(raw) ? raw[i] : (i === 0 ? raw : undefined);
        if (!Number.isFinite(v)) continue;
        const x = ((e.t - t0) / tRange) * W;
        const y = H - 4 - ((v - min) / range) * (H - 8);
        started ? ctx.lineTo(x, y) : (ctx.moveTo(x, y), started = true);
      }
      ctx.stroke();
      legend.push({ key: n > 1 ? `${key}[${i}]` : key, color });
      ci++;
    }
  }

  if (currentTime !== null && currentTime >= t0 && currentTime <= t1) {
    const cx = ((currentTime - t0) / tRange) * W;
    ctx.strokeStyle = '#0a0a0a'; ctx.lineWidth = 1.5; ctx.setLineDash([4, 3]);
    ctx.beginPath(); ctx.moveTo(cx, 0); ctx.lineTo(cx, H); ctx.stroke();
    ctx.setLineDash([]);
  }
  return legend;
}

/* ── Table flattening ──────────────────────────────────────────────── */

// Flatten a decoded message into [dottedKey, displayValue] rows so nested
// structs render as a real field/value table instead of JSON blobs.
function flattenForTable(obj, prefix = '', out = [], depth = 0) {
  if (depth > 6 || out.length > 300) return out;
  const fmtNum = v => (Number.isInteger(v) ? String(v) : v.toFixed(4));

  if (Array.isArray(obj)) {
    // Numeric arrays stay on one row; arrays of objects expand by index.
    if (obj.every(v => typeof v === 'number')) {
      out.push([prefix || 'value', obj.map(fmtNum).join(', ')]);
    } else {
      obj.forEach((v, i) => flattenForTable(v, `${prefix}[${i}]`, out, depth + 1));
    }
  } else if (obj && typeof obj === 'object') {
    for (const [k, v] of Object.entries(obj)) {
      if (k.startsWith('_')) continue;          // hide _raw internals
      flattenForTable(v, prefix ? `${prefix}.${k}` : k, out, depth + 1);
    }
  } else {
    out.push([prefix || 'value', typeof obj === 'number' ? fmtNum(obj) : String(obj)]);
  }
  return out;
}

/* ── 2D trajectory plot ────────────────────────────────────────────── */

// Find an x/y series in windowed entries: paired dotted keys
// (<prefix>.x / <prefix>.y, e.g. pose.position.x) or a `position` array.
function extractXY(entries) {
  if (!entries || !entries.length) return null;
  const keys = new Set();
  entries.forEach(e => Object.keys(e).forEach(k => keys.add(k)));
  for (const k of keys) {
    if (k.endsWith('.x')) {
      const base = k.slice(0, -2);
      if (keys.has(base + '.y')) {
        const pts = entries
          .filter(e => Number.isFinite(e[base + '.x']) && Number.isFinite(e[base + '.y']))
          .map(e => [e[base + '.x'], e[base + '.y'], e.t]);
        if (pts.length >= 2) return { label: base, pts };
      }
    }
  }
  // `position` array [x, y, ...]
  const pts = entries
    .filter(e => Array.isArray(e.position) && e.position.length >= 2)
    .map(e => [e.position[0], e.position[1], e.t]);
  if (pts.length >= 2) return { label: 'position[x,y]', pts };
  return null;
}

function drawPlot2D(canvas, entries, currentTime) {
  if (!canvas) return null;
  const ctx = canvas.getContext('2d');
  const W = canvas.width, H = canvas.height;
  ctx.clearRect(0, 0, W, H);
  const series = extractXY(entries);
  if (!series) return null;

  const xs = series.pts.map(p => p[0]), ys = series.pts.map(p => p[1]);
  let minX = Math.min(...xs), maxX = Math.max(...xs);
  let minY = Math.min(...ys), maxY = Math.max(...ys);
  // Equal aspect: pad the smaller span so 1 unit x == 1 unit y
  const pad = 12;
  const spanX = Math.max(1e-6, maxX - minX), spanY = Math.max(1e-6, maxY - minY);
  const span = Math.max(spanX, spanY);
  const cx = (minX + maxX) / 2, cy = (minY + maxY) / 2;
  minX = cx - span / 2; maxX = cx + span / 2;
  minY = cy - span / 2; maxY = cy + span / 2;
  const sx = v => pad + ((v - minX) / (maxX - minX)) * (W - 2 * pad);
  const sy = v => H - pad - ((v - minY) / (maxY - minY)) * (H - 2 * pad);

  // grid
  ctx.strokeStyle = '#eee'; ctx.lineWidth = 1;
  for (let i = 0; i <= 4; i++) {
    const gx = pad + (i / 4) * (W - 2 * pad), gy = pad + (i / 4) * (H - 2 * pad);
    ctx.beginPath(); ctx.moveTo(gx, pad); ctx.lineTo(gx, H - pad); ctx.stroke();
    ctx.beginPath(); ctx.moveTo(pad, gy); ctx.lineTo(W - pad, gy); ctx.stroke();
  }

  // trajectory
  ctx.strokeStyle = '#0a0a0a'; ctx.lineWidth = 1.5;
  ctx.beginPath();
  series.pts.forEach((p, i) => { const x = sx(p[0]), y = sy(p[1]); i ? ctx.lineTo(x, y) : ctx.moveTo(x, y); });
  ctx.stroke();

  // current-position marker (nearest point ≤ currentTime)
  if (currentTime != null) {
    let best = null;
    for (const p of series.pts) { if (p[2] <= currentTime) best = p; }
    best = best || series.pts[series.pts.length - 1];
    ctx.fillStyle = '#e8554e';
    ctx.beginPath(); ctx.arc(sx(best[0]), sy(best[1]), 4, 0, Math.PI * 2); ctx.fill();
  }
  return series.label;
}

/* ── Panel ─────────────────────────────────────────────────────────── */

const PANEL_TYPES = [
  { id: 'image',    label: 'Image' },
  { id: 'video',    label: 'Video' },
  { id: 'chart',    label: 'Line Chart' },
  { id: 'plot2d',   label: '2D Plot' },
  { id: 'table',    label: 'Table' },
  { id: 'json',     label: 'JSON Inspector' },
  { id: 'audio',    label: 'Audio' },
  { id: '3d',       label: '3D / TF' },
];

// Types that fetch their own data via a dedicated sub-component — the generic
// Panel data/image poll is skipped for these.
const SELF_FETCH_TYPES = new Set(['video', 'audio', '3d']);

function bisectRight(arr, x) {
  let lo = 0, hi = arr.length;
  while (lo < hi) { const m = (lo + hi) >> 1; if (arr[m] <= x) lo = m + 1; else hi = m; }
  return lo;
}

/* ── Video panel — smooth frame playback ───────────────────────────── */

function VideoBody({ slug, currentTime }) {
  const [frames, setFrames] = useState(null);   // sorted ts array
  const [src, setSrc]       = useState('');
  const [ok, setOk]         = useState(false);
  const preload = useRef({});

  useEffect(() => {
    let alive = true;
    fetch(`${_API}/topics/frames/${slug}`)
      .then(r => r.ok ? r.json() : null)
      .then(d => { if (alive && d && Array.isArray(d.frames)) setFrames(d.frames); })
      .catch(() => {});
    return () => { alive = false; };
  }, [slug]);

  useEffect(() => {
    if (!frames || !frames.length || currentTime == null) return;
    const i = Math.max(0, bisectRight(frames, currentTime) - 1);
    const ts = frames[i];
    setSrc(`${_API}/topics/image/${slug}?frame=${ts.toFixed(3)}`);
    for (let j = i + 1; j <= i + 8 && j < frames.length; j++) {
      const k = frames[j].toFixed(3);
      if (!preload.current[k]) {
        const im = new Image();
        im.src = `${_API}/topics/image/${slug}?frame=${k}`;
        preload.current[k] = im;
      }
    }
  }, [frames, currentTime, slug]);

  if (frames && frames.length === 0)
    return <div className="panel-wait">No frames for this topic</div>;
  return (
    <>
      {src && <img className="panel-image" src={src} alt="" onLoad={() => setOk(true)} onError={() => setOk(false)} />}
      {!ok && <div className="panel-wait">Loading video…</div>}
    </>
  );
}

/* ── Audio panel — waveform + timeline-synced playback ─────────────── */

function AudioBody({ slug, currentTime }) {
  const audioRef  = useRef();
  const canvasRef = useRef();
  const [state, setState] = useState('loading');   // loading | ready | none
  const [t0, setT0]       = useState(0);
  const peaksRef = useRef(null);

  useEffect(() => {
    fetch(`${_API}/topics/index`)
      .then(r => r.ok ? r.json() : null)
      .then(d => {
        const t = (d?.topics || []).find(x => x.slug === slug);
        if (t && t.t_start) setT0(t.t_start);
      })
      .catch(() => {});
  }, [slug]);

  useEffect(() => {
    let alive = true;
    setState('loading');
    fetch(`${_API}/topics/audio/${slug}`)
      .then(r => { if (!r.ok) throw new Error('no audio'); return r.arrayBuffer(); })
      .then(buf => {
        const AC = window.AudioContext || window.webkitAudioContext;
        if (!AC) { if (alive) setState('ready'); return; }
        return new AC().decodeAudioData(buf.slice(0)).then(audio => {
          if (!alive) return;
          const ch = audio.getChannelData(0);
          const N = 800, block = Math.floor(ch.length / N) || 1;
          const peaks = new Array(N);
          for (let i = 0; i < N; i++) {
            let mn = 1, mx = -1;
            for (let j = 0; j < block; j++) { const v = ch[i * block + j] || 0; if (v < mn) mn = v; if (v > mx) mx = v; }
            peaks[i] = [mn, mx];
          }
          peaksRef.current = peaks;
          setState('ready');
        });
      })
      .catch(() => { if (alive) setState('none'); });
    return () => { alive = false; };
  }, [slug]);

  useEffect(() => {
    const cv = canvasRef.current, peaks = peaksRef.current;
    if (!cv || !peaks) return;
    const p = cv.parentElement;
    cv.width = p.clientWidth; cv.height = 60;
    const ctx = cv.getContext('2d');
    const W = cv.width, H = cv.height, mid = H / 2;
    ctx.clearRect(0, 0, W, H);
    ctx.strokeStyle = '#888'; ctx.lineWidth = 1;
    for (let x = 0; x < W; x++) {
      const [mn, mx] = peaks[Math.floor(x / W * peaks.length)] || [0, 0];
      ctx.beginPath(); ctx.moveTo(x, mid - mx * mid); ctx.lineTo(x, mid - mn * mid); ctx.stroke();
    }
    const dur = audioRef.current?.duration;
    if (dur && currentTime != null) {
      const pos = Math.max(0, Math.min(1, (currentTime - t0) / dur));
      ctx.strokeStyle = '#e8554e'; ctx.lineWidth = 1.5;
      ctx.beginPath(); ctx.moveTo(pos * W, 0); ctx.lineTo(pos * W, H); ctx.stroke();
    }
  }, [state, currentTime, t0]);

  useEffect(() => {
    const a = audioRef.current;
    if (!a || state !== 'ready' || currentTime == null) return;
    const target = Math.max(0, currentTime - t0);
    if (isFinite(a.duration) && target <= a.duration && Math.abs(a.currentTime - target) > 0.3) {
      try { a.currentTime = target; } catch {}
    }
    if (window._hariaPlaying && a.paused) a.play().catch(() => {});
    if (!window._hariaPlaying && !a.paused) a.pause();
  }, [currentTime, t0, state]);

  if (state === 'none') return <div className="panel-wait">No audio for this topic</div>;
  return (
    <div style={{ display:'flex', flexDirection:'column', height:'100%', padding:8, gap:6 }}>
      <canvas ref={canvasRef} style={{ width:'100%', height:60, background:'var(--g6)' }} />
      <audio ref={audioRef} src={`${_API}/topics/audio/${slug}`} controls style={{ width:'100%' }} />
      <div style={{ fontFamily:'var(--mono)', fontSize:9, color:'var(--g3)' }}>
        {state === 'loading' ? 'Decoding waveform…' : 'Follows the timeline · use controls to scrub freely'}
      </div>
    </div>
  );
}

/* ── 3D / TF panel ─────────────────────────────────────────────────── */

function TFBody({ slug, currentTime }) {
  const mountRef = useRef();
  const three    = useRef({});
  const staticTf = useRef([]);
  const [err] = useState(window.THREE ? '' : 'three.js failed to load');

  useEffect(() => {
    fetch(`${_API}/topics/data/tf_static?t=1e12&window=1e12&raw=0`)
      .then(r => r.ok ? r.json() : null)
      .then(d => {
        const tf = {};
        (d?.entries || []).forEach(e => (e.tf || []).forEach(x => { tf[x[1]] = x; }));
        staticTf.current = Object.values(tf);
      })
      .catch(() => {});
  }, []);

  useEffect(() => {
    if (!window.THREE || !mountRef.current) return;
    const THREE = window.THREE;
    const el = mountRef.current;
    const scene = new THREE.Scene();
    scene.background = new THREE.Color(0xfafafa);
    const cam = new THREE.PerspectiveCamera(50, el.clientWidth / (el.clientHeight || 1), 0.01, 1000);
    const renderer = new THREE.WebGLRenderer({ antialias: true });
    renderer.setSize(el.clientWidth, el.clientHeight || 300);
    el.appendChild(renderer.domElement);
    scene.add(new THREE.GridHelper(4, 16, 0xcccccc, 0xeeeeee));
    const group = new THREE.Group(); scene.add(group);

    const orbit = { r: 4, theta: Math.PI / 4, phi: Math.PI / 4 };
    function place() {
      cam.position.set(
        orbit.r * Math.sin(orbit.phi) * Math.cos(orbit.theta),
        orbit.r * Math.cos(orbit.phi),
        orbit.r * Math.sin(orbit.phi) * Math.sin(orbit.theta),
      );
      cam.lookAt(0, 0, 0);
    }
    place();
    let drag = null;
    function down(e) { drag = { x: e.clientX, y: e.clientY }; }
    function move(e) {
      if (!drag) return;
      orbit.theta -= (e.clientX - drag.x) * 0.01;
      orbit.phi = Math.max(0.1, Math.min(Math.PI - 0.1, orbit.phi - (e.clientY - drag.y) * 0.01));
      drag = { x: e.clientX, y: e.clientY }; place();
    }
    function up() { drag = null; }
    function wheel(e) { e.preventDefault(); orbit.r = Math.max(0.3, Math.min(50, orbit.r * (e.deltaY > 0 ? 1.1 : 0.9))); place(); }
    renderer.domElement.addEventListener('mousedown', down);
    window.addEventListener('mousemove', move);
    window.addEventListener('mouseup', up);
    renderer.domElement.addEventListener('wheel', wheel, { passive: false });

    let raf;
    const loop = () => { renderer.render(scene, cam); raf = requestAnimationFrame(loop); };
    loop();

    three.current = { THREE, scene, cam, renderer, group };
    return () => {
      cancelAnimationFrame(raf);
      window.removeEventListener('mousemove', move);
      window.removeEventListener('mouseup', up);
      renderer.domElement.removeEventListener('wheel', wheel);
      try { el.removeChild(renderer.domElement); renderer.dispose(); } catch {}
      three.current = {};
    };
  }, []);

  useEffect(() => {
    const { THREE, group } = three.current;
    if (!THREE || !group || currentTime == null) return;
    fetch(`${_API}/topics/data/${slug}?t=${currentTime}&window=5&raw=0`)
      .then(r => r.ok ? r.json() : null)
      .then(d => {
        const latest = {};
        staticTf.current.forEach(x => { latest[x[1]] = x; });
        (d?.entries || []).forEach(e => {
          if (e.t > currentTime) return;
          (e.tf || []).forEach(x => { latest[x[1]] = x; });
        });
        drawFrames(THREE, group, Object.values(latest));
      })
      .catch(() => {});
  }, [currentTime, slug]);

  if (err) return <div className="panel-wait">{err}</div>;
  return <div ref={mountRef} style={{ width:'100%', height:'100%', minHeight:200 }} />;
}

function drawFrames(THREE, group, transforms) {
  while (group.children.length) group.remove(group.children[0]);
  if (!transforms.length) return;

  const local = {};
  transforms.forEach(([parent, child, x, y, z, qx, qy, qz, qw]) => {
    const m = new THREE.Matrix4().compose(
      new THREE.Vector3(x, y, z),
      new THREE.Quaternion(qx, qy, qz, qw),
      new THREE.Vector3(1, 1, 1),
    );
    local[child] = { parent, m };
  });

  const worldCache = {};
  function world(frame, depth) {
    if (depth > 64) return new THREE.Matrix4();
    if (worldCache[frame]) return worldCache[frame];
    const node = local[frame];
    if (!node) return new THREE.Matrix4();
    const w = world(node.parent, depth + 1).clone().multiply(node.m);
    worldCache[frame] = w;
    return w;
  }

  Object.keys(local).forEach(child => {
    const w = world(child, 0);
    const pos = new THREE.Vector3().setFromMatrixPosition(w);
    const axes = new THREE.AxesHelper(0.15);
    axes.applyMatrix4(w);
    group.add(axes);
    const node = local[child];
    const pw = world(node.parent, 0);
    const pp = new THREE.Vector3().setFromMatrixPosition(pw);
    const geo = new THREE.BufferGeometry().setFromPoints([pp, pos]);
    group.add(new THREE.Line(geo, new THREE.LineBasicMaterial({ color: 0xbbbbbb })));
  });
}

/* ── Panel ─────────────────────────────────────────────────────────── */

function Panel({ panel, topicMeta, onClose, onBringToFront, zIndex, currentTime }) {
  const [pos, setPos]           = useState(panel.pos);
  const [size, setSize]         = useState(panel.size);
  const [dragging, setDragging] = useState(false);
  const [data, setData]         = useState(null);
  const [imgSrc, setImgSrc]     = useState('');
  const [imgOk, setImgOk]       = useState(false);
  const [panelType, setPanelType] = useState(panel.type);
  const [showPicker, setShowPicker] = useState(false);
  const [legend, setLegend]     = useState([]);
  const chartRef  = useRef();
  const lastFetch = useRef(0);
  const abortRef  = useRef(null);

  useEffect(() => {
    if (!panel.slug) return;
    const now = Date.now();
    if (now - lastFetch.current < 200) return;
    lastFetch.current = now;
    if (SELF_FETCH_TYPES.has(panelType)) {
      // video / audio / 3d fetch their own data in dedicated sub-components
    } else if (panelType === 'image') {
      const t = currentTime !== null ? `?t=${currentTime}&ts=${now}` : `?ts=${now}`;
      setImgSrc(`${_API}/topics/image/${panel.slug}${t}`);
    } else {
      // Charts and 2D plots need the numeric fields of the whole window but
      // not the heavy _raw payloads; JSON/table need _raw but only the newest.
      const slim = (panelType === 'chart' || panelType === 'plot2d') ? '&raw=0' : '&limit=1';
      const q = currentTime !== null ? `?t=${currentTime}&window=10${slim}` : '';
      if (abortRef.current) abortRef.current.abort();
      const ctrl = new AbortController();
      abortRef.current = ctrl;
      fetch(`${_API}/topics/data/${panel.slug}${q}`, { signal: ctrl.signal })
        .then(r => r.ok ? r.json() : null)
        .then(d => d && setData(d))
        .catch(() => {});
    }
  }, [currentTime, panel.slug, panelType]);

  useEffect(() => () => { if (abortRef.current) abortRef.current.abort(); }, []);

  useEffect(() => {
    if ((panelType !== 'chart' && panelType !== 'plot2d') || !chartRef.current) return;
    const p = chartRef.current.parentElement;
    chartRef.current.width  = p.clientWidth;
    chartRef.current.height = p.clientHeight;
  }, [size, panelType]);

  useEffect(() => {
    if (!data || !chartRef.current) return;
    if (panelType === 'chart') {
      const lg = drawChart(chartRef.current, data.entries || [], currentTime);
      if (lg) setLegend(lg);
    } else if (panelType === 'plot2d') {
      const label = drawPlot2D(chartRef.current, data.entries || [], currentTime);
      setLegend(label ? [{ key: label, color: '#0a0a0a' }] : []);
    }
  }, [data, panelType, size, currentTime]);

  function onTitleMouseDown(e) {
    if (e.target.classList.contains('pt-close') || e.target.classList.contains('pt-type-btn')) return;
    onBringToFront();
    const { clientX: ox, clientY: oy } = e;
    const { x: px, y: py } = pos;
    setDragging(true);
    function onMove(e2) { setPos({ x: px + e2.clientX - ox, y: py + e2.clientY - oy }); }
    function onUp() { setDragging(false); window.removeEventListener('mousemove', onMove); window.removeEventListener('mouseup', onUp); }
    window.addEventListener('mousemove', onMove);
    window.addEventListener('mouseup', onUp);
  }

  function onResizeDown(e) {
    e.stopPropagation();
    const { clientX: ox, clientY: oy } = e;
    const { w, h } = size;
    function onMove(e2) { setSize({ w: Math.max(200, w + e2.clientX - ox), h: Math.max(140, h + e2.clientY - oy) }); }
    function onUp() { window.removeEventListener('mousemove', onMove); window.removeEventListener('mouseup', onUp); }
    window.addEventListener('mousemove', onMove);
    window.addEventListener('mouseup', onUp);
  }

  function renderBody() {
    if (showPicker) return (
      <div className="type-picker">
        <div className="tp-title">Choose visualization</div>
        <div className="tp-grid">
          {PANEL_TYPES.map(t => (
            <button key={t.id} className="tp-btn"
              onClick={() => { setPanelType(t.id); setShowPicker(false); }}>{t.label}</button>
          ))}
        </div>
      </div>
    );

    if (panelType === 'image') return (
      <>
        {imgSrc && <img className="panel-image" src={imgSrc} alt="" onLoad={() => setImgOk(true)} onError={() => setImgOk(false)} />}
        {!imgOk && <div className="panel-wait">Waiting for image…</div>}
      </>
    );

    if (panelType === 'chart') {
      const entries = data?.entries || [];
      return (
        <>
          <canvas ref={chartRef} className="panel-chart" />
          {entries.length === 0 && <div className="panel-wait">Waiting for data…</div>}
          {legend.length > 0 && (
            <div className="chart-legend">
              {legend.map(l => (
                <div key={l.key} className="cl-item">
                  <span className="cl-swatch" style={{ background: l.color }} />{l.key}
                </div>
              ))}
            </div>
          )}
        </>
      );
    }

    if (panelType === 'table') {
      const raw = data?.latest || data?.entries?.[data.entries.length - 1]?._raw;
      if (!raw) return <div className="panel-wait">Waiting for data…</div>;
      if (raw.name && raw.position !== undefined) return (
        <div className="panel-table-wrap">
          <table className="pt-table">
            <thead><tr><th>Joint</th><th>Position</th><th>Velocity</th><th>Effort</th></tr></thead>
            <tbody>
              {(raw.name || []).map((n, i) => (
                <tr key={i}>
                  <td>{n}</td>
                  <td>{raw.position?.[i]?.toFixed(4) ?? '—'}</td>
                  <td>{raw.velocity?.[i]?.toFixed(4) ?? '—'}</td>
                  <td>{raw.effort?.[i]?.toFixed(4)   ?? '—'}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      );
      const rows = flattenForTable(raw);
      return (
        <div className="panel-table-wrap">
          <table className="pt-table">
            <thead><tr><th>Field</th><th>Value</th></tr></thead>
            <tbody>
              {rows.map(([k, v]) => (
                <tr key={k}><td>{k}</td><td>{v}</td></tr>
              ))}
            </tbody>
          </table>
        </div>
      );
    }

    if (panelType === 'plot2d') {
      const has = extractXY(data?.entries || []);
      return (
        <>
          <canvas ref={chartRef} className="panel-chart" />
          {!has && <div className="panel-wait">No x/y series in this topic</div>}
          {legend.length > 0 && (
            <div className="chart-legend">
              {legend.map(l => (
                <div key={l.key} className="cl-item">
                  <span className="cl-swatch" style={{ background: l.color }} />{l.key}
                </div>
              ))}
            </div>
          )}
        </>
      );
    }

    if (panelType === 'json') {
      const d = data?.latest || data?.entries?.[data.entries.length - 1] || data;
      return <div className="panel-json">{d ? JSON.stringify(d, null, 2) : 'Waiting…'}</div>;
    }

    if (panelType === 'video') return <VideoBody slug={panel.slug} currentTime={currentTime} />;
    if (panelType === 'audio') return <AudioBody slug={panel.slug} currentTime={currentTime} />;
    if (panelType === '3d')    return <TFBody slug={panel.slug} currentTime={currentTime} />;

    return <div className="panel-wait">Unknown type</div>;
  }

  const active = imgOk || (data && (data.entries?.length > 0 || data.latest));

  return (
    <div className={`panel${dragging ? ' dragging' : ''}`}
      style={{ left: pos.x, top: pos.y, width: size.w, height: size.h, zIndex }}
      onMouseDown={onBringToFront}
    >
      <div className="panel-titlebar" onMouseDown={onTitleMouseDown}>
        <div className={`pt-dot${active ? '' : ' idle'}`} />
        <div className="pt-label" title={panel.topic}>{panel.topic}</div>
        <span className="pt-type-btn"
          onClick={e => { e.stopPropagation(); setShowPicker(true); }}
          title="Change type">{panelType}</span>
        <div className="pt-close" onClick={e => { e.stopPropagation(); onClose(); }}>✕</div>
      </div>
      <div className="panel-body">{renderBody()}</div>
      <div className="resize-handle" onMouseDown={onResizeDown} />
    </div>
  );
}

/* ── PanelArea ─────────────────────────────────────────────────────── */

let _nextId = 1;

function PanelArea({ topicIndex, currentTime }) {
  const [panels, setPanels] = useState([]);
  const [zOrder, setZOrder] = useState([]);
  const areaRef = useRef();

  window._hariaAddPanel = useCallback((meta, type) => {
    const id   = _nextId++;
    const area = areaRef.current;
    const W = area ? area.clientWidth  : 700;
    const H = area ? area.clientHeight : 500;
    const w = meta.is_image ? 440 : 380;
    const h = meta.is_image ? 340 : 260;
    const off = (panels.length % 8) * 28;
    setPanels(p => [...p, {
      id, topic: meta.topic, slug: meta.slug, type,
      pos:  { x: Math.min(W - w - 10, 20 + off), y: Math.min(H - h - 10, 20 + off) },
      size: { w, h },
    }]);
    setZOrder(z => [...z, id]);
  }, [panels.length]);

  function remove(id)       { setPanels(p => p.filter(p => p.id !== id)); setZOrder(z => z.filter(z => z !== id)); }
  function front(id)        { setZOrder(z => [...z.filter(x => x !== id), id]); }
  function zIdx(id)         { return (zOrder.indexOf(id) + 1) || 1; }

  window._hariaOpenTopics = new Set(panels.map(p => p.topic));

  return (
    <div className="panel-area" ref={areaRef}>
      {panels.length === 0 && (
        <div className="empty-hint">
          <div className="eh-title">No panels open</div>
          <div className="eh-sub">Click a topic in the sidebar to add a panel</div>
        </div>
      )}
      {panels.map(p => (
        <Panel key={p.id} panel={p}
          topicMeta={topicIndex.find(t => t.slug === p.slug)}
          onClose={() => remove(p.id)}
          onBringToFront={() => front(p.id)}
          zIndex={zIdx(p.id)}
          currentTime={currentTime}
        />
      ))}
    </div>
  );
}
window.HariaPanelArea = PanelArea;

/* ── Annotations Sidebar ───────────────────────────────────────────── */

function AnnotationsSidebar({ annotations, onDelete, onImport, onExport, collapsed, onToggle }) {
  const [importErr, setImportErr] = useState('');
  const [hasSel, setHasSel]       = useState(false);
  const fileRef = useRef();

  // Poll whether a selection exists on the annotation strip
  useEffect(() => {
    const t = setInterval(() => {
      setHasSel(!!(window._hariaPendingSelection));
    }, 150);
    return () => clearInterval(t);
  }, []);

  const allNames = [...new Set(annotations.map(a => a.name))];
  function color(ann) {
    // Category color when present; greyscale-by-name for legacy annotations
    const catColor = ann.category && window.HariaCatColor && window.HariaCatColor(ann.category);
    if (catColor) return catColor;
    const idx = allNames.indexOf(ann.name);
    return ANN_COLORS[Math.max(0, idx) % ANN_COLORS.length];
  }

  function fmtSec(sec) {
    if (!isFinite(sec)) return '—';
    const m  = String(Math.floor(sec / 60)).padStart(2, '0');
    const s  = String(Math.floor(sec % 60)).padStart(2, '0');
    const ms = String(Math.round((sec % 1) * 1000)).padStart(3, '0');
    return `${m}:${s}.${ms}`;
  }

  function handleImport(file) {
    setImportErr('');
    const reader = new FileReader();
    reader.onload = e => {
      try {
        const data = JSON.parse(e.target.result);
        const arr  = Array.isArray(data) ? data : (data.annotations || []);
        if (!arr.length) throw new Error('No annotations found');
        arr.forEach((a, i) => {
          if (typeof a.name !== 'string') throw new Error(`Item ${i}: missing name`);
          if (typeof a.t1   !== 'number') throw new Error(`Item ${i}: missing t1`);
          if (typeof a.t2   !== 'number') throw new Error(`Item ${i}: missing t2`);
        });
        onImport(arr.map(a => ({ ...a, id: a.id || (Date.now() + Math.random()) })));
      } catch (err) { setImportErr(err.message); }
    };
    reader.readAsText(file);
  }

  function handleAddClick() {
    if (window._hariaOpenAnnPopup) window._hariaOpenAnnPopup();
  }

  return (
    <div className={`ann-sidebar${collapsed ? ' collapsed' : ''}`}>
      {/* Header */}
      <div className="ann-sb-head" onClick={onToggle}>
        {!collapsed && <span className="ann-sb-title">Annotations</span>}
        {!collapsed && <span className="ann-sb-count">{annotations.length}</span>}
        <span className="ann-sb-chevron">{collapsed ? '‹' : '›'}</span>
      </div>

      {!collapsed && (
        <>
          {/* + Add annotation button — always visible */}
          <div className="ann-sb-add">
            <button
              className="ann-sb-add-btn"
              onClick={handleAddClick}
              disabled={!hasSel}
              title={hasSel ? 'Name and save the current selection (shortcut: n)' : 'First drag a selection on the annotation timeline below'}
            >
              <span className="plus">+</span>
              {hasSel ? 'Add annotation  ·  n' : 'Select a range first'}
            </button>
          </div>

          {/* Import / Export */}
          <div className="ann-sb-io">
            <button className="ann-sb-io-btn" onClick={() => fileRef.current.click()}>↑ Import</button>
            <button className="ann-sb-io-btn" onClick={onExport}>↓ Export</button>
            <input ref={fileRef} type="file" accept=".json" style={{ display: 'none' }}
              onChange={e => { if (e.target.files[0]) handleImport(e.target.files[0]); e.target.value = ''; }} />
          </div>

          {importErr && (
            <div style={{ padding: '6px 12px', fontFamily: 'var(--mono)', fontSize: 9, color: 'var(--danger)', borderBottom: '1px solid var(--g5)' }}>
              {importErr}
            </div>
          )}

          {/* List */}
          <div className="ann-sb-list">
            {annotations.length === 0 ? (
              <div className="ann-sb-empty">
                No annotations yet.<br />
                Drag on the annotation<br />
                timeline to select a range,<br />
                then click + above.
              </div>
            ) : (
              annotations.map(ann => (
                <div key={ann.id} className="ann-item" style={{ cursor:'pointer' }}
                  title="Click to jump to this annotation"
                  onClick={() => { if (window._hariaJumpTo) window._hariaJumpTo(ann.t1); }}>
                  <div className="ann-item-swatch" style={{ background: color(ann) }} />
                  <div className="ann-item-body">
                    <div className="ann-item-name" title={ann.name}>{ann.name}</div>
                    {ann.category && (
                      <div style={{ fontFamily:'var(--mono)', fontSize:8, color: color(ann), letterSpacing:'0.1em', textTransform:'uppercase' }}>
                        {window.HariaCatLabel ? window.HariaCatLabel(ann.category) : ann.category}
                      </div>
                    )}
                    <div className="ann-item-times">{fmtSec(ann.t1)} → {fmtSec(ann.t2)}</div>
                  </div>
                  <span className="ann-item-del" title="Delete"
                    onClick={e => {
                      e.stopPropagation();
                      if (confirm(`Delete annotation "${ann.name}"?`)) onDelete(ann.id);
                    }}>✕</span>
                </div>
              ))
            )}
          </div>
        </>
      )}
    </div>
  );
}
window.HariaAnnotationsSidebar = AnnotationsSidebar;

})();
