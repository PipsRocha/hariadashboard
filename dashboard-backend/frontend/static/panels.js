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

/* ── Panel ─────────────────────────────────────────────────────────── */

const PANEL_TYPES = [
  { id: 'image',    label: 'Image' },
  { id: 'chart',    label: 'Line Chart' },
  { id: 'table',    label: 'Table' },
  { id: 'json',     label: 'JSON Inspector' },
  { id: '3d',       label: '3D / TF' },
  { id: 'audio',    label: 'Audio' },
  { id: 'plot2d',   label: '2D Plot' },
  { id: 'timeline', label: 'Timeline' },
];

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
    if (panelType === 'image') {
      const t = currentTime !== null ? `?t=${currentTime}&ts=${now}` : `?ts=${now}`;
      setImgSrc(`${_API}/topics/image/${panel.slug}${t}`);
    } else {
      // Charts need the numeric fields of the whole window but not the heavy
      // _raw payloads; JSON/table views need _raw but only the newest entry.
      const slim = panelType === 'chart' ? '&raw=0' : '&limit=1';
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
    if (panelType !== 'chart' || !chartRef.current) return;
    const p = chartRef.current.parentElement;
    chartRef.current.width  = p.clientWidth;
    chartRef.current.height = p.clientHeight;
  }, [size, panelType]);

  useEffect(() => {
    if (panelType !== 'chart' || !data) return;
    const entries = data.entries || [];
    const lg = drawChart(chartRef.current, entries, currentTime);
    if (lg) setLegend(lg);
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
      return (
        <div className="panel-table-wrap">
          <table className="pt-table">
            <thead><tr><th>Field</th><th>Value</th></tr></thead>
            <tbody>
              {Object.entries(raw).map(([k, v]) => (
                <tr key={k}><td>{k}</td><td>{typeof v === 'object' ? JSON.stringify(v).slice(0, 100) : String(v)}</td></tr>
              ))}
            </tbody>
          </table>
        </div>
      );
    }

    if (panelType === 'json') {
      const d = data?.latest || data?.entries?.[data.entries.length - 1] || data;
      return <div className="panel-json">{d ? JSON.stringify(d, null, 2) : 'Waiting…'}</div>;
    }

    const labels = { '3d': '3D / TF', 'audio': 'Audio', 'plot2d': '2D Plot', 'timeline': 'Timeline' };
    if (labels[panelType]) return (
      <div className="panel-coming">
        <div className="pc-title">{labels[panelType]}</div>
        <div className="pc-sub">Coming soon</div>
      </div>
    );

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
              title={hasSel ? 'Name and save the current selection' : 'First drag a selection on the annotation timeline below'}
            >
              <span className="plus">+</span>
              {hasSel ? 'Add annotation' : 'Select a range first'}
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
