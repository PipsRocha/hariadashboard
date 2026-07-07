(function () {
const { useState, useEffect, useMemo } = React;
const API = window.HARIA_API ?? 'http://localhost:8000';

/**
 * HARIA Failure Dashboard — Annotations Explorer
 * Browses annotations.json across all local recordings: group by name,
 * count occurrences per name across files, filter by name / recording /
 * minimum count, and jump into playback at an annotation.
 */

function fmtSec(sec) {
  if (!isFinite(sec)) return '—';
  const m  = String(Math.floor(sec / 60)).padStart(2, '0');
  const s  = String(Math.floor(sec % 60)).padStart(2, '0');
  const ms = String(Math.round((sec % 1) * 1000)).padStart(3, '0');
  return `${m}:${s}.${ms}`;
}

const th = { textAlign:'left', padding:'8px 12px', fontFamily:'var(--mono)', fontSize:9,
             letterSpacing:'0.2em', textTransform:'uppercase', color:'var(--g3)',
             borderBottom:'1px solid var(--black)', position:'sticky', top:0, background:'var(--white)' };
const td = { padding:'8px 12px', fontFamily:'var(--mono)', fontSize:12,
             borderBottom:'1px solid var(--g5)' };

function AnnotationsExplorer({ onBack, onOpenRecording }) {
  const [rows, setRows]         = useState([]);
  const [loading, setLoading]   = useState(true);
  const [err, setErr]           = useState('');
  const [q, setQ]               = useState('');
  const [recFilter, setRec]     = useState('');
  const [minCount, setMinCount] = useState(1);
  const [expanded, setExpanded] = useState(null);   // annotation name

  useEffect(() => {
    fetch(`${API}/recordings/annotations`)
      .then(r => { if (!r.ok) throw new Error(`HTTP ${r.status}`); return r.json(); })
      .then(d => { if (Array.isArray(d)) setRows(d); })
      .catch(e => setErr(String(e)))
      .finally(() => setLoading(false));
  }, []);

  const recordings = useMemo(() => [...new Set(rows.map(r => r.recording))].sort(), [rows]);

  const groups = useMemo(() => {
    const filtered = rows.filter(r =>
      (!q || r.name.toLowerCase().includes(q.toLowerCase())) &&
      (!recFilter || r.recording === recFilter)
    );
    const byName = {};
    for (const r of filtered) (byName[r.name] = byName[r.name] || []).push(r);
    return Object.entries(byName)
      .map(([name, items]) => ({
        name, items,
        count: items.length,
        nRecs: new Set(items.map(i => i.recording)).size,
        totalDur: items.reduce((s, i) => s + (isFinite(i.t2 - i.t1) ? i.t2 - i.t1 : 0), 0),
      }))
      .filter(g => g.count >= (minCount || 1))
      .sort((a, b) => b.count - a.count || a.name.localeCompare(b.name));
  }, [rows, q, recFilter, minCount]);

  const shown = groups.reduce((s, g) => s + g.count, 0);

  function exportFiltered() {
    const flat = groups.flatMap(g => g.items);
    const blob = new Blob([JSON.stringify(flat, null, 2)], { type: 'application/json' });
    const url  = URL.createObjectURL(blob);
    const a    = document.createElement('a');
    a.href = url; a.download = 'haria_annotations_filtered.json'; a.click();
    URL.revokeObjectURL(url);
  }

  function relT(item, t) {
    // Times relative to the recording start read better than epoch seconds
    const t0 = item.recording_start_ns != null ? item.recording_start_ns / 1e9 : null;
    return t0 != null && t >= t0 ? fmtSec(t - t0) : (t != null ? t.toFixed(3) : '—');
  }

  return (
    <div className="setup-screen fade-in" style={{ display:'flex', flexDirection:'column', height:'100vh' }}>
      <div className="setup-header">
        <button className="back-btn" onClick={onBack}>← Back</button>
        <h1>Annotations</h1>
        <div style={{ flex:1 }} />
        <div style={{ fontFamily:'var(--mono)', fontSize:10, color:'var(--g3)', letterSpacing:'0.1em' }}>
          {shown} annotations · {groups.length} names · {recordings.length} recordings
        </div>
      </div>

      {/* Filter bar */}
      <div style={{ display:'flex', gap:0, borderBottom:'2px solid var(--black)', flexShrink:0 }}>
        <input
          placeholder="Filter by name…"
          value={q} onChange={e => setQ(e.target.value)}
          style={{ flex:2, padding:'12px 16px', border:'none', borderRight:'1px solid var(--black)',
                   background:'transparent', fontFamily:'var(--mono)', fontSize:12, outline:'none' }}
        />
        <select
          value={recFilter} onChange={e => setRec(e.target.value)}
          style={{ flex:2, padding:'12px 16px', border:'none', borderRight:'1px solid var(--black)',
                   background:'transparent', fontFamily:'var(--mono)', fontSize:11, outline:'none', cursor:'pointer' }}
        >
          <option value="">All recordings</option>
          {recordings.map(r => <option key={r} value={r}>{r}</option>)}
        </select>
        <label style={{ display:'flex', alignItems:'center', gap:8, padding:'0 16px',
                        borderRight:'1px solid var(--black)', fontFamily:'var(--mono)', fontSize:9,
                        letterSpacing:'0.15em', textTransform:'uppercase', color:'var(--g3)' }}>
          Min count
          <input type="number" min="1" value={minCount}
            onChange={e => setMinCount(parseInt(e.target.value) || 1)}
            style={{ width:52, border:'none', borderBottom:'1px solid var(--g5)', background:'transparent',
                     fontFamily:'var(--mono)', fontSize:12, outline:'none' }} />
        </label>
        <button className="btn" style={{ borderRight:'none' }} onClick={exportFiltered}
          disabled={groups.length === 0}>↓ Export filtered</button>
      </div>

      {/* Results */}
      <div style={{ flex:1, overflowY:'auto' }}>
        {loading && <div style={{ padding:32, fontFamily:'var(--mono)', fontSize:11, color:'var(--g3)' }}>Loading annotations…</div>}
        {err && <div className="err-row">{err}</div>}
        {!loading && !err && groups.length === 0 && (
          <div style={{ padding:32, fontFamily:'var(--mono)', fontSize:11, color:'var(--g3)' }}>
            {rows.length === 0
              ? 'No annotations found in any recording. Annotate a session in Record or Playback first.'
              : 'No annotations match the current filters.'}
          </div>
        )}
        {groups.length > 0 && (
          <table style={{ width:'100%', borderCollapse:'collapse' }}>
            <thead>
              <tr>
                <th style={th}>Name</th>
                <th style={th}>Count</th>
                <th style={th}>Recordings</th>
                <th style={th}>Total duration</th>
                <th style={th}>Avg duration</th>
              </tr>
            </thead>
            <tbody>
              {groups.map(g => (
                <React.Fragment key={g.name}>
                  <tr
                    onClick={() => setExpanded(expanded === g.name ? null : g.name)}
                    style={{ cursor:'pointer', background: expanded === g.name ? 'var(--g6)' : '' }}
                    onMouseEnter={e => e.currentTarget.style.background = 'var(--g6)'}
                    onMouseLeave={e => e.currentTarget.style.background = expanded === g.name ? 'var(--g6)' : ''}
                  >
                    <td style={{ ...td, fontWeight:500 }}>{expanded === g.name ? '▾ ' : '▸ '}{g.name}</td>
                    <td style={td}>{g.count}</td>
                    <td style={td}>{g.nRecs}</td>
                    <td style={td}>{fmtSec(g.totalDur)}</td>
                    <td style={td}>{fmtSec(g.totalDur / g.count)}</td>
                  </tr>
                  {expanded === g.name && g.items.map((item, i) => (
                    <tr key={`${g.name}-${i}`} style={{ background:'var(--g6)' }}>
                      <td style={{ ...td, paddingLeft:32, color:'var(--g2)' }}>{item.recording}</td>
                      <td style={{ ...td, color:'var(--g2)' }} colSpan={2}>
                        {relT(item, item.t1)} → {relT(item, item.t2)}
                      </td>
                      <td style={{ ...td, color:'var(--g2)' }}>
                        {isFinite(item.t2 - item.t1) ? fmtSec(item.t2 - item.t1) : '—'}
                      </td>
                      <td style={td}>
                        <button className="back-btn" style={{ padding:'4px 10px', fontSize:9 }}
                          onClick={() => onOpenRecording(item.recording, item.t1)}>
                          ▶ Open
                        </button>
                      </td>
                    </tr>
                  ))}
                </React.Fragment>
              ))}
            </tbody>
          </table>
        )}
      </div>
    </div>
  );
}

window.HariaAnnotationsExplorer = AnnotationsExplorer;

})();
