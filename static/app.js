// -----------------------------------------------------------------------
// State
// -----------------------------------------------------------------------
let allRecords = [];
let validDates = new Set();
let sortState = { col: 'score', dir: 'desc' };
let tierFilter = 'all'; // 'all' or number
let strongLoaded = false;

// -----------------------------------------------------------------------
// Formatters
// -----------------------------------------------------------------------
function fmtTime(s) {
  if (!s) return '—';
  const str = s.toString().padStart(6, '0');
  return `${str.slice(0,2)}:${str.slice(2,4)}:${str.slice(4,6)}`;
}

function fmtAmount(yuan) {
  if (yuan == null || yuan === '') return '—';
  const v = parseFloat(yuan);
  if (isNaN(v)) return '—';
  if (v >= 1e8) return (v / 1e8).toFixed(2) + '亿';
  if (v >= 1e4) return (v / 1e4).toFixed(0) + '万';
  return v.toString();
}

function fmtPct(v) {
  if (v == null || v === '') return '—';
  const n = parseFloat(v);
  if (isNaN(n)) return '—';
  return (n >= 0 ? '+' : '') + n.toFixed(2) + '%';
}

function scoreClass(s) {
  if (s >= 70) return 'score-high';
  if (s >= 40) return 'score-mid';
  return 'score-low';
}

function consecutiveClass(n) {
  if (n >= 5) return 'c5p';
  if (n === 4) return 'c4';
  if (n === 3) return 'c3';
  if (n === 2) return 'c2';
  return 'c1';
}

// -----------------------------------------------------------------------
// Date picker setup
// -----------------------------------------------------------------------
async function initDatePicker() {
  const [latestRes, datesRes] = await Promise.all([
    fetch('/api/latest-trading-date').then(r => r.json()),
    fetch('/api/trading-dates').then(r => r.json()),
  ]);

  validDates = new Set(datesRes.dates || []);
  const latest = latestRes.date;

  const picker = document.getElementById('date-picker');
  // Convert YYYYMMDD → YYYY-MM-DD for <input type="date">
  picker.value = `${latest.slice(0,4)}-${latest.slice(4,6)}-${latest.slice(6,8)}`;
  picker.max = new Date().toISOString().split('T')[0];

  picker.addEventListener('change', () => {
    // Snap to nearest valid trading date if needed
    const picked = picker.value.replace(/-/g, '');
    if (!validDates.has(picked)) {
      document.getElementById('status-msg').textContent = '非交易日，请选择其他日期';
    } else {
      document.getElementById('status-msg').textContent = '';
    }
  });

  return latest;
}

// -----------------------------------------------------------------------
// Load main ZT data
// -----------------------------------------------------------------------
async function loadData(dateStr) {
  setLoading(true);
  strongLoaded = false;

  document.getElementById('status-msg').textContent = '';
  document.getElementById('empty-msg').classList.add('hidden');

  const [sentimentRes, ztRes] = await Promise.allSettled([
    fetch(`/api/sentiment/${dateStr}`).then(r => r.json()),
    fetch(`/api/zt/${dateStr}`).then(r => {
      if (!r.ok) return r.json().then(e => Promise.reject(e));
      return r.json();
    }),
  ]);

  setLoading(false);

  if (sentimentRes.status === 'fulfilled') {
    renderSentiment(sentimentRes.value);
  }

  if (ztRes.status === 'rejected') {
    const err = ztRes.reason;
    const detail = err?.detail;
    if (detail?.error === 'not_a_trading_date') {
      document.getElementById('status-msg').textContent =
        `非交易日。最近交易日：${fmtDate(detail.nearest)}`;
    } else {
      document.getElementById('status-msg').textContent = '数据加载失败，请稍后重试';
    }
    allRecords = [];
    renderTable();
    return;
  }

  const data = ztRes.value;
  allRecords = data.records || [];

  if (allRecords.length === 0) {
    document.getElementById('empty-msg').classList.remove('hidden');
    renderTiers([]);
    renderTable();
    return;
  }

  renderTiers(data.tiers || []);
  populateIndustryFilter();
  renderTable();
}

// -----------------------------------------------------------------------
// Sentiment panel
// -----------------------------------------------------------------------
function renderSentiment(data) {
  document.getElementById('val-total').textContent = data.total_zt ?? '—';

  if (data.promotion_rate != null) {
    const pr = data.promotion_rate;
    const el = document.getElementById('val-promotion');
    el.textContent = pr + '%';
    el.className = 'card-value ' + (pr >= 30 ? 'green' : pr >= 15 ? 'orange' : 'red');
    document.getElementById('sub-promotion').textContent =
      `前日共 ${data.prev_date ? fmtDate(data.prev_date) : '—'} 涨停`;
  } else {
    document.getElementById('val-promotion').textContent = '—';
  }

  if (data.broke_rate != null) {
    const br = data.broke_rate;
    const el = document.getElementById('val-broke');
    el.textContent = br + '%';
    el.className = 'card-value ' + (br <= 20 ? 'green' : br <= 40 ? 'orange' : 'red');
    document.getElementById('sub-broke').textContent = `炸板 ${data.broke_count} 只`;
  }
}

// -----------------------------------------------------------------------
// Tier pills
// -----------------------------------------------------------------------
function renderTiers(tiers) {
  const row = document.getElementById('tiers-row');
  row.innerHTML = '<span class="tier-pill tier-all active" data-consecutive="all">全部</span>';

  tiers.forEach(t => {
    const pill = document.createElement('span');
    pill.className = 'tier-pill';
    pill.dataset.consecutive = t.consecutive;
    pill.innerHTML = `${t.label}<span class="tier-count">${t.count}</span>`;
    row.appendChild(pill);
  });

  row.querySelectorAll('.tier-pill').forEach(pill => {
    pill.addEventListener('click', () => {
      row.querySelectorAll('.tier-pill').forEach(p => p.classList.remove('active'));
      pill.classList.add('active');
      tierFilter = pill.dataset.consecutive === 'all' ? 'all' : parseInt(pill.dataset.consecutive);
      renderTable();
    });
  });
}

// -----------------------------------------------------------------------
// Industry filter
// -----------------------------------------------------------------------
function populateIndustryFilter() {
  const sel = document.getElementById('industry-filter');
  const current = sel.value;
  const industries = [...new Set(allRecords.map(r => r.industry).filter(Boolean))].sort();
  sel.innerHTML = '<option value="">全部行业</option>';
  industries.forEach(ind => {
    const opt = document.createElement('option');
    opt.value = ind;
    opt.textContent = ind;
    if (ind === current) opt.selected = true;
    sel.appendChild(opt);
  });
}

// -----------------------------------------------------------------------
// Table render
// -----------------------------------------------------------------------
function getFilteredSorted() {
  const search = document.getElementById('search-box').value.trim().toLowerCase();
  const industry = document.getElementById('industry-filter').value;
  const hideConsec = document.getElementById('hide-consecutive').checked;

  let rows = allRecords.filter(r => {
    if (search && !(
      (r.code || '').toLowerCase().includes(search) ||
      (r.name || '').toLowerCase().includes(search) ||
      (r.industry || '').toLowerCase().includes(search)
    )) return false;
    if (industry && r.industry !== industry) return false;
    if (hideConsec && (r.consecutive || 1) >= 2) return false;
    if (tierFilter !== 'all') {
      const bucket = (r.consecutive || 1) >= 5 ? 5 : (r.consecutive || 1);
      if (bucket !== tierFilter) return false;
    }
    return true;
  });

  const { col, dir } = sortState;
  rows.sort((a, b) => {
    let va = a[col], vb = b[col];
    if (col === 'first_time') {
      va = parseInt(va || '999999');
      vb = parseInt(vb || '999999');
    } else {
      va = parseFloat(va) || 0;
      vb = parseFloat(vb) || 0;
    }
    return dir === 'asc' ? va - vb : vb - va;
  });

  return rows;
}

function renderTable() {
  const rows = getFilteredSorted();
  const tbody = document.getElementById('zt-tbody');
  document.getElementById('count-display').textContent = `共 ${rows.length} 只`;

  if (rows.length === 0) {
    tbody.innerHTML = '';
    return;
  }

  const frag = document.createDocumentFragment();
  rows.forEach(r => {
    const tr = document.createElement('tr');
    const consec = r.consecutive || 1;
    if (consec >= 2) tr.classList.add('row-consecutive');

    const firstTime = r.first_time || '';
    const isEarly = firstTime && parseInt(firstTime) < 93100;
    const timeHtml = `<span class="${isEarly ? 'time-early' : ''}">${fmtTime(firstTime)}</span>`;

    const breakCount = r.break_count || 0;
    const breakHtml = breakCount > 0
      ? `<span class="break-warn">${breakCount}</span>`
      : `<span style="color:var(--text-secondary)">${breakCount}</span>`;

    const sc = r.score ?? 0;
    const scoreBadge = `<span class="score-badge ${scoreClass(sc)}">${sc}</span>`;

    const cClass = consecutiveClass(consec);
    const consecBadge = `<span class="consecutive-badge ${cClass}">${consec}</span>`;

    const market = r.code && r.code.startsWith('6') ? 'sh' : 'sz';
    const codeLink = r.code
      ? `<a class="stock-link" href="https://quote.eastmoney.com/${market}${r.code}.html" target="_blank">${r.code}</a>`
      : '—';

    function histCell(count, window) {
      if (count == null) return '<td class="hist-val">—</td>';
      return `<td class="hist-val ${count > 0 ? 'nonzero' : ''}">${count}/${window}</td>`;
    }

    tr.innerHTML = `
      <td>${scoreBadge}</td>
      <td>${codeLink}</td>
      <td>${r.name || '—'}</td>
      <td>${timeHtml}</td>
      <td>${fmtAmount(r.volume)}</td>
      <td>${fmtAmount(r.seal_fund)}</td>
      <td>${breakHtml}</td>
      <td>${consecBadge}</td>
      <td>${r.industry || '—'}</td>
      <td style="color:var(--text-secondary)">${r.zt_stat || '—'}</td>
      ${histCell(r.week1_count, 5)}
      ${histCell(r.week2_count, 10)}
      ${histCell(r.week3_count, 15)}
      ${histCell(r.month1_count, 20)}
    `;
    frag.appendChild(tr);
  });

  tbody.innerHTML = '';
  tbody.appendChild(frag);
}

// -----------------------------------------------------------------------
// Sorting
// -----------------------------------------------------------------------
function setupSorting() {
  document.querySelectorAll('#zt-table th.sortable').forEach(th => {
    th.addEventListener('click', () => {
      const col = th.dataset.col;
      if (sortState.col === col) {
        sortState.dir = sortState.dir === 'asc' ? 'desc' : 'asc';
      } else {
        sortState.col = col;
        sortState.dir = ['score', 'volume', 'seal_fund', 'consecutive', 'week1_count', 'week2_count', 'week3_count', 'month1_count'].includes(col)
          ? 'desc' : 'asc';
      }
      updateSortHeaders();
      renderTable();
    });
  });
  updateSortHeaders();
}

function updateSortHeaders() {
  document.querySelectorAll('#zt-table th').forEach(th => {
    th.classList.remove('sort-asc', 'sort-desc');
    if (th.dataset.col === sortState.col) {
      th.classList.add(sortState.dir === 'asc' ? 'sort-asc' : 'sort-desc');
    }
  });
}

// -----------------------------------------------------------------------
// Filter events
// -----------------------------------------------------------------------
function setupFilters() {
  document.getElementById('search-box').addEventListener('input', renderTable);
  document.getElementById('industry-filter').addEventListener('change', renderTable);
  document.getElementById('hide-consecutive').addEventListener('change', renderTable);
  document.getElementById('load-btn').addEventListener('click', () => {
    const picked = document.getElementById('date-picker').value.replace(/-/g, '');
    if (picked) loadData(picked);
  });
  document.getElementById('date-picker').addEventListener('keydown', e => {
    if (e.key === 'Enter') document.getElementById('load-btn').click();
  });
}

// -----------------------------------------------------------------------
// Strong stocks section
// -----------------------------------------------------------------------
function setupStrongSection() {
  const toggle = document.getElementById('strong-toggle');
  const content = document.getElementById('strong-content');
  const arrow = document.getElementById('strong-arrow');

  toggle.addEventListener('click', async () => {
    const isHidden = content.classList.contains('hidden');
    content.classList.toggle('hidden', !isHidden);
    arrow.classList.toggle('open', isHidden);

    if (isHidden && !strongLoaded) {
      await loadStrongData();
    }
  });
}

async function loadStrongData() {
  const picker = document.getElementById('date-picker');
  const dateStr = picker.value.replace(/-/g, '');
  if (!dateStr) return;

  const loading = document.getElementById('strong-loading');
  const table = document.getElementById('strong-table');
  const empty = document.getElementById('strong-empty');

  loading.classList.remove('hidden');
  table.classList.add('hidden');
  empty.classList.add('hidden');

  try {
    const data = await fetch(`/api/strong/${dateStr}`).then(r => r.json());
    strongLoaded = true;
    loading.classList.add('hidden');

    if (!data.records || data.records.length === 0) {
      empty.classList.remove('hidden');
      return;
    }

    const tbody = document.getElementById('strong-tbody');
    tbody.innerHTML = '';
    data.records.forEach(r => {
      const pct = parseFloat(r.change_pct);
      const pctColor = pct >= 0 ? 'var(--red)' : 'var(--green)';
      const tr = document.createElement('tr');
      tr.innerHTML = `
        <td><strong>${r.code || '—'}</strong></td>
        <td>${r.name || '—'}</td>
        <td style="color:${pctColor};font-weight:600">${fmtPct(r.change_pct)}</td>
        <td>${r.price ?? '—'}</td>
        <td>${r.industry || '—'}</td>
        <td style="color:var(--text-secondary)">${r.zt_stat || '—'}</td>
      `;
      tbody.appendChild(tr);
    });
    table.classList.remove('hidden');
  } catch (e) {
    loading.classList.add('hidden');
    empty.textContent = '数据加载失败';
    empty.classList.remove('hidden');
  }
}

// -----------------------------------------------------------------------
// Helpers
// -----------------------------------------------------------------------
function setLoading(on) {
  document.getElementById('loading-overlay').classList.toggle('hidden', !on);
}

function fmtDate(d) {
  if (!d || d.length !== 8) return d;
  return `${d.slice(0,4)}-${d.slice(4,6)}-${d.slice(6,8)}`;
}

// -----------------------------------------------------------------------
// Bootstrap
// -----------------------------------------------------------------------
document.addEventListener('DOMContentLoaded', async () => {
  setupSorting();
  setupFilters();
  setupStrongSection();

  try {
    const latest = await initDatePicker();
    await loadData(latest);
  } catch (e) {
    document.getElementById('status-msg').textContent = '初始化失败：' + e.message;
    setLoading(false);
  }
});
