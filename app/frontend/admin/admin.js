/* ==========================================
   PCE Admin Panel — JavaScript Controller
   ========================================== */

// ─── Configuration ───────────────────────────────────────────────
const API_BASE = window.location.origin.includes('localhost')
  ? 'http://localhost:8000'
  : (window.API_BASE || '');

const ENDPOINTS = {
  login: `${API_BASE}/api/v1/admin/auth/login`,
  pendingMatches: `${API_BASE}/api/v1/admin/pending-matches`,
  reviewMatch: `${API_BASE}/api/v1/admin/review-match`,
  compliance: `${API_BASE}/api/v1/admin/compliance/settings`,
  domains: `${API_BASE}/api/v1/admin/compliance/domains`,
  updateDomain: (id) => `${API_BASE}/api/v1/admin/compliance/domains/${id}`,
  health: `${API_BASE}/health`,
  healthAgents: `${API_BASE}/health/agents`,
};

// ─── State ───────────────────────────────────────────────────────
let state = {
  token: localStorage.getItem('pce_admin_token'),
  email: localStorage.getItem('pce_admin_email'),
  matches: [],
  domains: [],
  selectedMatches: new Set(),
  currentPage: 'review',
};

// ─── API Client ──────────────────────────────────────────────────
async function api(url, options = {}) {
  const opts = {
    headers: {
      'Content-Type': 'application/json',
      ...(state.token ? { 'Authorization': `Bearer ${state.token}` } : {}),
      ...options.headers,
    },
    ...options,
  };

  if (opts.body && typeof opts.body === 'object') {
    opts.body = JSON.stringify(opts.body);
  }

  try {
    const res = await fetch(url, opts);
    if (res.status === 401) {
      logout();
      throw new Error('Session expired. Please log in again.');
    }
    if (!res.ok) {
      const err = await res.json().catch(() => ({}));
      throw new Error(err.detail || `HTTP ${res.status}`);
    }
    return await res.json().catch(() => ({}));
  } catch (e) {
    updateApiStatus(false);
    throw e;
  }
}

// ─── Auth ────────────────────────────────────────────────────────
function isLoggedIn() {
  return !!state.token;
}

function saveSession(token, email) {
  state.token = token;
  state.email = email;
  localStorage.setItem('pce_admin_token', token);
  localStorage.setItem('pce_admin_email', email);
}

function logout() {
  state.token = null;
  state.email = null;
  state.selectedMatches.clear();
  localStorage.removeItem('pce_admin_token');
  localStorage.removeItem('pce_admin_email');
  showView('login-view');
  toast('Logged out', 'success');
}

async function handleLogin(e) {
  e.preventDefault();
  const email = document.getElementById('login-email').value.trim();
  const password = document.getElementById('login-password').value;
  const btn = document.getElementById('login-btn');
  const errorEl = document.getElementById('login-error');

  setLoading(btn, true);
  errorEl.classList.add('hidden');

  try {
    const data = await api(ENDPOINTS.login, {
      method: 'POST',
      body: { email, password },
    });
    saveSession(data.access_token, email);
    document.getElementById('user-email').textContent = email.split('@')[0];
    showView('dashboard-view');
    loadPage('review');
    toast('Welcome back, ' + email.split('@')[0], 'success');
  } catch (err) {
    errorEl.textContent = err.message || 'Invalid credentials';
    errorEl.classList.remove('hidden');
  } finally {
    setLoading(btn, false);
  }
}

// ─── Navigation ──────────────────────────────────────────────────
function showView(viewId) {
  document.querySelectorAll('.view').forEach(v => v.classList.remove('active'));
  document.getElementById(viewId).classList.add('active');
}

function showPage(pageId) {
  state.currentPage = pageId;
  document.querySelectorAll('.page').forEach(p => p.classList.remove('active'));
  document.getElementById(`page-${pageId}`).classList.add('active');
  document.getElementById('page-title').textContent = pageTitle(pageId);

  document.querySelectorAll('.nav-item').forEach(n => n.classList.remove('active'));
  document.querySelector(`.nav-item[data-page="${pageId}"]`)?.classList.add('active');

  // Load page data
  if (pageId === 'review') loadReviewQueue();
  if (pageId === 'compliance') loadCompliance();
  if (pageId === 'domains') loadDomains();
  if (pageId === 'system') loadSystemHealth();
}

function pageTitle(page) {
  const titles = { review: 'Review Queue', compliance: 'Compliance', domains: 'Domain Manager', system: 'System Status' };
  return titles[page] || 'Dashboard';
}

function loadPage(page) {
  showPage(page);
}

// ─── Review Queue ────────────────────────────────────────────────
async function loadReviewQueue() {
  const listEl = document.getElementById('review-list');
  const emptyEl = document.getElementById('review-empty');
  listEl.innerHTML = '<div class="empty-state"><div class="empty-icon">⏳</div><h3>Loading...</h3></div>';

  try {
    const data = await api(ENDPOINTS.pendingMatches);
    state.matches = data.data || [];
    state.selectedMatches.clear();
    updateBulkButtons();
    updateReviewBadge(state.matches.length);

    if (state.matches.length === 0) {
      listEl.classList.add('hidden');
      emptyEl.classList.remove('hidden');
      return;
    }

    listEl.classList.remove('hidden');
    emptyEl.classList.add('hidden');
    renderReviewList(state.matches);
    updateApiStatus(true);
  } catch (err) {
    listEl.innerHTML = `<div class="empty-state"><div class="empty-icon">❌</div><h3>Error</h3><p>${err.message}</p><button class="btn-primary" style="margin-top:16px;" onclick="loadReviewQueue()">Retry</button></div>`;
  }
}

function renderReviewList(matches) {
  const listEl = document.getElementById('review-list');
  listEl.innerHTML = matches.map(m => `
    <div class="review-card" data-offer-id="${m.offer_id}">
      <div class="review-select">
        <input type="checkbox" data-offer-id="${m.offer_id}" onchange="toggleSelect('${m.offer_id}')">
      </div>
      <div class="review-body">
        <div class="review-header">
          <span class="vendor-tag">${escapeHtml(m.vendor_name || 'Unknown')}</span>
          ${m.confidence_score ? `<span class="confidence-badge ${confidenceClass(m.confidence_score)}">${confidenceLabel(m.confidence_score)} • ${Math.round(m.confidence_score * 100)}%</span>` : ''}
        </div>
        <div class="review-title">${escapeHtml(m.vendor_title || 'Untitled')}</div>
        <div class="review-price">₹${m.current_price ? m.current_price.toFixed(2) : '0.00'}</div>
        <div class="review-match">
          <div class="review-match-label">Suggested Match</div>
          <div class="review-match-title">${escapeHtml(m.suggested_product_title || 'New Product')}</div>
        </div>
      </div>
      <div class="review-actions">
        <button class="btn-approve" onclick="approveMatch('${m.offer_id}')">✓ Approve</button>
        <button class="btn-reject" onclick="rejectMatch('${m.offer_id}')">✕ Reject</button>
      </div>
    </div>
  `).join('');
}

function confidenceClass(score) {
  if (score >= 0.92) return 'confidence-high';
  if (score >= 0.85) return 'confidence-medium';
  return 'confidence-low';
}

function confidenceLabel(score) {
  if (score >= 0.92) return 'High';
  if (score >= 0.85) return 'Medium';
  return 'Low';
}

function toggleSelect(offerId) {
  if (state.selectedMatches.has(offerId)) {
    state.selectedMatches.delete(offerId);
  } else {
    state.selectedMatches.add(offerId);
  }
  updateBulkButtons();
}

function updateBulkButtons() {
  const hasSelection = state.selectedMatches.size > 0;
  document.getElementById('bulk-approve-btn').disabled = !hasSelection;
  document.getElementById('bulk-reject-btn').disabled = !hasSelection;
}

async function approveMatch(offerId) {
  const card = document.querySelector(`.review-card[data-offer-id="${offerId}"]`);
  if (card) card.classList.add('processing');

  try {
    await api(ENDPOINTS.reviewMatch, {
      method: 'POST',
      body: { offer_id: offerId, approved: true },
    });
    removeCard(offerId);
    toast('Match approved', 'success');
  } catch (err) {
    if (card) card.classList.remove('processing');
    toast(err.message, 'error');
  }
}

async function rejectMatch(offerId) {
  const card = document.querySelector(`.review-card[data-offer-id="${offerId}"]`);
  if (card) card.classList.add('processing');

  try {
    await api(ENDPOINTS.reviewMatch, {
      method: 'POST',
      body: { offer_id: offerId, approved: false },
    });
    removeCard(offerId);
    toast('Match rejected — new product created', 'success');
  } catch (err) {
    if (card) card.classList.remove('processing');
    toast(err.message, 'error');
  }
}

async function bulkApprove() {
  const ids = Array.from(state.selectedMatches);
  if (ids.length === 0) return;

  let success = 0;
  for (const id of ids) {
    try {
      await api(ENDPOINTS.reviewMatch, {
        method: 'POST',
        body: { offer_id: id, approved: true },
      });
      removeCard(id);
      success++;
    } catch (err) {
      toast(`Failed to approve ${id}`, 'error');
    }
  }
  state.selectedMatches.clear();
  updateBulkButtons();
  toast(`${success} matches approved`, 'success');
}

async function bulkReject() {
  const ids = Array.from(state.selectedMatches);
  if (ids.length === 0) return;

  let success = 0;
  for (const id of ids) {
    try {
      await api(ENDPOINTS.reviewMatch, {
        method: 'POST',
        body: { offer_id: id, approved: false },
      });
      removeCard(id);
      success++;
    } catch (err) {
      toast(`Failed to reject ${id}`, 'error');
    }
  }
  state.selectedMatches.clear();
  updateBulkButtons();
  toast(`${success} matches rejected`, 'success');
}

function removeCard(offerId) {
  const card = document.querySelector(`.review-card[data-offer-id="${offerId}"]`);
  if (card) {
    card.classList.add('removing');
    setTimeout(() => {
      card.remove();
      state.matches = state.matches.filter(m => m.offer_id !== offerId);
      updateReviewBadge(state.matches.length);
      if (state.matches.length === 0) {
        document.getElementById('review-list').classList.add('hidden');
        document.getElementById('review-empty').classList.remove('hidden');
      }
    }, 400);
  }
}

function updateReviewBadge(count) {
  const badge = document.getElementById('badge-review');
  if (count > 0) {
    badge.textContent = count;
    badge.classList.remove('hidden');
  } else {
    badge.classList.add('hidden');
  }
}

function filterReviewQueue(query) {
  const q = query.toLowerCase();
  const filtered = state.matches.filter(m =>
    (m.vendor_name || '').toLowerCase().includes(q) ||
    (m.vendor_title || '').toLowerCase().includes(q) ||
    (m.suggested_product_title || '').toLowerCase().includes(q)
  );
  if (filtered.length === 0) {
    document.getElementById('review-list').innerHTML = '<div class="empty-state"><div class="empty-icon">🔍</div><h3>No matches found</h3><p>Try a different search term.</p></div>';
    document.getElementById('review-list').classList.remove('hidden');
    document.getElementById('review-empty').classList.add('hidden');
  } else {
    renderReviewList(filtered);
    document.getElementById('review-list').classList.remove('hidden');
    document.getElementById('review-empty').classList.add('hidden');
  }
}

// ─── Compliance ──────────────────────────────────────────────────
async function loadCompliance() {
  try {
    const data = await api(ENDPOINTS.compliance);
    document.getElementById('toggle-scraping').checked = data.scraping_enabled;
    document.getElementById('toggle-robots').checked = data.enforce_robots_txt;
    document.getElementById('toggle-allowlist').checked = data.enforce_domain_allowlist;
    document.getElementById('input-rpm').value = data.default_scrape_rpm;
    document.getElementById('compliance-json').textContent = JSON.stringify(data, null, 2);
    updateApiStatus(true);
  } catch (err) {
    document.getElementById('compliance-json').textContent = 'Error: ' + err.message;
  }
}

async function saveCompliance() {
  const btn = document.getElementById('save-compliance-btn');
  setLoading(btn, true);

  try {
    await api(ENDPOINTS.compliance, {
      method: 'POST',
      body: {
        scraping_enabled: document.getElementById('toggle-scraping').checked,
        enforce_robots_txt: document.getElementById('toggle-robots').checked,
        enforce_domain_allowlist: document.getElementById('toggle-allowlist').checked,
        default_scrape_rpm: parseInt(document.getElementById('input-rpm').value, 10),
      },
    });
    toast('Compliance settings saved', 'success');
    loadCompliance();
  } catch (err) {
    toast(err.message, 'error');
  } finally {
    setLoading(btn, false);
  }
}

// ─── Domains ─────────────────────────────────────────────────────
async function loadDomains() {
  const tbody = document.getElementById('domains-tbody');
  tbody.innerHTML = '<tr><td colspan="8" class="table-loading">Loading domains...</td></tr>';

  try {
    const data = await api(ENDPOINTS.domains);
    state.domains = data.data || [];
    renderDomains(state.domains);
    updateApiStatus(true);
  } catch (err) {
    tbody.innerHTML = `<tr><td colspan="8" class="table-loading">Error: ${err.message}</td></tr>`;
  }
}

function renderDomains(domains) {
  const tbody = document.getElementById('domains-tbody');
  if (domains.length === 0) {
    tbody.innerHTML = '<tr><td colspan="8" class="table-loading">No domains found.</td></tr>';
    return;
  }

  tbody.innerHTML = domains.map(d => `
    <tr data-domain-id="${d.id}">
      <td><strong>${escapeHtml(d.name)}</strong></td>
      <td><code>${escapeHtml(d.domain)}</code></td>
      <td><span class="status-pill ${d.is_active ? 'on' : 'off'}">${d.is_active ? 'Yes' : 'No'}</span></td>
      <td>
        <label class="switch" style="width:40px;height:22px;">
          <input type="checkbox" ${d.scraping_allowed ? 'checked' : ''}
            onchange="toggleDomainScraping('${d.id}', this.checked)">
          <span class="slider" style="border-radius:22px;"></span>
        </label>
      </td>
      <td>
        <input type="number" value="${d.scrape_rpm}" min="1" max="120" style="width:60px;padding:6px;"
          onchange="updateDomainRpm('${d.id}', this.value)">
      </td>
      <td><span class="status-pill ${d.respects_robots_txt ? 'on' : 'off'}">${d.respects_robots_txt ? 'Yes' : 'No'}</span></td>
      <td>
        ${d.title_selector ? '✓' : '✗'} Title
        ${d.price_selector ? '✓' : '✗'} Price
      </td>
      <td>
        <button class="btn-ghost" onclick="saveDomainChanges('${d.id}')" title="Save">💾</button>
      </td>
    </tr>
  `).join('');
}

let domainChanges = {};

function toggleDomainScraping(id, allowed) {
  domainChanges[id] = domainChanges[id] || {};
  domainChanges[id].scraping_allowed = allowed;
}

function updateDomainRpm(id, rpm) {
  domainChanges[id] = domainChanges[id] || {};
  domainChanges[id].scrape_rpm = parseInt(rpm, 10);
}

async function saveDomainChanges(id) {
  const changes = domainChanges[id];
  if (!changes) return;

  try {
    await api(ENDPOINTS.updateDomain(id), {
      method: 'PATCH',
      body: changes,
    });
    delete domainChanges[id];
    toast('Domain updated', 'success');
    loadDomains();
  } catch (err) {
    toast(err.message, 'error');
  }
}

function filterDomains(query) {
  const q = query.toLowerCase();
  const filtered = state.domains.filter(d =>
    (d.name || '').toLowerCase().includes(q) ||
    (d.domain || '').toLowerCase().includes(q)
  );
  renderDomains(filtered);
}

// ─── System Health ───────────────────────────────────────────────
async function loadSystemHealth() {
  // API Health
  try {
    const health = await api(ENDPOINTS.health);
    const el = document.getElementById('health-api');
    el.innerHTML = `
      <div style="font-size:28px;margin-bottom:8px;">🟢</div>
      <div class="health-status online">Online — v${health.version || '?'}</div>
      <div style="margin-top:8px;color:var(--text-muted);font-size:12px;">
        ${(health.features || []).join(' • ')}
      </div>
    `;
    document.getElementById('health-raw').textContent = JSON.stringify(health, null, 2);
    updateApiStatus(true);
  } catch (err) {
    document.getElementById('health-api').innerHTML = `
      <div style="font-size:28px;margin-bottom:8px;">🔴</div>
      <div class="health-status error">${err.message}</div>
    `;
    document.getElementById('health-raw').textContent = err.message;
    updateApiStatus(false);
  }

  // Agent Health
  try {
    const agents = await api(ENDPOINTS.healthAgents);
    const el = document.getElementById('health-agents');
    const isActive = agents.agents_enabled;
    el.innerHTML = `
      <div style="font-size:28px;margin-bottom:8px;">${isActive ? '🤖' : '💤'}</div>
      <div class="health-status ${isActive ? 'online' : 'standby'}">
        ${isActive ? 'Active' : 'Standby'}
      </div>
      <div style="margin-top:8px;color:var(--text-muted);font-size:12px;">
        ${agents.message || ''}
      </div>
    `;
  } catch (err) {
    document.getElementById('health-agents').innerHTML = `
      <div style="font-size:28px;margin-bottom:8px;">⚠️</div>
      <div class="health-status error">${err.message}</div>
    `;
  }
}

// ─── UI Helpers ──────────────────────────────────────────────────
function updateApiStatus(online) {
  const dot = document.querySelector('#api-status .status-dot');
  const text = document.querySelector('#api-status .status-text');
  if (online) {
    dot.className = 'status-dot online';
    text.textContent = 'Online';
  } else {
    dot.className = 'status-dot offline';
    text.textContent = 'Offline';
  }
}

function setLoading(btn, loading) {
  const text = btn.querySelector('.btn-text');
  const spinner = btn.querySelector('.btn-spinner');
  if (text) text.classList.toggle('hidden', loading);
  if (spinner) spinner.classList.toggle('hidden', !loading);
  btn.disabled = loading;
}

function toast(message, type = 'success') {
  const container = document.getElementById('toast-container');
  const el = document.createElement('div');
  el.className = `toast-item ${type}`;
  el.textContent = message;
  container.appendChild(el);
  setTimeout(() => el.remove(), 3000);
}

function escapeHtml(text) {
  if (!text) return '';
  const div = document.createElement('div');
  div.textContent = text;
  return div.innerHTML;
}

// ─── Event Listeners ─────────────────────────────────────────────
document.addEventListener('DOMContentLoaded', () => {
  // Login form
  document.getElementById('login-form').addEventListener('submit', handleLogin);

  // Logout
  document.getElementById('logout-btn').addEventListener('click', logout);

  // Navigation
  document.querySelectorAll('.nav-item').forEach(item => {
    item.addEventListener('click', (e) => {
      e.preventDefault();
      const page = item.dataset.page;
      if (page) loadPage(page);
    });
  });

  // Refresh button
  document.getElementById('refresh-btn').addEventListener('click', () => {
    loadPage(state.currentPage);
    toast('Refreshed', 'success');
  });

  // Bulk actions
  document.getElementById('bulk-approve-btn').addEventListener('click', bulkApprove);
  document.getElementById('bulk-reject-btn').addEventListener('click', bulkReject);

  // Review search
  document.getElementById('review-search').addEventListener('input', (e) => {
    filterReviewQueue(e.target.value);
  });

  // Domain search
  document.getElementById('domain-search').addEventListener('input', (e) => {
    filterDomains(e.target.value);
  });

  // Compliance save
  document.getElementById('save-compliance-btn').addEventListener('click', saveCompliance);

  // Init
  if (isLoggedIn()) {
    document.getElementById('user-email').textContent = (state.email || 'admin').split('@')[0];
    showView('dashboard-view');
    loadPage('review');
  } else {
    showView('login-view');
  }
});
