// ─────────────────────────────────────────────────────────────────
// STATE
// ─────────────────────────────────────────────────────────────────
let allRecords   = [];
let validDates   = new Set();
let currentDate  = '';
let sortState    = { col: 'score', dir: 'desc' };
let tierFilter   = 'all';
let auctionOnly  = false;
let capFilter    = 'all';          // Feature 4: market cap tier
let leaderCodes  = new Set();      // Feature 6: leader detection
let backtestCross = null;          // cross-dim lookup from rolling backtest
let trendData    = {};             // code → {trend_score, trend_label}
let trendFilter  = 'all';         // 'all' | '上升' | '震荡' | '下降'
let strongLoaded = false;
let analysisLoadedFor = '';

let trendChartInst  = null;
let sectorChartInst = null;
let klineChart      = null;        // Feature 7: lightweight-charts instance
let wencaiLoaded    = false;       // track whether wencai tab has been fetched

// ─────────────────────────────────────────────────────────────────
// FORMATTERS
// ─────────────────────────────────────────────────────────────────
function fmtTime(s) {
  if (!s) return '—';
  const str = s.toString().padStart(6, '0');
  return `${str.slice(0,2)}:${str.slice(2,4)}:${str.slice(4,6)}`;
}
function fmtDate(d) {
  if (!d || d.length !== 8) return d || '';
  return `${d.slice(0,4)}-${d.slice(4,6)}-${d.slice(6,8)}`;
}
function fmtDateShort(d) {
  if (!d || d.length !== 8) return d || '';
  return `${d.slice(4,6)}/${d.slice(6,8)}`;
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
  if (v == null) return '—';
  const n = parseFloat(v);
  if (isNaN(n)) return '—';
  return (n >= 0 ? '+' : '') + n.toFixed(2) + '%';
}
function scoreClass(s) {
  if (s >= 70) return 'score-high';
  if (s >= 40) return 'score-mid';
  return 'score-low';
}
function consecClass(n) {
  if (n >= 5) return 'c5p';
  if (n === 4) return 'c4';
  if (n === 3) return 'c3';
  if (n === 2) return 'c2';
  return 'c1';
}
function isAuction(first_time) {
  return !!first_time && parseInt(first_time) <= 92559;
}

// Feature 4: market cap tier
function capTier(circ_cap) {
  if (!circ_cap) return 'unknown';
  const v = parseFloat(circ_cap);
  if (isNaN(v)) return 'unknown';
  if (v < 5e9)  return 'small';
  if (v < 2e10) return 'mid';
  return 'large';
}

// Bucket helpers matching backend _seal_bucket / _consec_bucket
function sealBucket(ft) {
  if (!ft) return '盘中';
  const t = parseInt(ft);
  if (isNaN(t)) return '盘中';
  if (t < 93100)  return '竞价';
  if (t < 100000) return '早盘';
  return '盘中';
}
function consecBucket(c) {
  const n = parseInt(c) || 1;
  if (n === 1) return '首板';
  if (n === 2) return '二板';
  return '三板+';
}

// ─────────────────────────────────────────────────────────────────
// FEATURE 6: LEADER DETECTION
// ─────────────────────────────────────────────────────────────────
function computeLeaders(records) {
  const byIndustry = {};
  records.forEach(r => {
    if (!r.industry) return;
    (byIndustry[r.industry] = byIndustry[r.industry] || []).push(r);
  });

  const leaders = new Set();
  Object.values(byIndustry).forEach(stocks => {
    if (stocks.length < 2) return;   // need 2+ in sector to crown a leader
    const sorted = [...stocks].sort((a, b) => {
      // Primary: most consecutive boards
      const dc = (b.consecutive || 1) - (a.consecutive || 1);
      if (dc !== 0) return dc;
      // Secondary: earliest seal (smaller number = earlier)
      const ta = parseInt(a.first_time || '999999');
      const tb = parseInt(b.first_time || '999999');
      if (ta !== tb) return ta - tb;
      // Tertiary: higher score
      return (b.score || 0) - (a.score || 0);
    });
    leaders.add(sorted[0].code);
  });
  return leaders;
}

// ─────────────────────────────────────────────────────────────────
// WATCHLIST (localStorage)
// ─────────────────────────────────────────────────────────────────
function getWatchlist() {
  try { return JSON.parse(localStorage.getItem('zt_watchlist') || '{}'); }
  catch { return {}; }
}
function isWatched(code) { return code in getWatchlist(); }
function toggleWatchlist(code, name) {
  const wl = getWatchlist();
  if (wl[code]) delete wl[code]; else wl[code] = name;
  localStorage.setItem('zt_watchlist', JSON.stringify(wl));
  updateWatchlistBadge();
  renderTable();
  if (document.getElementById('tab-watchlist').classList.contains('active'))
    renderWatchlistTab();
}
function updateWatchlistBadge() {
  const count = Object.keys(getWatchlist()).length;
  const badge = document.getElementById('watchlist-badge');
  badge.textContent = count > 0 ? count : '';
  badge.style.display = count > 0 ? 'inline-block' : 'none';
}

// ─────────────────────────────────────────────────────────────────
// TABS
// ─────────────────────────────────────────────────────────────────
function setupTabs() {
  document.querySelectorAll('.tab-btn').forEach(btn => {
    btn.addEventListener('click', () => {
      document.querySelectorAll('.tab-btn').forEach(b => b.classList.remove('active'));
      document.querySelectorAll('.tab-pane').forEach(p => p.classList.remove('active'));
      btn.classList.add('active');
      document.getElementById(`tab-${btn.dataset.tab}`).classList.add('active');
      if (btn.dataset.tab === 'analysis') maybeLoadAnalysis();
      if (btn.dataset.tab === 'watchlist') renderWatchlistTab();
    });
  });
}

// ─────────────────────────────────────────────────────────────────
// DATE PICKER
// ─────────────────────────────────────────────────────────────────
async function initDatePicker() {
  const [latestRes, datesRes] = await Promise.all([
    fetch('/api/latest-trading-date').then(r => r.json()),
    fetch('/api/trading-dates').then(r => r.json()),
  ]);
  validDates = new Set(datesRes.dates || []);
  const latest = latestRes.date;
  const picker = document.getElementById('date-picker');
  picker.value = `${latest.slice(0,4)}-${latest.slice(4,6)}-${latest.slice(6,8)}`;
  picker.max = new Date().toISOString().split('T')[0];
  picker.addEventListener('change', () => {
    const picked = picker.value.replace(/-/g, '');
    document.getElementById('status-msg').textContent =
      (!validDates.has(picked)) ? '非交易日，请选择其他日期' : '';
  });
  return latest;
}

// ─────────────────────────────────────────────────────────────────
// MAIN DATA LOAD
// ─────────────────────────────────────────────────────────────────
async function loadData(dateStr) {
  currentDate = dateStr;
  strongLoaded = false;
  analysisLoadedFor = '';
  backtestCross = null;
  trendData = {};

  setLoading(true);
  document.getElementById('status-msg').textContent = '';
  document.getElementById('empty-msg').classList.add('hidden');

  const [sentRes, ztRes] = await Promise.allSettled([
    fetch(`/api/sentiment/${dateStr}`).then(r => r.json()),
    fetch(`/api/zt/${dateStr}`).then(r => {
      if (!r.ok) return r.json().then(e => Promise.reject(e));
      return r.json();
    }),
  ]);

  setLoading(false);

  if (sentRes.status === 'fulfilled') renderSentiment(sentRes.value);

  if (ztRes.status === 'rejected') {
    const detail = ztRes.reason?.detail;
    document.getElementById('status-msg').textContent =
      detail?.error === 'not_a_trading_date'
        ? `非交易日，最近交易日：${fmtDate(detail.nearest)}`
        : '数据加载失败，请稍后重试';
    allRecords = []; leaderCodes = new Set();
    renderTiers([]); renderTable();
    return;
  }

  const data = ztRes.value;
  allRecords = data.records || [];
  leaderCodes = computeLeaders(allRecords);  // Feature 6

  if (!allRecords.length) {
    document.getElementById('empty-msg').classList.remove('hidden');
    renderTiers([]); renderTable();
    return;
  }

  renderTiers(data.tiers || []);
  populateIndustryFilter();
  renderTable();
  loadTrendData(dateStr);  // background, non-blocking

  if (document.querySelector('.tab-btn[data-tab="analysis"]').classList.contains('active'))
    maybeLoadAnalysis();
}

// ─────────────────────────────────────────────────────────────────
// SENTIMENT CARDS
// ─────────────────────────────────────────────────────────────────
function renderSentiment(data) {
  document.getElementById('val-total').textContent = data.total_zt ?? '—';
  if (data.promotion_rate != null) {
    const pr = data.promotion_rate;
    const el = document.getElementById('val-promotion');
    el.textContent = pr + '%';
    el.className = 'card-value ' + (pr >= 30 ? 'green' : pr >= 15 ? 'orange' : 'red');
    document.getElementById('sub-promotion').textContent =
      data.prev_date ? `前日：${fmtDate(data.prev_date)}` : '';
  }
  if (data.broke_rate != null) {
    const br = data.broke_rate;
    const el = document.getElementById('val-broke');
    el.textContent = br + '%';
    el.className = 'card-value ' + (br <= 20 ? 'green' : br <= 40 ? 'orange' : 'red');
    document.getElementById('sub-broke').textContent = `炸板 ${data.broke_count} 只`;
  }
}

// ─────────────────────────────────────────────────────────────────
// TIER PILLS
// ─────────────────────────────────────────────────────────────────
function renderTiers(tiers) {
  const row = document.getElementById('tiers-row');
  row.innerHTML = '<span class="tier-pill active" data-consecutive="all">全部</span>';
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

// ─────────────────────────────────────────────────────────────────
// INDUSTRY FILTER
// ─────────────────────────────────────────────────────────────────
function populateIndustryFilter() {
  const sel = document.getElementById('industry-filter');
  const cur = sel.value;
  const inds = [...new Set(allRecords.map(r => r.industry).filter(Boolean))].sort();
  sel.innerHTML = '<option value="">全部行业</option>';
  inds.forEach(ind => {
    const opt = document.createElement('option');
    opt.value = ind; opt.textContent = ind;
    if (ind === cur) opt.selected = true;
    sel.appendChild(opt);
  });
}

// ─────────────────────────────────────────────────────────────────
// MAIN TABLE
// ─────────────────────────────────────────────────────────────────
function getFilteredSorted() {
  const search   = document.getElementById('search-box').value.trim().toLowerCase();
  const industry = document.getElementById('industry-filter').value;
  const hideC    = document.getElementById('hide-consecutive').checked;

  let rows = allRecords.filter(r => {
    if (search && !((r.code||'').toLowerCase().includes(search) ||
                    (r.name||'').toLowerCase().includes(search) ||
                    (r.industry||'').toLowerCase().includes(search))) return false;
    if (industry && r.industry !== industry) return false;
    if (hideC && (r.consecutive||1) >= 2) return false;
    if (auctionOnly && !isAuction(r.first_time)) return false;
    // Feature 4: market cap filter
    if (capFilter !== 'all' && capTier(r.circ_cap) !== capFilter) return false;
    if (trendFilter !== 'all') {
      const td = trendData[r.code];
      if (!td || td.trend_label !== trendFilter) return false;
    }
    if (tierFilter !== 'all') {
      const bucket = (r.consecutive||1) >= 5 ? 5 : (r.consecutive||1);
      if (bucket !== tierFilter) return false;
    }
    return true;
  });

  const { col, dir } = sortState;
  rows.sort((a, b) => {
    let va, vb;
    if (col === 'first_time') {
      va = parseInt(a[col]||'999999'); vb = parseInt(b[col]||'999999');
    } else if (col === 'trend_score') {
      va = (trendData[a.code]?.trend_score ?? -1);
      vb = (trendData[b.code]?.trend_score ?? -1);
    } else {
      va = parseFloat(a[col]) || 0; vb = parseFloat(b[col]) || 0;
    }
    return dir === 'asc' ? va - vb : vb - va;
  });
  return rows;
}

function renderTable() {
  const rows = getFilteredSorted();
  const tbody = document.getElementById('zt-tbody');
  document.getElementById('count-display').textContent = `共 ${rows.length} 只`;
  if (!rows.length) { tbody.innerHTML = ''; return; }

  const frag = document.createDocumentFragment();
  rows.forEach(r => {
    const tr = document.createElement('tr');
    const consec = r.consecutive || 1;
    if (consec >= 2) tr.classList.add('row-consecutive');

    const isEarly = isAuction(r.first_time);
    const timeHtml = isEarly
      ? `<span class="time-early">${fmtTime(r.first_time)}</span><span class="badge-auction">竞价</span>`
      : fmtTime(r.first_time);

    const bc = r.break_count || 0;
    const breakHtml = bc > 0
      ? `<span class="break-warn">${bc}</span>`
      : `<span style="color:var(--text-secondary)">0</span>`;

    const sc = r.score ?? 0;
    const market = (r.code||'').startsWith('6') ? 'sh' : 'sz';
    const codeLink = r.code
      ? `<a class="stock-link" href="https://quote.eastmoney.com/${market}${r.code}.html"
            target="_blank" onclick="event.stopPropagation()">${r.code}</a>`
      : '—';

    // Feature 6: leader badge
    const isLeader = leaderCodes.has(r.code);
    const industryHtml = r.industry
      ? `${r.industry}${isLeader ? '<span class="leader-badge">龙头</span>' : ''}`
      : '—';

    const watched = isWatched(r.code);

    function histCell(count, w) {
      if (count == null) return '<td class="hist-val">—</td>';
      return `<td class="hist-val ${count > 0 ? 'nonzero' : ''}">${count}/${w}</td>`;
    }

    tr.innerHTML = `
      <td><button class="star-btn ${watched?'starred':''}"
            data-code="${r.code}" data-name="${r.name||''}">
            ${watched?'★':'☆'}</button></td>
      <td>${(() => {
        let pred = '';
        if (backtestCross) {
          const k = `${sealBucket(r.first_time)}|${consecBucket(r.consecutive)}`;
          const p = backtestCross[k];
          if (p) {
            if (p.t2_win_rate != null && p.t2_n >= 5) {
              const pc = p.t2_win_rate >= 60 ? 'pred-high' : p.t2_win_rate >= 40 ? 'pred-mid' : 'pred-low';
              pred = `<div class="pred-rate ${pc}">T2胜${p.t2_win_rate}%</div>`;
            } else if (p.n >= 5) {
              const pc = p.t1_zt_rate >= 50 ? 'pred-high' : p.t1_zt_rate >= 30 ? 'pred-mid' : 'pred-low';
              pred = `<div class="pred-rate ${pc}">↑${p.t1_zt_rate}%</div>`;
            }
          }
        }
        return `<span class="score-badge ${scoreClass(sc)}">${sc}</span>${pred}`;
      })()}</td>
      <td>${codeLink}</td>
      <td>${r.name||'—'}</td>
      <td>${timeHtml}</td>
      <td>${fmtAmount(r.volume)}</td>
      <td>${fmtAmount(r.seal_fund)}</td>
      <td>${r.turnover != null ? r.turnover.toFixed(2)+'%' : '—'}</td>
      <td>${breakHtml}</td>
      <td><span class="consecutive-badge ${consecClass(consec)}">${consec}</span></td>
      <td>${industryHtml}</td>
      <td style="color:var(--text-secondary);font-size:12px">${r.zt_stat||'—'}</td>
      ${histCell(r.week1_count,5)}
      ${histCell(r.week2_count,10)}
      ${histCell(r.week3_count,15)}
      ${histCell(r.month1_count,20)}
      <td>${(() => {
        const td = trendData[r.code];
        if (!td) return '<span class="trend-badge trend-pending">…</span>';
        const cls = td.trend_label === '上升' ? 'trend-up' : td.trend_label === '下降' ? 'trend-down' : 'trend-neutral';
        return `<span class="trend-badge ${cls}">${td.trend_label} ${td.trend_score}</span>`;
      })()}</td>
    `;

    tr.addEventListener('click', e => {
      if (e.target.closest('a') || e.target.closest('.star-btn')) return;
      openStockModal(r.code, r.name);
    });
    frag.appendChild(tr);
  });

  tbody.innerHTML = '';
  tbody.appendChild(frag);

  tbody.querySelectorAll('.star-btn').forEach(btn => {
    btn.addEventListener('click', e => {
      e.stopPropagation();
      toggleWatchlist(btn.dataset.code, btn.dataset.name);
    });
  });
}

// ─────────────────────────────────────────────────────────────────
// SORTING
// ─────────────────────────────────────────────────────────────────
function setupSorting() {
  document.querySelectorAll('#zt-table th.sortable').forEach(th => {
    th.addEventListener('click', () => {
      const col = th.dataset.col;
      if (sortState.col === col) {
        sortState.dir = sortState.dir === 'asc' ? 'desc' : 'asc';
      } else {
        sortState.col = col;
        sortState.dir = ['score','volume','seal_fund','turnover','consecutive',
                         'week1_count','week2_count','week3_count','month1_count','trend_score']
          .includes(col) ? 'desc' : 'asc';
      }
      updateSortHeaders(); renderTable();
    });
  });
  updateSortHeaders();
}
function updateSortHeaders() {
  document.querySelectorAll('#zt-table th').forEach(th => {
    th.classList.remove('sort-asc','sort-desc');
    if (th.dataset.col === sortState.col)
      th.classList.add(sortState.dir === 'asc' ? 'sort-asc' : 'sort-desc');
  });
}

// ─────────────────────────────────────────────────────────────────
// FILTERS SETUP
// ─────────────────────────────────────────────────────────────────
function setupFilters() {
  document.getElementById('search-box').addEventListener('input', renderTable);
  document.getElementById('industry-filter').addEventListener('change', renderTable);
  document.getElementById('hide-consecutive').addEventListener('change', renderTable);

  // Auction filter
  const auctionBtn = document.getElementById('auction-filter');
  auctionBtn.addEventListener('click', () => {
    auctionOnly = !auctionOnly;
    auctionBtn.classList.toggle('active', auctionOnly);
    renderTable();
  });

  // Feature 4: market cap filter pills
  document.querySelectorAll('.cap-btn').forEach(btn => {
    btn.addEventListener('click', () => {
      document.querySelectorAll('.cap-btn').forEach(b => b.classList.remove('active'));
      btn.classList.add('active');
      capFilter = btn.dataset.cap;
      renderTable();
    });
  });

  // Trend filter pills
  document.querySelectorAll('.trend-btn').forEach(btn => {
    btn.addEventListener('click', () => {
      document.querySelectorAll('.trend-btn').forEach(b => b.classList.remove('active'));
      btn.classList.add('active');
      trendFilter = btn.dataset.trend;
      renderTable();
    });
  });

  document.getElementById('load-btn').addEventListener('click', () => {
    const picked = document.getElementById('date-picker').value.replace(/-/g, '');
    if (picked) loadData(picked);
  });
  document.getElementById('date-picker').addEventListener('keydown', e => {
    if (e.key === 'Enter') document.getElementById('load-btn').click();
  });
}

// ─────────────────────────────────────────────────────────────────
// TREND DATA (lazy, background load)
// ─────────────────────────────────────────────────────────────────
async function loadTrendData(dateStr) {
  const msg = document.getElementById('trend-loading-msg');
  msg.classList.remove('hidden');
  try {
    const data = await fetch(`/api/zt-trend/${dateStr}`).then(r => r.json());
    trendData = {};
    (data.records || []).forEach(r => { trendData[r.code] = r; });
    renderTable();
  } catch (e) {
    // silently fail — trend column shows "…" until data arrives
  } finally {
    msg.classList.add('hidden');
  }
}

// ─────────────────────────────────────────────────────────────────
// STRONG STOCKS
// ─────────────────────────────────────────────────────────────────
function setupStrongSection() {
  document.getElementById('strong-toggle').addEventListener('click', async () => {
    const content = document.getElementById('strong-content');
    const arrow   = document.getElementById('strong-arrow');
    const isHidden = content.classList.contains('hidden');
    content.classList.toggle('hidden', !isHidden);
    arrow.classList.toggle('open', isHidden);
    if (isHidden && !strongLoaded) await loadStrongData();
  });
}
async function loadStrongData() {
  if (!currentDate) return;
  const loading = document.getElementById('strong-loading');
  const table   = document.getElementById('strong-table');
  const empty   = document.getElementById('strong-empty');
  loading.classList.remove('hidden'); table.classList.add('hidden'); empty.classList.add('hidden');
  try {
    const data = await fetch(`/api/strong/${currentDate}`).then(r => r.json());
    strongLoaded = true;
    loading.classList.add('hidden');
    if (!data.records?.length) { empty.classList.remove('hidden'); return; }
    const tbody = document.getElementById('strong-tbody');
    tbody.innerHTML = '';
    data.records.forEach(r => {
      const pct = parseFloat(r.change_pct);
      const tr = document.createElement('tr');
      tr.innerHTML = `
        <td><strong>${r.code||'—'}</strong></td>
        <td>${r.name||'—'}</td>
        <td style="color:${pct>=0?'var(--red)':'var(--green)'};font-weight:600">${fmtPct(r.change_pct)}</td>
        <td>${r.price??'—'}</td>
        <td>${r.industry||'—'}</td>
        <td style="color:var(--text-secondary);font-size:12px">${r.zt_stat||'—'}</td>
      `;
      tbody.appendChild(tr);
    });
    table.classList.remove('hidden');
  } catch {
    loading.classList.add('hidden');
    empty.textContent = '数据加载失败'; empty.classList.remove('hidden');
  }
}

// ─────────────────────────────────────────────────────────────────
// ANALYSIS TAB
// ─────────────────────────────────────────────────────────────────
async function maybeLoadAnalysis() {
  if (analysisLoadedFor === currentDate) return;
  analysisLoadedFor = currentDate;

  renderSectorChart();   // client-side, no API call

  const [trendRes, nextdayRes, tierPromoRes] = await Promise.allSettled([
    fetch(`/api/market-trend/${currentDate}`).then(r => r.json()),
    fetch(`/api/nextday-stats/${currentDate}`).then(r => r.json()),
    fetch(`/api/tier-promotion/${currentDate}`).then(r => r.json()),
  ]);

  if (trendRes.status === 'fulfilled')     renderTrendChart(trendRes.value.trend);
  if (nextdayRes.status === 'fulfilled')   renderNextdayStats(nextdayRes.value);
  if (tierPromoRes.status === 'fulfilled') renderTierPromotion(tierPromoRes.value);

  loadBacktest();   // loads independently; window selector can re-trigger
}

// Chart: 市场温度
function renderTrendChart(trend) {
  document.getElementById('trend-loading').classList.add('hidden');
  const labels = trend.map(d => fmtDateShort(d.date));
  if (trendChartInst) trendChartInst.destroy();
  trendChartInst = new Chart(document.getElementById('trend-chart'), {
    data: {
      labels,
      datasets: [
        { type:'bar',  label:'涨停数量', data: trend.map(d=>d.total_zt),
          backgroundColor:'rgba(220,38,38,.15)', borderColor:'rgba(220,38,38,.6)',
          borderWidth:1, yAxisID:'y' },
        { type:'line', label:'晋级率%', data: trend.map(d=>d.promotion_rate),
          borderColor:'#16a34a', backgroundColor:'transparent',
          borderWidth:2, pointRadius:3, tension:0.3, yAxisID:'y2', spanGaps:true },
        { type:'line', label:'炸板率%', data: trend.map(d=>d.broke_rate),
          borderColor:'#ea580c', backgroundColor:'transparent',
          borderWidth:2, pointRadius:3, tension:0.3, yAxisID:'y2' },
      ],
    },
    options: {
      responsive:true, maintainAspectRatio:false,
      interaction:{ mode:'index', intersect:false },
      plugins:{ legend:{ labels:{ font:{size:12} } } },
      scales:{
        x:{ ticks:{ font:{size:11} } },
        y:{ position:'left',  title:{ display:true, text:'涨停数', font:{size:11} } },
        y2:{ position:'right', title:{ display:true, text:'%', font:{size:11} },
             grid:{ drawOnChartArea:false }, min:0, max:100 },
      },
    },
  });
}

// Chart: 板块热度
function renderSectorChart() {
  if (!allRecords.length) {
    document.getElementById('sector-empty').classList.remove('hidden'); return;
  }
  document.getElementById('sector-empty').classList.add('hidden');
  const counts = {};
  allRecords.forEach(r => { if (r.industry) counts[r.industry] = (counts[r.industry]||0)+1; });
  const sorted = Object.entries(counts).sort((a,b)=>b[1]-a[1]).slice(0,15);
  if (sectorChartInst) sectorChartInst.destroy();
  sectorChartInst = new Chart(document.getElementById('sector-chart'), {
    type:'bar',
    data:{
      labels: sorted.map(([ind])=>ind),
      datasets:[{ label:'涨停只数', data: sorted.map(([,n])=>n),
        backgroundColor: sorted.map((_,i) =>
          i===0?'rgba(220,38,38,.75)': i<=2?'rgba(234,88,12,.6)':'rgba(37,99,235,.3)'),
        borderWidth:0, borderRadius:3 }],
    },
    options:{
      indexAxis:'y', responsive:true, maintainAspectRatio:false,
      plugins:{ legend:{ display:false } },
      scales:{ x:{ ticks:{ stepSize:1 } }, y:{ ticks:{ font:{size:12} } } },
    },
  });
}

// 次日再涨停率
function renderNextdayStats(data) {
  document.getElementById('nextday-loading').classList.add('hidden');
  if (!data.available) { document.getElementById('nextday-na').classList.remove('hidden'); return; }
  const grid = document.getElementById('nextday-stats');
  grid.innerHTML = '';
  data.tiers.forEach(t => {
    const rc = t.rate>=40?'high': t.rate>=20?'mid':'low';
    const div = document.createElement('div');
    div.className = 'nextday-tier';
    div.innerHTML = `<div class="nextday-tier-label">${t.label} 次日再涨停</div>
      <div class="nextday-rate ${rc}">${t.rate}%</div>
      <div class="nextday-detail">${t.hit_next_zt} / ${t.total} 只</div>`;
    grid.appendChild(div);
  });
  grid.classList.remove('hidden');
}

// Feature 5: 连板晋级率
function renderTierPromotion(data) {
  document.getElementById('tier-promo-loading').classList.add('hidden');
  const grid = document.getElementById('tier-promo-grid');
  if (!data.stats || !data.stats.length) {
    grid.innerHTML = '<div style="color:var(--text-secondary)">暂无足够历史数据</div>';
    grid.classList.remove('hidden');
    return;
  }
  grid.innerHTML = '';
  data.stats.forEach(t => {
    const rc = t.rate>=40?'high': t.rate>=20?'mid':'low';
    const div = document.createElement('div');
    div.className = 'nextday-tier';
    div.innerHTML = `<div class="nextday-tier-label">${t.label}</div>
      <div class="nextday-rate ${rc}">${t.rate}%</div>
      <div class="nextday-detail">${t.advanced} / ${t.attempts} 次（${data.days}日）</div>`;
    grid.appendChild(div);
  });
  grid.classList.remove('hidden');
}

// ─────────────────────────────────────────────────────────────────
// FEATURE 7: STOCK MODAL WITH K-LINE + TIMELINE
// ─────────────────────────────────────────────────────────────────
function setupModal() {
  document.getElementById('modal-close').addEventListener('click', closeModal);
  document.getElementById('stock-modal').addEventListener('click', e => {
    if (e.target === document.getElementById('stock-modal')) closeModal();
  });
  document.addEventListener('keydown', e => { if (e.key === 'Escape') closeModal(); });

  // Modal tab switching
  document.querySelectorAll('.modal-tab-btn').forEach(btn => {
    btn.addEventListener('click', () => {
      document.querySelectorAll('.modal-tab-btn').forEach(b => b.classList.remove('active'));
      document.querySelectorAll('.modal-tab-pane').forEach(p => p.classList.add('hidden'));
      btn.classList.add('active');
      document.getElementById(`modal-tab-${btn.dataset.modalTab}`).classList.remove('hidden');
      // Expand modal for wencai tab (needs desktop-width viewport), shrink back otherwise
      document.querySelector('.modal-box').classList.toggle('wencai-expanded', btn.dataset.modalTab === 'wencai');
      if (btn.dataset.modalTab === 'wencai' && !wencaiLoaded) {
        const code = document.getElementById('modal-code').textContent;
        const name = document.getElementById('modal-name').textContent;
        loadWencai(code, name);
      }
    });
  });
}

function closeModal() {
  document.getElementById('stock-modal').classList.add('hidden');
  document.querySelector('.modal-box').classList.remove('wencai-expanded');
  if (klineChart) { klineChart.remove(); klineChart = null; }
}

async function openStockModal(code, name) {
  // Reset modal state
  document.getElementById('modal-name').textContent = name || code;
  document.getElementById('modal-code').textContent = code;
  document.getElementById('stock-modal').classList.remove('hidden');

  // Activate K-line tab by default
  document.querySelectorAll('.modal-tab-btn').forEach(b => b.classList.remove('active'));
  document.querySelectorAll('.modal-tab-pane').forEach(p => p.classList.add('hidden'));
  document.querySelector('.modal-tab-btn[data-modal-tab="kline"]').classList.add('active');
  document.getElementById('modal-tab-kline').classList.remove('hidden');

  // Reset K-line container
  if (klineChart) { klineChart.remove(); klineChart = null; }
  document.getElementById('kline-container').innerHTML = '';
  document.getElementById('kline-loading').classList.remove('hidden');
  document.getElementById('kline-loading').textContent = '加载中…';

  // Reset timeline
  document.getElementById('timeline-loading').classList.remove('hidden');
  document.getElementById('timeline-grid').innerHTML = '';
  document.getElementById('timeline-stats').innerHTML = '';

  // Reset wencai
  wencaiLoaded = false;
  document.getElementById('wencai-loading').classList.remove('hidden');
  document.getElementById('wencai-iframe').src = 'about:blank';
  document.getElementById('wencai-iframe').classList.add('hidden');
  document.getElementById('wencai-error').classList.add('hidden');

  // Load both in parallel
  loadKlineChart(code);
  loadTimeline(code);
}

async function loadKlineChart(code) {
  const container = document.getElementById('kline-container');
  try {
    const data = await fetch(`/api/kline/${code}/${currentDate}`).then(r => r.json());
    document.getElementById('kline-loading').classList.add('hidden');

    if (!data.kline?.length) {
      const reason = data.error ? `：${data.error}` : '';
      container.innerHTML = `<div style="padding:20px;color:var(--text-secondary)">K线数据暂无${reason}</div>`;
      return;
    }

    // Format for lightweight-charts: time must be "YYYY-MM-DD"
    const fmt = d => `${d.date.slice(0,4)}-${d.date.slice(4,6)}-${d.date.slice(6,8)}`;

    const candles = data.kline.map(d => ({
      time: fmt(d), open: d.open, high: d.high, low: d.low, close: d.close,
    }));

    // Mark limit-up days (pct ≥ 9.8%)
    const ztTimes = new Set(
      data.kline.filter(d => d.pct >= 9.8).map(d => fmt(d))
    );

    klineChart = LightweightCharts.createChart(container, {
      width:  container.clientWidth,
      height: 380,
      layout: { background: { color: '#ffffff' }, textColor: '#334155' },
      grid:   { vertLines: { color: '#f1f5f9' }, horzLines: { color: '#f1f5f9' } },
      crosshair: { mode: LightweightCharts.CrosshairMode.Normal },
      rightPriceScale: { borderColor: '#e2e8f0' },
      timeScale: { borderColor: '#e2e8f0', timeVisible: true },
    });

    // Price scale: leave bottom 22% for volume
    klineChart.priceScale('right').applyOptions({
      scaleMargins: { top: 0.05, bottom: 0.22 },
    });

    // A股: 涨=红 跌=绿 (Chinese convention)
    const candleSeries = klineChart.addCandlestickSeries({
      upColor:        '#dc2626', downColor:        '#16a34a',
      borderUpColor:  '#dc2626', borderDownColor:  '#16a34a',
      wickUpColor:    '#dc2626', wickDownColor:    '#16a34a',
    });
    candleSeries.setData(candles);

    // Markers for limit-up days
    if (ztTimes.size > 0) {
      candleSeries.setMarkers(
        candles.filter(c => ztTimes.has(c.time)).map(c => ({
          time: c.time, position: 'aboveBar',
          color: '#dc2626', shape: 'circle', size: 0.6,
        }))
      );
    }

    // Volume histogram (bottom 22% of chart)
    const volSeries = klineChart.addHistogramSeries({
      priceFormat: { type: 'volume' },
      priceScaleId: 'vol',
    });
    klineChart.priceScale('vol').applyOptions({
      scaleMargins: { top: 0.8, bottom: 0 },
    });
    volSeries.setData(data.kline.map(d => ({
      time:  fmt(d),
      value: d.volume,
      color: d.close >= d.open ? 'rgba(220,38,38,0.4)' : 'rgba(22,163,74,0.4)',
    })));

    klineChart.timeScale().fitContent();

  } catch {
    document.getElementById('kline-loading').classList.add('hidden');
    container.innerHTML = '<div style="padding:20px;color:var(--text-secondary)">加载失败</div>';
  }
}

async function loadTimeline(code) {
  try {
    const data = await fetch(`/api/stock-timeline/${code}/${currentDate}`).then(r => r.json());
    document.getElementById('timeline-loading').classList.add('hidden');
    renderTimeline(data.timeline || []);
  } catch {
    document.getElementById('timeline-loading').textContent = '加载失败';
  }
}

function renderTimeline(timeline) {
  const grid = document.getElementById('timeline-grid');
  grid.innerHTML = '';
  let ztCount = 0, maxConsec = 0;

  timeline.forEach(day => {
    const cell = document.createElement('div');
    const early = isAuction(day.first_time);
    if (day.hit_zt) {
      ztCount++;
      if (day.consecutive > maxConsec) maxConsec = day.consecutive;
      cell.className = `tl-cell zt ${early ? 'early' : ''}`;
      cell.innerHTML = `<span class="tl-date">${fmtDateShort(day.date)}</span>
                        <span class="tl-consec">${day.consecutive}板</span>`;
      cell.title = `${fmtDate(day.date)}\n${day.consecutive}板\n首封:${fmtTime(day.first_time)}\n炸板:${day.break_count}次`;
    } else {
      cell.className = 'tl-cell no-zt';
      cell.innerHTML = `<span class="tl-date">${fmtDateShort(day.date)}</span>`;
      cell.title = fmtDate(day.date);
    }
    grid.appendChild(cell);
  });

  document.getElementById('timeline-stats').innerHTML = `
    <div class="ts-item"><div class="ts-label">近30日涨停</div><div class="ts-val">${ztCount}</div></div>
    <div class="ts-item"><div class="ts-label">最高连板</div><div class="ts-val">${maxConsec||'—'}</div></div>
    <div class="ts-item"><div class="ts-label">涨停频率</div>
      <div class="ts-val">${timeline.length?(ztCount/timeline.length*100).toFixed(0)+'%':'—'}</div></div>
  `;
}

// ─────────────────────────────────────────────────────────────────
// WATCHLIST TAB
// ─────────────────────────────────────────────────────────────────
function renderWatchlistTab() {
  const wl    = getWatchlist();
  const codes = Object.keys(wl);
  document.getElementById('watchlist-title').textContent = `自选股（${codes.length} 只）`;

  const empty = document.getElementById('watchlist-empty');
  const wrap  = document.getElementById('watchlist-table-wrap');
  if (!codes.length) { empty.classList.remove('hidden'); wrap.style.display='none'; return; }
  empty.classList.add('hidden'); wrap.style.display='block';

  const todayMap = {};
  allRecords.forEach(r => { if (r.code) todayMap[r.code] = r; });

  const tbody = document.getElementById('watchlist-tbody');
  tbody.innerHTML = '';
  codes.forEach(code => {
    const r    = todayMap[code];
    const name = wl[code];
    const tr   = document.createElement('tr');
    tr.style.cursor = 'pointer';

    if (r) {
      const isEarly = isAuction(r.first_time);
      tr.innerHTML = `
        <td><button class="star-btn starred" data-code="${code}" data-name="${name}">★</button></td>
        <td><strong>${code}</strong></td>
        <td>${name}</td>
        <td class="status-zt">涨停${r.consecutive>1?' '+r.consecutive+'板':''}</td>
        <td>${isEarly
              ? `<span class="time-early">${fmtTime(r.first_time)}</span><span class="badge-auction">竞价</span>`
              : fmtTime(r.first_time)}</td>
        <td>${fmtAmount(r.volume)}</td>
        <td>${r.break_count>0?`<span class="break-warn">${r.break_count}</span>`:'0'}</td>
        <td><span class="consecutive-badge ${consecClass(r.consecutive||1)}">${r.consecutive||1}</span></td>
        <td><span class="score-badge ${scoreClass(r.score||0)}">${r.score||0}</span></td>
      `;
    } else {
      tr.innerHTML = `
        <td><button class="star-btn starred" data-code="${code}" data-name="${name}">★</button></td>
        <td><strong>${code}</strong></td><td>${name}</td>
        <td class="status-none">未涨停</td>
        <td colspan="5" style="color:var(--text-secondary)">—</td>
      `;
    }

    tr.querySelectorAll('.star-btn').forEach(btn => {
      btn.addEventListener('click', () => { toggleWatchlist(btn.dataset.code, btn.dataset.name); renderWatchlistTab(); });
    });
    tr.addEventListener('click', e => {
      if (e.target.closest('.star-btn')) return;
      openStockModal(code, name);
    });
    tbody.appendChild(tr);
  });
}

// ─────────────────────────────────────────────────────────────────
// UTILS
// ─────────────────────────────────────────────────────────────────
function setLoading(on) {
  document.getElementById('loading-overlay').classList.toggle('hidden', !on);
}

// ─────────────────────────────────────────────────────────────────
// ROLLING WINDOW BACKTEST
// ─────────────────────────────────────────────────────────────────
async function loadBacktest(win) {
  win = win || parseInt(document.getElementById('backtest-window').value) || 20;
  const loading = document.getElementById('bt-loading');
  loading.classList.remove('hidden');
  loading.textContent = '加载中…';
  document.getElementById('bt-meta').classList.add('hidden');
  document.getElementById('bt-dims').classList.add('hidden');
  try {
    const data = await fetch(`/api/feature-backtest/${currentDate}?window=${win}`)
      .then(r => r.json());
    renderBacktest(data, win);
  } catch {
    loading.textContent = '加载失败';
  }
}

async function renderBacktest(data, win) {
  document.getElementById('bt-loading').classList.add('hidden');
  const meta = document.getElementById('bt-meta');
  const dims = document.getElementById('bt-dims');

  if (!data || !data.total) {
    // Show warmup status so user knows data is being fetched
    try {
      const status = await fetch('/api/cache-status').then(r => r.json());
      const need = (win || 20) * 2 + 3;
      meta.innerHTML = status.count < need
        ? `历史缓存 ${status.count} 天，回测需要约 ${need} 天。` +
          `后台正在获取中，稍后点击 <button class="bt-retry-btn" onclick="loadBacktest()">重新加载</button> 即可。`
        : `数据不足，请尝试缩短窗口或 <button class="bt-retry-btn" onclick="loadBacktest()">重新加载</button>`;
    } catch {
      meta.innerHTML = `暂无历史数据，<button class="bt-retry-btn" onclick="loadBacktest()">重新加载</button>`;
    }
    meta.classList.remove('hidden');
    return;
  }

  meta.innerHTML =
    `共 <strong>${data.total}</strong> 个样本 &nbsp;·&nbsp; ` +
    `${fmtDate(data.date_from)} — ${fmtDate(data.date_to)}`;
  meta.classList.remove('hidden');

  // Store cross stats so renderTable() can annotate each stock
  backtestCross = data.cross || null;

  const dimDefs = [
    { key: 'by_seal',   title: '按封板时间' },
    { key: 'by_consec', title: '按连板数' },
    { key: 'by_breaks', title: '按炸板次数' },
  ];

  dims.innerHTML = dimDefs.map(def => {
    const rows = (data[def.key] || []).map(b => {
      const hasT2 = b.t2_win_rate != null && (b.t2_n || 0) >= 3;
      const rate  = hasT2 ? b.t2_win_rate : b.t1_zt_rate;
      const rc    = hasT2
        ? (rate >= 60 ? 'high' : rate >= 40 ? 'mid' : 'low')
        : (rate >= 50 ? 'high' : rate >= 30 ? 'mid' : 'low');
      return `<tr>
        <td class="bt-label">${b.label}</td>
        <td class="bt-n">${b.n}</td>
        <td class="bt-rate bt-${rc}">${hasT2 ? b.t2_win_rate + '%' : '—'}</td>
        <td class="bt-n">${b.t2_ret_mean != null ? fmtPct(b.t2_ret_mean) : '—'}</td>
        <td class="bt-n">${b.t1_prem_mean != null ? fmtPct(b.t1_prem_mean) : '—'}</td>
        <td class="bt-rate bt-${hasT2 ? (b.t1_zt_rate >= 50 ? 'high' : b.t1_zt_rate >= 30 ? 'mid' : 'low') : rc}">${b.t1_zt_rate}%</td>
        <td class="bt-bar-cell">
          <div class="bt-bar-wrap">
            <div class="bt-bar bt-bar-${rc}" style="width:${Math.min(rate,100)}%"></div>
          </div>
        </td>
      </tr>`;
    }).join('');
    return `<div class="bt-dim">
      <div class="bt-dim-title">${def.title}</div>
      <table class="bt-table">
        <thead><tr>
          <th>特征</th><th>样本</th>
          <th>T+2胜率</th><th>T+2均收益</th>
          <th>T+1溢价</th><th>T+1晋级</th>
          <th></th>
        </tr></thead>
        <tbody>${rows}</tbody>
      </table>
    </div>`;
  }).join('');
  dims.classList.remove('hidden');

  // Re-render main table to show predicted rates
  renderTable();
}

// ─────────────────────────────────────────────────────────────────
// WENCAI AI
// ─────────────────────────────────────────────────────────────────
function loadWencai(code, name) {
  wencaiLoaded = true;
  const loading = document.getElementById('wencai-loading');
  const iframe  = document.getElementById('wencai-iframe');
  const errDiv  = document.getElementById('wencai-error');
  const label   = document.getElementById('wencai-stock-label');
  const extLink = document.getElementById('wencai-open-link');

  label.textContent = `${name}（${code}）`;

  // Build query and URLs
  const query   = encodeURIComponent(name || code);
  const sign    = Date.now();
  const proxyUrl  = `/proxy/wencai/screener/result?w=${query}&querytype=stock&sign=${sign}`;
  const directUrl = `https://www.iwencai.com/screener/result?w=${query}&querytype=stock&sign=${sign}`;
  extLink.href = directUrl;

  // Show the URL in the address bar
  const addrEl = document.getElementById('wencai-addr-url');
  addrEl.textContent = directUrl;
  addrEl.href = directUrl;

  loading.classList.remove('hidden');
  iframe.classList.add('hidden');
  errDiv.classList.add('hidden');

  // Set timeout — if iframe doesn't load in 15s show error
  const timer = setTimeout(() => {
    loading.classList.add('hidden');
    errDiv.innerHTML = '加载超时，可能需要在问财登录后重试，或 <a class="wencai-ext-link" href="' + directUrl + '" target="_blank">在新标签打开</a>';
    errDiv.classList.remove('hidden');
  }, 15000);

  iframe.onload = () => {
    clearTimeout(timer);
    loading.classList.add('hidden');
    iframe.classList.remove('hidden');
  };
  iframe.onerror = () => {
    clearTimeout(timer);
    loading.classList.add('hidden');
    errDiv.innerHTML = '加载失败，请 <a class="wencai-ext-link" href="' + directUrl + '" target="_blank">在新标签打开</a>';
    errDiv.classList.remove('hidden');
  };

  iframe.src = proxyUrl;
}

// ─────────────────────────────────────────────────────────────────
// BOOTSTRAP
// ─────────────────────────────────────────────────────────────────
document.addEventListener('DOMContentLoaded', async () => {
  setupTabs();
  setupSorting();
  setupFilters();
  setupStrongSection();
  setupModal();
  updateWatchlistBadge();
  document.getElementById('backtest-window').addEventListener('change', e => {
    loadBacktest(parseInt(e.target.value));
  });
  try {
    const latest = await initDatePicker();
    await loadData(latest);
  } catch (e) {
    document.getElementById('status-msg').textContent = '初始化失败：' + e.message;
    setLoading(false);
  }
});
