(function() {
const { useState, useEffect, useRef, useCallback, useReducer } = React;
const API = window.HARIA_API || 'http://localhost:8000';
const _API = API;

/**
 * HARIA Failure Dashboard — Sidebar
 * Renders the topic list and emits addPanel events.
 */


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

function suggestType(meta) {
  if (!meta) return 'json';
  if (meta.is_image) return 'image';
  if (meta.is_table) return 'table';
  if (meta.is_num)   return 'chart';
  return 'json';
}

function badgeLabel(meta) {
  if (meta.is_image) return 'img';
  if (meta.is_table) return 'tbl';
  if (meta.is_num)   return 'num';
  return 'raw';
}

function groupTopics(topics) {
  return {
    'Image':   topics.filter(t => t.is_image),
    'Numeric': topics.filter(t => t.is_num  && !t.is_image),
    'Table':   topics.filter(t => t.is_table && !t.is_image && !t.is_num),
    'Other':   topics.filter(t => !t.is_image && !t.is_num && !t.is_table),
  };
}

// ── TypePickerPopup ──────────────────────────────────────────────────────
function TypePickerPopup({ topicMeta, onSelect, onCancel }) {
  return (
    <div style={{
      position: 'fixed', inset: 0, background: 'rgba(0,0,0,0.3)',
      display: 'flex', alignItems: 'center', justifyContent: 'center', zIndex: 2000,
    }} onClick={onCancel}>
      <div style={{
        background: 'var(--white)', border: '2px solid var(--black)',
        padding: '24px', width: 280, boxShadow: '4px 4px 0 var(--black)',
      }} onClick={e => e.stopPropagation()}>
        <div style={{ fontFamily: 'var(--mono)', fontSize: 9, letterSpacing: '0.2em', textTransform: 'uppercase', color: 'var(--g3)', marginBottom: 14 }}>
          Choose panel type for<br/>
          <span style={{ color: 'var(--black)', fontSize: 11 }}>{topicMeta.topic}</span>
        </div>
        <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 6 }}>
          {PANEL_TYPES.map(t => (
            <button key={t.id} className="tp-btn" onClick={() => onSelect(t.id)}>
              {t.label}
            </button>
          ))}
        </div>
      </div>
    </div>
  );
}

// ── Sidebar ──────────────────────────────────────────────────────────────
function Sidebar({ topicIndex, openTopics, onAddPanel }) {
  const [search, setSearch] = useState('');
  const [pickerTopic, setPickerTopic] = useState(null);

  const filtered = search
    ? topicIndex.filter(t => t.topic.toLowerCase().includes(search.toLowerCase()))
    : topicIndex;

  const groups = groupTopics(filtered);

  function handleTopicClick(meta) {
    // If already open, add another panel (user may want e.g. chart + table of same topic)
    // Show type picker only if not an obvious single-type topic
    if (meta.is_image) {
      onAddPanel(meta, 'image');
    } else {
      setPickerTopic(meta);
    }
  }

  function handlePickerSelect(panelType) {
    onAddPanel(pickerTopic, panelType);
    setPickerTopic(null);
  }

  return (
    <>
      <div className="sidebar">
        <div className="sb-head">
          <span>Topics</span>
          <span className="sb-count">{topicIndex.filter(t => t.active).length}/{topicIndex.length}</span>
        </div>
        <input
          className="sb-search"
          placeholder="Filter topics…"
          value={search}
          onChange={e => setSearch(e.target.value)}
        />
        <div className="sb-scroll">
          {topicIndex.length === 0 && (
            <div className="no-topics">Discovering topics…</div>
          )}
          {Object.entries(groups).map(([groupName, items]) =>
            items.length === 0 ? null : (
              <div key={groupName}>
                <div className="sb-group-label">{groupName}</div>
                {items.map(t => {
                  const isOpen = openTopics.has(t.topic);
                  return (
                    <div
                      key={t.topic}
                      className={`topic-item${isOpen ? ' open' : ''}`}
                      onClick={() => handleTopicClick(t)}
                      title={`${t.topic}\n${t.msg_type}\n${t.count || 0} msgs`}
                    >
                      <div className={`ti-dot${t.active ? (isOpen ? ' live-inv' : ' live') : ''}`} />
                      <div className="ti-name">{t.topic}</div>
                      <div className="ti-badge">{badgeLabel(t)}</div>
                    </div>
                  );
                })}
              </div>
            )
          )}
        </div>
      </div>

      {pickerTopic && (
        <TypePickerPopup
          topicMeta={pickerTopic}
          onSelect={handlePickerSelect}
          onCancel={() => setPickerTopic(null)}
        />
      )}
    </>
  );
}

window.HariaSidebar = Sidebar;
window.HariaSuggestType = suggestType;
window.HariaPanelTypes = PANEL_TYPES;

})();
