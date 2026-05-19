(function() {
const { useState, useEffect, useRef } = React;
const _API = window.HARIA_API || 'http://localhost:8000';

const CATEGORIES = [
  { id: 'failure',      label: 'Failure',           color: '#e8554e' },
  { id: 'recovery',     label: 'Recovery',          color: '#3fb27f' },
  { id: 'intervention', label: 'User Intervention', color: '#f5a623' },
  { id: 'anomaly',      label: 'Anomaly',           color: '#9b6dff' },
  { id: 'note',         label: 'Note',              color: '#5b8def' },
];
const COLOR_OF = Object.fromEntries(CATEGORIES.map(c => [c.id, c.color]));

function fmt(t, tStart) {
  if (t == null) return '—';
  const rel = Math.max(0, t - (tStart || 0));
  const m   = String(Math.floor(rel / 60)).padStart(2, '0');
  const s   = String(Math.floor(rel % 60)).padStart(2, '0');
  const ms  = String(Math.floor((rel % 1) * 10));
  return `${m}:${s}.${ms}`;
}

function duration(a) {
  return Math.max(0, (a.t_end || a.t_start) - a.t_start);
}

// ── Left panel ──────────────────────────────────────────────────────────
function AnnotationPanel({
  annotations, tStart, onJump, onEdit, onDelete, onImport, onExport, onClose,
}) {
  const [filter, setFilter] = useState('all');
  const [search, setSearch] = useState('');
  const fileRef = useRef();

  const filtered = (annotations || []).filter(a => {
    if (filter !== 'all' && a.category !== filter) return false;
    if (search) {
      const q = search.toLowerCase();
      return (a.label || '').toLowerCase().includes(q)
          || (a.description || '').toLowerCase().includes(q)
          || (a.author || '').toLowerCase().includes(q);
    }
    return true;
  });

  return (
    <div className="ann-panel">
      <div className="ann-head">
        <div className="ann-title">Annotations</div>
        <button className="ann-x" onClick={onClose} title="Close panel">✕</button>
      </div>

      <div className="ann-toolbar">
        <button className="btn small" onClick={() => fileRef.current.click()}>
          ⬆ Import
        </button>
        <button
          className="btn small"
          onClick={onExport}
          disabled={!annotations || annotations.length === 0}
        >
          ⬇ Export
        </button>
        <input
          ref={fileRef}
          type="file"
          accept="application/json,.json"
          style={{ display: 'none' }}
          onChange={e => {
            const f = e.target.files[0];
            if (f) onImport(f);
            e.target.value = '';
          }}
        />
      </div>

      <div className="ann-filters">
        <input
          className="ann-search"
          placeholder="Search label, description, author…"
          value={search}
          onChange={e => setSearch(e.target.value)}
        />
        <select
          className="ann-cat-filter"
          value={filter}
          onChange={e => setFilter(e.target.value)}
        >
          <option value="all">All categories</option>
          {CATEGORIES.map(c => (
            <option key={c.id} value={c.id}>{c.label}</option>
          ))}
        </select>
      </div>

      <div className="ann-count">
        {filtered.length} of {(annotations || []).length} shown
      </div>

      <div className="ann-list">
        {filtered.length === 0 && (
          <div className="ann-empty">
            {(annotations || []).length === 0
              ? 'No annotations yet. Drag on the annotation track in the timeline to select an interval, then press + to add one.'
              : 'No annotations match the current filter.'}
          </div>
        )}
        {filtered.map(a => (
          <div
            key={a.id}
            className="ann-item"
            onDoubleClick={() => onEdit(a)}
            onClick={() => onJump(a.t_start)}
          >
            <div
              className="ann-swatch"
              style={{ background: COLOR_OF[a.category] || '#888' }}
            />
            <div className="ann-body">
              <div className="ann-row1">
                <span className="ann-time">
                  {fmt(a.t_start, tStart)}
                  {duration(a) > 0.05 && (
                    <> → {fmt(a.t_end, tStart)}</>
                  )}
                </span>
                <span className="ann-cat">{a.category}</span>
                {duration(a) > 0.05 && (
                  <span className="ann-dur">{duration(a).toFixed(2)}s</span>
                )}
              </div>
              <div className="ann-label">
                {a.label || <em className="ann-no-label">(no label)</em>}
              </div>
              {a.description && (
                <div className="ann-desc">{a.description}</div>
              )}
              {a.author && (
                <div className="ann-author">— {a.author}</div>
              )}
            </div>
            <div className="ann-controls" onClick={e => e.stopPropagation()}>
              <button
                className="btn tiny"
                onClick={() => onEdit(a)}
                title="Edit"
              >✎</button>
              <button
                className="btn tiny danger"
                onClick={() => {
                  if (confirm(`Delete annotation "${a.label || a.category}"?`)) {
                    onDelete(a.id);
                  }
                }}
                title="Delete"
              >🗑</button>
            </div>
          </div>
        ))}
      </div>

      <div className="ann-foot">
        <span className="ann-hint">
          Click an annotation to jump · double-click to edit
        </span>
      </div>
    </div>
  );
}

// ── Editor modal ────────────────────────────────────────────────────────
function AnnotationEditor({ draft, tStart, onSave, onCancel, onDelete }) {
  const [form, setForm] = useState({
    category:    'note',
    label:       '',
    description: '',
    author:      '',
    ...draft,
  });
  const labelRef = useRef();

  useEffect(() => {
    // Focus label field on open
    if (labelRef.current) labelRef.current.focus();
  }, []);

  useEffect(() => {
    function onKey(e) {
      if (e.key === 'Escape') onCancel();
      if (e.key === 'Enter' && (e.metaKey || e.ctrlKey)) handleSave();
    }
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [form]);

  function update(k, v) {
    setForm(prev => ({ ...prev, [k]: v }));
  }

  function handleSave() {
    if (form.t_end < form.t_start) {
      // Auto-swap if user inverted the interval
      onSave({ ...form, t_start: form.t_end, t_end: form.t_start });
    } else {
      onSave(form);
    }
  }

  const isEdit = !!form.id;
  const dur    = Math.max(0, (form.t_end || form.t_start) - form.t_start);

  return (
    <div className="ann-modal-bg" onMouseDown={onCancel}>
      <div className="ann-modal" onMouseDown={e => e.stopPropagation()}>
        <div className="ann-modal-head">
          <span>{isEdit ? 'Edit Annotation' : 'New Annotation'}</span>
          <button className="ann-x" onClick={onCancel}>✕</button>
        </div>

        <div className="ann-modal-body">
          <div className="ann-time-summary">
            <div className="ats-cell">
              <div className="ats-label">Start</div>
              <div className="ats-val">{fmt(form.t_start, tStart)}</div>
            </div>
            <div className="ats-arrow">→</div>
            <div className="ats-cell">
              <div className="ats-label">End</div>
              <div className="ats-val">{fmt(form.t_end, tStart)}</div>
            </div>
            <div className="ats-cell">
              <div className="ats-label">Duration</div>
              <div className="ats-val">{dur.toFixed(2)}s</div>
            </div>
          </div>

          <label className="ann-field-label">Category</label>
          <div className="ann-cat-picker">
            {CATEGORIES.map(c => (
              <button
                key={c.id}
                type="button"
                className={`ann-cat-chip${form.category === c.id ? ' selected' : ''}`}
                style={{ borderColor: c.color }}
                onClick={() => update('category', c.id)}
              >
                <span className="acc-swatch" style={{ background: c.color }} />
                {c.label}
              </button>
            ))}
          </div>

          <label className="ann-field-label">Label</label>
          <input
            ref={labelRef}
            className="ann-input"
            value={form.label || ''}
            onChange={e => update('label', e.target.value)}
            placeholder="Short title (e.g., 'gripper slipped during grasp')"
          />

          <label className="ann-field-label">Description</label>
          <textarea
            className="ann-textarea"
            value={form.description || ''}
            onChange={e => update('description', e.target.value)}
            placeholder="What happened? What did the robot or user do? Any context to record?"
            rows={4}
          />

          <label className="ann-field-label">Author</label>
          <input
            className="ann-input"
            value={form.author || ''}
            onChange={e => update('author', e.target.value)}
            placeholder="Initials or name"
          />

          <details className="ann-advanced">
            <summary>Adjust timing manually</summary>
            <div className="ann-time-edit">
              <label>
                Start (s)
                <input
                  type="number"
                  step="0.01"
                  value={form.t_start}
                  onChange={e => update('t_start', parseFloat(e.target.value) || 0)}
                />
              </label>
              <label>
                End (s)
                <input
                  type="number"
                  step="0.01"
                  value={form.t_end}
                  onChange={e => update('t_end', parseFloat(e.target.value) || 0)}
                />
              </label>
            </div>
          </details>
        </div>

        <div className="ann-modal-actions">
          {isEdit && (
            <button
              className="btn danger"
              onClick={() => {
                if (confirm('Delete this annotation?')) {
                  onDelete(form.id);
                }
              }}
            >
              Delete
            </button>
          )}
          <div className="ann-spacer" />
          <button className="btn" onClick={onCancel}>Cancel</button>
          <button
            className="btn primary"
            onClick={handleSave}
            disabled={dur < 0}
          >
            {isEdit ? 'Save' : 'Create'}
          </button>
        </div>

        <div className="ann-modal-hint">
          <kbd>Esc</kbd> cancel · <kbd>Ctrl+Enter</kbd> save
        </div>
      </div>
    </div>
  );
}

// ── Import dialog (asks merge vs replace) ───────────────────────────────
function ImportDialog({ file, onConfirm, onCancel }) {
  const [mode, setMode] = useState('merge');

  return (
    <div className="ann-modal-bg" onMouseDown={onCancel}>
      <div className="ann-modal small" onMouseDown={e => e.stopPropagation()}>
        <div className="ann-modal-head">
          <span>Import Annotations</span>
          <button className="ann-x" onClick={onCancel}>✕</button>
        </div>
        <div className="ann-modal-body">
          <div className="ann-import-file">
            File: <strong>{file.name}</strong>
          </div>
          <label className="ann-radio">
            <input
              type="radio"
              checked={mode === 'merge'}
              onChange={() => setMode('merge')}
            />
            <div>
              <div className="ann-radio-title">Merge</div>
              <div className="ann-radio-sub">
                Add imported annotations to the existing ones.
                Duplicate IDs will get new IDs.
              </div>
            </div>
          </label>
          <label className="ann-radio">
            <input
              type="radio"
              checked={mode === 'replace'}
              onChange={() => setMode('replace')}
            />
            <div>
              <div className="ann-radio-title">Replace</div>
              <div className="ann-radio-sub">
                Delete all existing annotations before importing.
              </div>
            </div>
          </label>
        </div>
        <div className="ann-modal-actions">
          <div className="ann-spacer" />
          <button className="btn" onClick={onCancel}>Cancel</button>
          <button className="btn primary" onClick={() => onConfirm(mode)}>
            Import
          </button>
        </div>
      </div>
    </div>
  );
}

// ── Expose to window ────────────────────────────────────────────────────
window.HariaAnnotationPanel   = AnnotationPanel;
window.HariaAnnotationEditor  = AnnotationEditor;
window.HariaAnnotationImport  = ImportDialog;
window.HARIA_ANN_CATEGORIES   = CATEGORIES;

})();