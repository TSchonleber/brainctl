/* ===== brainctl Explorer App ===== */

const API = '';
let currentSection = 'memories';
let currentSearch = '';
let loadedCount = 0;
const PAGE_SIZE = 20;

// ===== INIT =====
document.addEventListener('DOMContentLoaded', () => {
  loadStats();
  loadUpdateStatus();
  loadSection('memories');
  bindEvents();
});

function bindEvents() {
  // View switcher
  document.querySelectorAll('.view-btn').forEach(btn => {
    btn.addEventListener('click', () => {
      document.querySelectorAll('.view-btn').forEach(b => b.classList.remove('active'));
      btn.classList.add('active');
      const view = btn.dataset.view;
      document.querySelectorAll('.view').forEach(v => v.classList.remove('active'));
      if (view === 'explorer') {
        document.getElementById('explorerView').classList.add('active');
      } else {
        document.getElementById('neuralView').classList.add('active');
        if (typeof initNeural === 'function') initNeural();
      }
    });
  });

  // Sidebar nav
  document.querySelectorAll('.nav-item').forEach(item => {
    item.addEventListener('click', () => {
      document.querySelectorAll('.nav-item').forEach(n => n.classList.remove('active'));
      item.classList.add('active');
      loadSection(item.dataset.section);
    });
  });

  // Global search
  let searchTimeout;
  document.getElementById('globalSearch').addEventListener('input', (e) => {
    clearTimeout(searchTimeout);
    searchTimeout = setTimeout(() => {
      currentSearch = e.target.value;
      loadedCount = 0;
      loadSection(currentSection);
    }, 300);
  });
}

// ===== STATS =====
async function loadStats() {
  try {
    const res = await fetch(`${API}/api/stats`);
    const data = await res.json();
    const pills = document.getElementById('statsPills');
    pills.innerHTML = `
      <div class="stat-pill"><span class="stat-num">${fmtNum(data.memories)}</span>memories</div>
      <div class="stat-pill"><span class="stat-num">${fmtNum(data.entities)}</span>entities</div>
      <div class="stat-pill"><span class="stat-num">${fmtNum(data.events)}</span>events</div>
    `;
  } catch (e) { console.error('Stats error:', e); }
}

async function loadUpdateStatus(force = false) {
  try {
    const query = force ? '?refresh=1' : '';
    const res = await fetch(`${API}/api/update${query}`);
    const data = await res.json();
    const pills = document.getElementById('statsPills');
    if (!pills) return;

    const oldPill = document.getElementById('updateStatusPill');
    if (oldPill) oldPill.remove();

    const label = data.update_available
      ? `update ${escHtml(data.remote_commit || 'available')}`
      : data.local_commit
        ? `up to date ${escHtml(data.local_commit)}`
        : 'version unknown';
    const cls = data.update_available ? 'stat-pill stat-pill-warn' : 'stat-pill stat-pill-ok';
    const title = data.update_available
      ? `Local ${data.local_commit || 'unknown'} behind ${data.default_branch || 'remote'} ${data.remote_commit || 'unknown'}`
      : data.remote_url
        ? `Checked against ${data.default_branch || 'remote'}`
        : 'No remote version source configured';

    pills.insertAdjacentHTML('beforeend', `
      <div id="updateStatusPill" class="${cls}" title="${escHtml(title)}">
        <span class="stat-num">${label}</span>
      </div>
    `);
  } catch (e) {
    console.error('Update status error:', e);
  }
}

// ===== SECTIONS =====
async function loadSection(section) {
  currentSection = section;
  loadedCount = 0;
  const area = document.getElementById('contentArea');
  area.innerHTML = '<div style="color:#555;padding:40px;text-align:center">Loading…</div>';

  try {
    if (section === 'health') return loadHealth(area);
    const endpoint = `/api/${section}`;
    const params = new URLSearchParams();
    if (currentSearch) params.set('search', currentSearch);
    params.set('limit', String(PAGE_SIZE));
    const res = await fetch(`${API}${endpoint}?${params}`);
    const data = await res.json();
    loadedCount = data.length;
    renderSection(area, section, data);
  } catch (e) {
    area.innerHTML = `<div class="empty-state"><div class="empty-icon">⚠️</div><p>${e.message}</p></div>`;
  }
}

async function loadMore() {
  const params = new URLSearchParams();
  if (currentSearch) params.set('search', currentSearch);
  params.set('limit', String(loadedCount + PAGE_SIZE));
  const res = await fetch(`${API}/api/${currentSection}?${params}`);
  const data = await res.json();
  loadedCount = data.length;
  renderSection(document.getElementById('contentArea'), currentSection, data);
}

function renderSection(area, section, data) {
  const titles = {
    memories: 'Memories', entities: 'Entities', events: 'Events',
    decisions: 'Decisions', triggers: 'Triggers'
  };
  if (!data.length) {
    area.innerHTML = `<h2 class="section-title">${titles[section] || section}</h2>
      <div class="empty-state"><div class="empty-icon">📭</div><p>No ${section} found</p></div>`;
    return;
  }

  let html = `<h2 class="section-title">${titles[section] || section}</h2>`;
  const renderer = renderers[section] || renderers.generic;
  data.forEach(item => { html += renderer(item); });

  if (data.length >= loadedCount && data.length % PAGE_SIZE === 0) {
    html += `<div class="load-more-wrap"><button class="load-more-btn" onclick="loadMore()">Load more</button></div>`;
  }
  area.innerHTML = html;
}

// ===== RENDERERS =====
const CAT_COLORS = {
  semantic: '#4fc3f7', episodic: '#81c784', procedural: '#ffb74d',
  decision: '#ce93d8', general: '#888', system: '#ef5350'
};
const TYPE_EMOJIS = {
  person: '👤', agent: '🤖', tool: '🔧', organization: '🏢',
  project: '📁', concept: '💡'
};

const renderers = {
  memories(item) {
    const conf = item.confidence || 0;
    const confClass = conf >= 0.7 ? 'conf-high' : conf >= 0.4 ? 'conf-med' : 'conf-low';
    const catColor = CAT_COLORS[item.category] || '#888';
    return `<div class="card">
      <div class="card-header">
        <div class="card-meta">
          <span class="cat-badge"><span class="cat-dot" style="background:${catColor}"></span>${item.category || 'general'}</span>
          <div class="confidence-bar"><div class="confidence-bar-fill ${confClass}" style="width:${conf * 100}%"></div></div>
        </div>
        <span class="time-ago">${timeAgo(item.created_at)}</span>
      </div>
      <div class="card-body">${escHtml(item.content || '')}</div>
    </div>`;
  },

  entities(item) {
    const type = item.entity_type || 'unknown';
    const emoji = TYPE_EMOJIS[type] || '❓';
    let obs = [];
    try { obs = JSON.parse(item.observations || '[]'); } catch(e) {}
    return `<div class="card">
      <div class="card-header">
        <div style="display:flex;align-items:center;gap:10px">
          <span style="font-size:20px">${emoji}</span>
          <span class="card-title">${escHtml(item.name)}</span>
          <span class="entity-type type-${type}">${type}</span>
        </div>
        <div class="card-meta">
          <div class="confidence-bar"><div class="confidence-bar-fill ${item.confidence >= 0.7 ? 'conf-high' : 'conf-med'}" style="width:${(item.confidence || 0) * 100}%"></div></div>
          <span class="time-ago">${timeAgo(item.created_at)}</span>
        </div>
      </div>
      ${obs.length ? `<div class="card-body">${obs.slice(0, 3).map(o => escHtml(o)).join(' · ')}</div>` : ''}
    </div>`;
  },

  events(item) {
    return `<div class="card">
      <div class="card-header">
        <div class="card-meta">
          <span class="cat-badge"><span class="cat-dot" style="background:#4fc3f7"></span>${item.event_type || 'event'}</span>
          ${item.project ? `<span class="cat-badge">${escHtml(item.project)}</span>` : ''}
        </div>
        <span class="time-ago">${timeAgo(item.created_at)}</span>
      </div>
      <div class="card-body">${escHtml(item.summary || item.description || '')}</div>
    </div>`;
  },

  decisions(item) {
    return `<div class="card">
      <div class="card-header">
        <span class="card-title">${escHtml(item.title || item.decision || '')}</span>
        <span class="time-ago">${timeAgo(item.created_at)}</span>
      </div>
      <div class="card-body">
        ${item.reasoning ? `<div style="margin-bottom:6px;color:#bbb">${escHtml(item.reasoning)}</div>` : ''}
        ${item.outcome ? `<span class="decision-outcome" style="background:rgba(129,199,132,0.12);color:#81c784">${escHtml(item.outcome)}</span>` : ''}
      </div>
    </div>`;
  },

  triggers(item) {
    const statusClass = item.status === 'active' ? 'status-active' : item.status === 'fired' ? 'status-fired' : 'status-expired';
    return `<div class="card">
      <div class="card-header">
        <span class="card-title">${escHtml(item.trigger_condition || item.condition || '')}</span>
        <div class="card-meta">
          <span class="trigger-status ${statusClass}">${item.status || 'unknown'}</span>
          <span class="time-ago">${timeAgo(item.created_at)}</span>
        </div>
      </div>
      <div class="card-body">${escHtml(item.action || '')}</div>
    </div>`;
  },

  generic(item) {
    return `<div class="card"><div class="card-body">${escHtml(JSON.stringify(item, null, 2))}</div></div>`;
  }
};

// ===== HEALTH =====
async function loadHealth(area) {
  const res = await fetch(`${API}/api/health`);
  const d = await res.json();
  let html = '<h2 class="section-title">Health</h2><div class="health-grid">';
  const items = [
    ['Active Memories', d.active_memories, '💭'],
    ['Retired', d.retired_memories, '🗑'],
    ['Entities', d.active_entities, '🔮'],
    ['Active Triggers', d.active_triggers, '🔔'],
    ['Avg Confidence', d.avg_confidence, '📈'],
    ['DB Size', d.db_size_kb ? d.db_size_kb + ' KB' : '—', '💾'],
  ];
  items.forEach(([label, val, icon]) => {
    html += `<div class="health-card">
      <div style="font-size:24px;margin-bottom:8px">${icon}</div>
      <div class="health-value">${val ?? '—'}</div>
      <div class="health-label">${label}</div>
    </div>`;
  });
  html += '</div>';

  if (d.categories && d.categories.length) {
    html += '<h3 style="font-size:14px;color:#888;margin-bottom:12px">Categories</h3><div style="display:flex;gap:8px;flex-wrap:wrap;margin-bottom:24px">';
    d.categories.forEach(c => {
      const color = CAT_COLORS[c.category] || '#888';
      html += `<span class="cat-badge"><span class="cat-dot" style="background:${color}"></span>${c.category}: ${c.cnt}</span>`;
    });
    html += '</div>';
  }

  // Token cost section
  try {
    const costRes = await fetch(`${API}/api/cost`);
    const cost = await costRes.json();
    html += '<h3 style="font-size:14px;color:#888;margin:24px 0 12px">Token Cost (7 days)</h3><div class="health-grid">';
    html += `<div class="health-card"><div style="font-size:24px;margin-bottom:8px">🔥</div><div class="health-value">${fmtNum(cost.last_7_days?.tokens || 0)}</div><div class="health-label">Tokens consumed</div></div>`;
    html += `<div class="health-card"><div style="font-size:24px;margin-bottom:8px">📊</div><div class="health-value">${cost.last_7_days?.avg_per_query || 0}</div><div class="health-label">Avg tokens/query</div></div>`;
    html += `<div class="health-card"><div style="font-size:24px;margin-bottom:8px">🔍</div><div class="health-value">${fmtNum(cost.last_7_days?.queries || 0)}</div><div class="health-label">Queries (7d)</div></div>`;
    html += `<div class="health-card"><div style="font-size:24px;margin-bottom:8px">📅</div><div class="health-value">${fmtNum(cost.today?.tokens || 0)}</div><div class="health-label">Tokens today</div></div>`;
    html += '</div>';
    if (cost.top_agents?.length) {
      html += '<h3 style="font-size:14px;color:#888;margin:16px 0 12px">Top Token Consumers</h3><div style="display:flex;gap:8px;flex-wrap:wrap;margin-bottom:24px">';
      cost.top_agents.forEach(a => {
        html += `<span class="cat-badge" style="padding:4px 10px"><span class="cat-dot" style="background:#ef5350"></span>${escHtml(a.agent)}: ${fmtNum(a.tokens)} tokens</span>`;
      });
      html += '</div>';
    }
  } catch (e) { console.error('Cost API error:', e); }

  area.innerHTML = html;
}

// ===== UTILS =====
function timeAgo(ts) {
  if (!ts) return '';
  const now = Date.now();
  const then = new Date(ts).getTime();
  const diff = now - then;
  if (diff < 0) return 'just now';
  const mins = Math.floor(diff / 60000);
  if (mins < 1) return 'just now';
  if (mins < 60) return `${mins}m ago`;
  const hrs = Math.floor(mins / 60);
  if (hrs < 24) return `${hrs}h ago`;
  const days = Math.floor(hrs / 24);
  if (days < 30) return `${days}d ago`;
  const months = Math.floor(days / 30);
  return `${months}mo ago`;
}

function fmtNum(n) {
  if (n == null) return '0';
  if (n >= 1000) return (n / 1000).toFixed(1) + 'k';
  return String(n);
}

function escHtml(s) {
  const d = document.createElement('div');
  d.textContent = s;
  return d.innerHTML;
}
