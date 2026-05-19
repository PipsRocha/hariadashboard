(function () {
const { useState, useEffect, useRef, useCallback } = React;

/* ─────────────────────────────────────────────
   Helpers
───────────────────────────────────────────── */

function fmtTime(sec) {
  if (!isFinite(sec) || sec == null) return { m: '00', s: '00', ms: '0' };
  return {
    m:  String(Math.floor(sec / 60)).padStart(2, '0'),
    s:  String(Math.floor(sec % 60)).padStart(2, '0'),
    ms: String(Math.floor((sec % 1) * 10)),
  };
}
function fmtSec(sec) {
  if (!isFinite(sec)) return '—';
  const m  = String(Math.floor(sec / 60)).padStart(2, '0');
  const s  = String(Math.floor(sec % 60)).padStart(2, '0');
  const ms = String(Math.round((sec % 1) * 1000)).padStart(3, '0');
  return `${m}:${s}.${ms}`;
}
window.HariaFmtTime = fmtTime;
window.HariaFmtSec  = fmtSec;

const ANN_COLORS = ['#0a0a0a','#4a4a4a','#888','#1a1a1a','#666','#aaa'];
function annColor(name, allNames) {
  const idx = [...new Set(allNames)].indexOf(name);
  return ANN_COLORS[Math.max(0, idx) % ANN_COLORS.length];
}

/* ─────────────────────────────────────────────
   useViewport — independent pan+zoom per strip
   viewport = { start: 0..1, end: 0..1 }
   representing visible fraction of total duration
───────────────────────────────────────────── */
function useViewport() {
  const [vp, setVp] = useState({ start: 0, end: 1 });

  const pan = useCallback((deltaFrac) => {
    setVp(v => {
      const w = v.end - v.start;
      const s = Math.max(0, Math.min(1 - w, v.start + deltaFrac));
      return { start: s, end: s + w };
    });
  }, []);

  const zoom = useCallback((factor, centerFrac) => {
    setVp(v => {
      const w    = v.end - v.start;
      const newW = Math.max(0.005, Math.min(1, w * factor));
      const c    = v.start + centerFrac * w;
      const s    = Math.max(0, Math.min(1 - newW, c - centerFrac * newW));
      return { start: s, end: s + newW };
    });
  }, []);

  return { vp, pan, zoom };
}

/* ─────────────────────────────────────────────
   useStripInteraction — shared mouse/wheel logic
   for any strip element
───────────────────────────────────────────── */
function useStripInteraction(ref, { vp, pan, zoom, onDragStart, onDragMove, onDragEnd }) {
  const duration = vp.end - vp.start;

  function clientToFrac(clientX) {
    const rect = ref.current.getBoundingClientRect();
    return vp.start + ((clientX - rect.left) / rect.width) * duration;
  }

  function handleMouseDown(e) {
    if (e.button === 1 || (e.button === 0 && e.altKey)) {
      // Middle-click or alt+drag = pan
      e.preventDefault();
      const startX = e.clientX;
      const w = ref.current.clientWidth;
      function onMove(e2) { pan(-(e2.clientX - startX) / w * duration); }
      function onUp()     { window.removeEventListener('mousemove', onMove); window.removeEventListener('mouseup', onUp); }
      window.addEventListener('mousemove', onMove);
      window.addEventListener('mouseup', onUp);
      return;
    }
    if (e.button === 0 && !e.altKey) {
      const frac = clientToFrac(e.clientX);
      onDragStart && onDragStart(frac, e);
      function onMove(e2) { onDragMove && onDragMove(clientToFrac(e2.clientX), e2); }
      function onUp(e2)   {
        onDragEnd && onDragEnd(clientToFrac(e2.clientX), e2);
        window.removeEventListener('mousemove', onMove);
        window.removeEventListener('mouseup', onUp);
      }
      window.addEventListener('mousemove', onMove);
      window.addEventListener('mouseup', onUp);
    }
  }

  function handleWheel(e) {
    e.preventDefault();
    const rect = ref.current.getBoundingClientRect();
    const cf   = (e.clientX - rect.left) / rect.width;
    // ctrl/meta = zoom, otherwise pan
    if (e.ctrlKey || e.metaKey || Math.abs(e.deltaY) > Math.abs(e.deltaX) * 2) {
      zoom(e.deltaY > 0 ? 1.18 : 0.85, cf);
    } else {
      pan((e.deltaX / rect.width) * duration);
    }
  }

  return { handleMouseDown, handleWheel, clientToFrac };
}

/* ─────────────────────────────────────────────
   Name popup
───────────────────────────────────────────── */
function AnnNamePopup({ existingNames, style, onConfirm, onCancel }) {
  const [value, setValue]       = useState('');
  const [sugg, setSugg]         = useState([]);
  const [showSugg, setShowSugg] = useState(false);
  const inputRef = useRef();

  useEffect(() => { setTimeout(() => inputRef.current?.focus(), 20); }, []);

  function updateSugg(v) {
    const q = v.toLowerCase();
    setSugg(q ? existingNames.filter(n => n.toLowerCase().includes(q) && n !== v) : [...existingNames]);
  }
  function confirm(name) {
    const n = (name !== undefined ? name : value).trim();
    if (n) onConfirm(n);
  }

  return (
    <div className="ann-name-popup" style={style}>
      <div className="ann-name-popup-head">Annotation name</div>
      <div style={{ position: 'relative' }}>
        <input ref={inputRef} className="ann-name-input"
          value={value} placeholder="e.g. fault_onset, recovery…"
          onChange={e => { setValue(e.target.value); updateSugg(e.target.value); }}
          onFocus={() => { updateSugg(value); setShowSugg(true); }}
          onBlur={() => setTimeout(() => setShowSugg(false), 150)}
          onKeyDown={e => {
            if (e.key === 'Enter')  { e.preventDefault(); confirm(); }
            if (e.key === 'Escape') onCancel();
          }}
        />
        {showSugg && sugg.length > 0 && (
          <div className="ann-suggestions">
            {sugg.map(s => (
              <div key={s} className="ann-sugg-item"
                onMouseDown={() => { setValue(s); setShowSugg(false); setTimeout(() => confirm(s), 0); }}>
                {s}
              </div>
            ))}
          </div>
        )}
      </div>
      <div className="ann-name-actions">
        <button className="ann-name-cancel" onClick={onCancel}>Cancel</button>
        <button className="ann-name-confirm" onClick={() => confirm()}>Save</button>
      </div>
    </div>
  );
}

/* ─────────────────────────────────────────────
   ScrubberStrip — independent viewport
───────────────────────────────────────────── */
function ScrubberStrip({ tStart, tEnd, currentTime, annotations, onSeek }) {
  const ref = useRef();
  const { vp, pan, zoom } = useViewport();
  const duration   = Math.max(1, (tEnd || 0) - (tStart || 0));
  const viewWidth  = vp.end - vp.start;
  const allAnnNames = [...new Set((annotations || []).map(a => a.name))];

  const { handleMouseDown, handleWheel } = useStripInteraction(ref, {
    vp, pan, zoom,
    onDragStart: (frac) => onSeek((tStart || 0) + frac * duration),
    onDragMove:  (frac) => onSeek((tStart || 0) + frac * duration),
  });

  const elapsed     = currentTime != null && tStart ? Math.max(0, currentTime - tStart) : 0;
  const progressGF  = Math.min(1, elapsed / duration);
  const progressVis = (progressGF >= vp.start && progressGF <= vp.end);
  const progressPct = progressVis ? ((progressGF - vp.start) / viewWidth) * 100 : -999;

  const TICK_N = 8;
  const ticks  = Array.from({ length: TICK_N + 1 }, (_, i) => {
    const gf = vp.start + (i / TICK_N) * viewWidth;
    const { m, s } = fmtTime(gf * duration);
    return `${m}:${s}`;
  });

  return (
    <div className="tl-strip scrubber">
      <div className="tl-strip-label">
        <div className="tl-strip-label-text">Playhead</div>
        <div className="tl-strip-label-sub">drag · scroll</div>
      </div>
      <div className="tl-viewport" ref={ref}
        onMouseDown={handleMouseDown}
        onWheel={handleWheel}
        style={{ cursor: 'ew-resize' }}
      >
        {/* base track */}
        <div style={{ position:'absolute', left:0, right:0, top:'50%', transform:'translateY(-50%)', height:3, background:'var(--g5)' }}>
          <div style={{ height:'100%', width: progressVis ? `${progressPct}%` : '0%', background:'var(--black)' }} />
        </div>

        {/* annotation shading on scrubber */}
        {(annotations || []).map(ann => {
          const gf1 = (ann.t1 - (tStart || 0)) / duration;
          const gf2 = (ann.t2 - (tStart || 0)) / duration;
          if (gf2 < vp.start || gf1 > vp.end) return null;
          const x1pct = Math.max(0, (gf1 - vp.start) / viewWidth * 100);
          const x2pct = Math.min(100, (gf2 - vp.start) / viewWidth * 100);
          return (
            <div key={ann.id} style={{
              position:'absolute', top:8, bottom:8,
              left:`${x1pct}%`, width:`${x2pct - x1pct}%`,
              background: annColor(ann.name, allAnnNames), opacity:0.18,
            }} />
          );
        })}

        {/* playhead */}
        {progressVis && (
          <div className="tl-playhead" style={{ left:`${progressPct}%` }} />
        )}

        {/* ticks */}
        <div className="tl-ticks">
          {ticks.map((t, i) => (
            <span key={i} style={{ position:'absolute', left:`${(i/TICK_N)*100}%`, transform:'translateX(-50%)' }}>{t}</span>
          ))}
        </div>
      </div>
    </div>
  );
}

/* ─────────────────────────────────────────────
   AnnotationStrip — independent viewport
   exposes pendingSelection to parent via callback
───────────────────────────────────────────── */
function AnnotationStrip({ tStart, tEnd, annotations, onSelectionChange, pendingSelection, showPopup, onConfirm, onCancelPopup }) {
  const ref = useRef();
  const { vp, pan, zoom } = useViewport();
  const [hovered, setHovered] = useState(null);
  const [dragFrac, setDragFrac] = useState(null); // starting frac of current drag
  const duration  = Math.max(1, (tEnd || 0) - (tStart || 0));
  const viewWidth = vp.end - vp.start;
  const allAnnNames = [...new Set((annotations || []).map(a => a.name))];

  const { handleMouseDown, handleWheel } = useStripInteraction(ref, {
    vp, pan, zoom,
    onDragStart: (frac) => {
      setDragFrac(frac);
      onSelectionChange({ f1: frac, f2: frac });
    },
    onDragMove: (frac) => {
      if (dragFrac === null) return;
      onSelectionChange({ f1: dragFrac, f2: frac });
    },
    onDragEnd: (frac) => {
      if (dragFrac === null) return;
      const f1 = dragFrac, f2 = frac;
      if (Math.abs(f2 - f1) < 0.004) {
        onSelectionChange(null);
      } else {
        onSelectionChange({ f1: Math.min(f1,f2), f2: Math.max(f1,f2) });
      }
      setDragFrac(null);
    },
  });

  // ghost geometry
  let ghostLeft = null, ghostWidth = null;
  if (pendingSelection) {
    const { f1, f2 } = pendingSelection;
    const vf1 = Math.max(vp.start, f1);
    const vf2 = Math.min(vp.end,   f2);
    if (vf2 > vf1) {
      ghostLeft  = (vf1 - vp.start) / viewWidth * 100;
      ghostWidth = (vf2 - vf1)      / viewWidth * 100;
    }
  }

  // popup anchor: mid of selection
  const popupLeft = (() => {
    if (!pendingSelection) return '40%';
    const mid = (pendingSelection.f1 + pendingSelection.f2) / 2;
    const pct = (mid - vp.start) / viewWidth * 100;
    return `clamp(0px, calc(${pct}% - 120px), calc(100% - 244px))`;
  })();

  return (
    <div style={{ position: 'relative' }}>
      <div className="tl-strip annotate">
        <div className="tl-strip-label">
          <div className="tl-strip-label-text">Annotate</div>
          <div className="tl-strip-label-sub">drag · scroll</div>
        </div>
        <div className="tl-viewport" ref={ref}
          onMouseDown={handleMouseDown}
          onWheel={handleWheel}
          style={{ background: 'var(--g6)' }}
        >
          {/* saved blocks */}
          {(annotations || []).map(ann => {
            const gf1 = (ann.t1 - (tStart || 0)) / duration;
            const gf2 = (ann.t2 - (tStart || 0)) / duration;
            if (gf2 < vp.start || gf1 > vp.end) return null;
            const x1pct = Math.max(0, (gf1 - vp.start) / viewWidth * 100);
            const x2pct = Math.min(100, (gf2 - vp.start) / viewWidth * 100);
            const color  = annColor(ann.name, allAnnNames);
            return (
              <div key={ann.id} className="ann-block"
                style={{ left:`${x1pct}%`, width:`${x2pct-x1pct}%`, background:color, opacity: hovered===ann.id ? 0.9 : 0.65 }}
                onMouseEnter={() => setHovered(ann.id)}
                onMouseLeave={() => setHovered(null)}
              >
                <span className="ann-block-label">{ann.name}</span>
                {hovered === ann.id && (
                  <div className="ann-tooltip">
                    <strong>{ann.name}</strong><br/>
                    {fmtSec(ann.t1-(tStart||0))} → {fmtSec(ann.t2-(tStart||0))}<br/>
                    dur: {fmtSec(ann.t2-ann.t1)}
                  </div>
                )}
              </div>
            );
          })}

          {/* ghost */}
          {ghostLeft !== null && (
            <div className="ann-ghost" style={{ left:`${ghostLeft}%`, width:`${ghostWidth}%` }} />
          )}
        </div>
      </div>

      {/* Name popup positioned relative to strip */}
      {showPopup && (
        <AnnNamePopup
          existingNames={allAnnNames}
          style={{ position:'absolute', bottom:'calc(100% + 6px)', left: popupLeft }}
          onConfirm={onConfirm}
          onCancel={onCancelPopup}
        />
      )}
    </div>
  );
}

/* ─────────────────────────────────────────────
   TimelineContainer — composes both strips
───────────────────────────────────────────── */
function TimelineContainer({ mode, tStart, tEnd, topicIndex, onTimeChange, onStop,
                              annotations, onAddAnnotation, annCollapsed, onToggleAnn }) {
  const [currentTime, setCurrent] = useState(null);
  const [pendingSel, setPendingSel] = useState(null);   // {f1,f2} global fracs
  const [showPopup, setShowPopup]   = useState(false);
  const rafRef = useRef();
  const duration = Math.max(1, (tEnd||0) - (tStart||0));

  // Live clock
  useEffect(() => {
    if (mode !== 'record') return;
    function tick() {
      const now = Date.now() / 1000;
      setCurrent(now); onTimeChange(now);
      rafRef.current = requestAnimationFrame(tick);
    }
    rafRef.current = requestAnimationFrame(tick);
    return () => cancelAnimationFrame(rafRef.current);
  }, [mode]);

  useEffect(() => {
    if (mode === 'playback' && tStart && currentTime === null) {
      setCurrent(tStart); onTimeChange(tStart);
    }
  }, [mode, tStart]);

  // Expose pending selection so sidebar + button can trigger popup
  window._hariaPendingSelection = pendingSel;
  window._hariaOpenAnnPopup = () => {
    if (window._hariaPendingSelection) setShowPopup(true);
  };

  function confirmAnnotation(name) {
    if (!pendingSel) return;
    const t1 = (tStart||0) + pendingSel.f1 * duration;
    const t2 = (tStart||0) + pendingSel.f2 * duration;
    onAddAnnotation({ id: Date.now(), name, t1, t2 });
    setPendingSel(null); setShowPopup(false);
  }

  const elapsed    = currentTime != null && tStart ? Math.max(0, currentTime - tStart) : 0;
  const { m, s, ms } = fmtTime(elapsed);

  return (
    <div className="tl-container">
      {/* Header */}
      <div className="tl-header">
        <div className="tl-hcell" style={{ width:80, borderRight:'1px solid var(--black)' }}>
          <div className="tl-label">Time</div>
          <div className="tl-clock">{m}:{s}<span className="tl-ms">.{ms}</span></div>
        </div>
        <div className="tl-hcell">
          <div className="tl-label">Duration</div>
          <div className="tl-val">{duration > 1 ? fmtSec(duration) : '—'}</div>
        </div>
        <div className="tl-hcell">
          <div className="tl-label">Topics</div>
          <div className="tl-val">{topicIndex.filter(t => t.active || mode==='playback').length}</div>
        </div>
        <div className="tl-hcell" style={{ cursor:'pointer' }} onClick={onToggleAnn}>
          <div className="tl-label">Annotations</div>
          <div className="tl-val">{annotations.length} · {annCollapsed ? 'show ›' : 'hide ‹'}</div>
        </div>
        <div className="tl-hspacer" />
        <div style={{ display:'flex', alignItems:'center', fontFamily:'var(--mono)', fontSize:9, color:'var(--g3)', padding:'0 14px', letterSpacing:'0.1em', borderLeft:'1px solid var(--black)' }}>
          scroll=pan · ctrl+scroll=zoom · alt+drag=pan
        </div>
        <button className="tb-stop" style={{ borderLeft:'1px solid var(--black)', padding:'0 18px' }} onClick={onStop}>■ Stop</button>
      </div>

      {/* Scrubber — independent viewport */}
      <ScrubberStrip
        tStart={tStart} tEnd={tEnd}
        currentTime={currentTime}
        annotations={annotations}
        onSeek={t => { setCurrent(t); onTimeChange(t); }}
      />

      {/* Annotation strip — independent viewport */}
      <AnnotationStrip
        tStart={tStart} tEnd={tEnd}
        annotations={annotations}
        pendingSelection={pendingSel}
        onSelectionChange={setPendingSel}
        showPopup={showPopup}
        onConfirm={confirmAnnotation}
        onCancelPopup={() => { setShowPopup(false); setPendingSel(null); }}
      />
    </div>
  );
}

window.HariaTimelineContainer = TimelineContainer;

})();
