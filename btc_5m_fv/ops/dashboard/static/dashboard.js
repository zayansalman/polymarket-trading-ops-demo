/**
 * BTC 5m Binary Fair Value Dashboard — Client-Side Logic
 *
 * - Tab switching (vanilla JS, no framework)
 * - Start / Stop / Refresh button handlers (fetch API)
 * - Server-Sent Events for live updates (replaces gr.Timer polling)
 * - Error handling and automatic reconnection
 * - Toast notifications
 */

// ---------------------------------------------------------------------------
// Tab Switching
// ---------------------------------------------------------------------------

function showTab(tabName) {
  // Hide all tab contents
  document.querySelectorAll('.tab-content').forEach(function(el) {
    el.classList.remove('active');
  });
  // Deactivate all tab buttons
  document.querySelectorAll('.tab-btn').forEach(function(el) {
    el.classList.remove('active');
  });
  // Activate selected tab
  var contentEl = document.getElementById('tab-' + tabName);
  var btnEl = document.querySelector('.tab-btn[data-tab="' + tabName + '"]');
  if (contentEl) contentEl.classList.add('active');
  if (btnEl) btnEl.classList.add('active');
  // Store preference
  try { localStorage.setItem('btc-dashboard-active-tab', tabName); } catch (e) {}
}

// Restore active tab on load
document.addEventListener('DOMContentLoaded', function() {
  try {
    var saved = localStorage.getItem('btc-dashboard-active-tab');
    if (saved) showTab(saved);
  } catch (e) {}
});

// ---------------------------------------------------------------------------
// Toast Notifications
// ---------------------------------------------------------------------------

function showToast(message, type) {
  type = type || 'info';
  var container = document.querySelector('.toast-container');
  if (!container) {
    container = document.createElement('div');
    container.className = 'toast-container';
    document.body.appendChild(container);
  }
  var toast = document.createElement('div');
  toast.className = 'toast ' + type;
  toast.textContent = message;
  container.appendChild(toast);
  setTimeout(function() {
    toast.classList.add('fade-out');
    setTimeout(function() { toast.remove(); }, 300);
  }, 3000);
}

// ---------------------------------------------------------------------------
// Button Handlers
// ---------------------------------------------------------------------------

function setButtonsDisabled(disabled) {
  document.querySelectorAll('.btn-row .btn').forEach(function(btn) {
    btn.disabled = disabled;
  });
}

function handleStart() {
  setButtonsDisabled(true);
  fetch('/api/start', { method: 'POST' })
    .then(function(r) { return r.json(); })
    .then(function(data) {
      showToast('Bot started: ' + (data.detail || data.status), 'success');
      refreshAll();
    })
    .catch(function(err) {
      showToast('Start failed: ' + err.message, 'error');
      console.error('Start error:', err);
    })
    .finally(function() { setButtonsDisabled(false); });
}

function handleStop() {
  setButtonsDisabled(true);
  fetch('/api/stop', { method: 'POST' })
    .then(function(r) { return r.json(); })
    .then(function(data) {
      showToast('Bot stopped: ' + (data.detail || data.status), 'info');
      refreshAll();
    })
    .catch(function(err) {
      showToast('Stop failed: ' + err.message, 'error');
      console.error('Stop error:', err);
    })
    .finally(function() { setButtonsDisabled(false); });
}

function handleRefresh() {
  setButtonsDisabled(true);
  showToast('Refreshing...', 'info');
  refreshAll()
    .then(function() {
      showToast('Data refreshed', 'success');
    })
    .catch(function(err) {
      showToast('Refresh failed: ' + err.message, 'error');
    })
    .finally(function() { setButtonsDisabled(false); });
}

function handleRefreshBacktest() {
  var btn = document.getElementById('btn-refresh-backtest');
  if (btn) btn.disabled = true;
  fetch('/api/data')
    .then(function(r) { return r.json(); })
    .then(function(data) {
      var el = document.getElementById('backtest-content');
      if (el && data.backtest) el.innerHTML = data.backtest;
      showToast('Backtest report refreshed', 'success');
    })
    .catch(function(err) {
      showToast('Refresh failed: ' + err.message, 'error');
    })
    .finally(function() {
      if (btn) btn.disabled = false;
    });
}

// ---------------------------------------------------------------------------
// Data Refresh — updates DOM from JSON payload
// ---------------------------------------------------------------------------

function refreshAll() {
  return fetch('/api/data')
    .then(function(r) {
      if (!r.ok) throw new Error('HTTP ' + r.status);
      return r.json();
    })
    .then(function(data) {
      updateDashboard(data);
    });
}

function updateDashboard(data) {
  if (!data) return;

  // Overview
  if (data.overview) {
    var ov = document.getElementById('overview-content');
    if (ov) ov.innerHTML = data.overview.html || '';
  }

  // Paper (BTC 5m tab)
  if (data.paper) {
    var paperEl = document.getElementById('paper-content');
    if (paperEl) paperEl.innerHTML = data.paper.html || '';
  }

  // Status
  if (data.overview && data.overview.status) {
    var st = document.getElementById('status-content');
    if (st) st.innerHTML = data.overview.status || '';
  }

  // Activity
  if (data.activity) {
    var act = document.getElementById('activity-content');
    if (act) act.innerHTML = data.activity || '';
  }

  // History
  if (data.history) {
    var hist = document.getElementById('history-content');
    if (hist) hist.innerHTML = data.history || '';
  }

  // Backtest
  if (data.backtest) {
    var bt = document.getElementById('backtest-content');
    if (bt) bt.innerHTML = data.backtest || '';
  }
}

// ---------------------------------------------------------------------------
// Server-Sent Events (replaces gr.Timer polling)
// ---------------------------------------------------------------------------

var sseReconnectDelay = 1000;
var sseMaxReconnectDelay = 30000;
var sseReconnectTimer = null;
var eventSource = null;

function updateSseIndicator(state) {
  var dot = document.querySelector('.sse-dot');
  if (!dot) return;
  dot.classList.remove('connected', 'disconnected', 'connecting');
  dot.classList.add(state);
}

function connectSSE() {
  if (eventSource) {
    try { eventSource.close(); } catch (e) {}
  }

  updateSseIndicator('connecting');

  eventSource = new EventSource('/api/stream');

  eventSource.onopen = function() {
    updateSseIndicator('connected');
    sseReconnectDelay = 1000; // reset backoff
  };

  eventSource.onmessage = function(event) {
    try {
      var data = JSON.parse(event.data);
      updateDashboard(data);
    } catch (err) {
      console.error('SSE parse error:', err);
    }
  };

  eventSource.onerror = function() {
    updateSseIndicator('disconnected');
    try { eventSource.close(); } catch (e) {}
    eventSource = null;

    // Exponential backoff
    sseReconnectDelay = Math.min(sseReconnectDelay * 2, sseMaxReconnectDelay);
    sseReconnectTimer = setTimeout(connectSSE, sseReconnectDelay);
  };
}

function disconnectSSE() {
  if (sseReconnectTimer) {
    clearTimeout(sseReconnectTimer);
    sseReconnectTimer = null;
  }
  if (eventSource) {
    try { eventSource.close(); } catch (e) {}
    eventSource = null;
  }
  updateSseIndicator('disconnected');
}

// Start SSE on load
document.addEventListener('DOMContentLoaded', function() {
  connectSSE();
});

// Graceful disconnect on page unload
window.addEventListener('beforeunload', function() {
  disconnectSSE();
});
