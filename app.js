// ============================================================================
// Stonk Advisor - Pure JavaScript Frontend
// ============================================================================

const API = "/api";

// ============================================================================
// State & Router
// ============================================================================

let state = {
  page: "dashboard",
  data: {},
  loading: {},
};

function setState(updates) {
  Object.assign(state, updates);
  render();
}

function navigate(page) {
  setState({ page });
  window.location.hash = page;
}

// ============================================================================
// API
// ============================================================================

async function api(endpoint, options = {}) {
  const url = `${API}${endpoint}`;
  const config = {
    headers: { "Content-Type": "application/json", ...options.headers },
    ...options,
  };
  if (options.body) config.body = JSON.stringify(options.body);
  
  const res = await fetch(url, config);
  if (!res.ok) throw new Error(res.statusText);
  return res.json();
}

// ============================================================================
// Data Fetching
// ============================================================================

async function loadWatchlist() {
  return api("/watchlist");
}

async function loadQuotes(symbols) {
  if (!symbols?.length) return {};
  return api(`/market/quotes?symbols=${symbols.join(",")}`);
}

async function loadAISignals() {
  return api("/ai");
}

async function loadPortfolios() {
  return api("/portfolio");
}

async function loadHoldings(portfolioId) {
  return api(`/portfolio/${portfolioId}/holdings`);
}

async function loadAlerts() {
  return api("/alerts");
}

async function searchSymbols(query) {
  return api(`/market/search?q=${encodeURIComponent(query)}`);
}

function getQuote(symbol) {
  return state.data.quotes?.[symbol];
}

// ============================================================================
// Render Helpers
// ============================================================================

function formatPrice(p) {
  return p?.toFixed(2) ?? "--";
}

function formatChange(p) {
  if (p === undefined || p === null) return "--";
  return (p >= 0 ? "+" : "") + p.toFixed(2) + "%";
}

function signalClass(sig) {
  return sig?.replace(" ", "-") ?? "hold";
}

function signalLabel(sig) {
  return (sig || "hold").replace("_", " ").replace(/\b\w/g, c => c.toUpperCase());
}

// Escapes text for safe interpolation into innerHTML. Needed because quote
// names, search results, and symbols ultimately come from an external API or
// user input and get inserted into the DOM as markup, not just text.
function esc(str) {
  const div = document.createElement("div");
  div.textContent = str ?? "";
  return div.innerHTML;
}

// ============================================================================
// View: Dashboard
// ============================================================================

function renderDashboard() {
  const { watchlist, quotes, signals } = state.data;
  const symbols = watchlist?.symbols || [];
  
  return `
    <div class="section">
      <h1 class="page-title">Market Overview</h1>
      <p class="page-subtitle">Real-time market data and AI-powered insights</p>
    </div>
    
    <div class="section grid-3">
      ${["^GSPC", "^DJI", "^IXIC"].map(sym => {
        const q = quotes?.[sym];
        if (!q) return "";
        return `
          <div class="market-card">
            <div class="market-card-header">
              <span class="market-card-name">${esc(q.name)}</span>
              <span>${q.change_percent >= 0 ? "📈" : "📉"}</span>
            </div>
            <div class="market-card-price">$${formatPrice(q.price)}</div>
            <div class="market-card-change ${q.change_percent >= 0 ? "green" : "red"}">
              ${formatChange(q.change_percent)}
            </div>
          </div>
        `;
      }).join("")}
    </div>
    
    <div class="section grid-2">
      <div class="card">
        <div class="card-header flex-between">
          <span>Watchlist</span>
          <button class="btn btn-secondary" onclick="navigate('trading')">+ Add</button>
        </div>
        <div class="card-body">
          ${symbols.length === 0 
            ? '<div class="empty">No stocks in watchlist</div>'
            : symbols.map(sym => {
              const q = quotes?.[sym];
              return `
                <div class="watchlist-item" onclick="viewQuote('${esc(sym)}')">
                  <div>
                    <div class="watchlist-symbol">${esc(q?.symbol || sym)}</div>
                    <div class="watchlist-name">${esc(q?.name || "Loading...")}</div>
                  </div>
                  <div class="watchlist-price">
                    <div class="watchlist-price-amount">$${formatPrice(q?.price)}</div>
                    <div class="watchlist-price-change ${q?.change_percent >= 0 ? "green" : "red"}">
                      ${formatChange(q?.change_percent)}
                    </div>
                  </div>
                </div>
              `;
            }).join("")
          }
        </div>
      </div>
      
      <div class="card">
        <div class="card-header">AI Signals</div>
        <div class="card-body" style="max-height: 400px; overflow-y: auto;">
          ${(!signals || signals.length === 0)
            ? '<div class="empty">No signals available</div>'
            : signals.slice(0, 10).map(s => `
              <div class="ai-signal ${signalClass(s.signal)}">
                <div class="ai-signal-header">
                  <span class="ai-symbol">${esc(s.symbol)}</span>
                  <div>
                    <span class="signal ${"signal-" + signalClass(s.signal)}">${signalLabel(s.signal)}</span>
                    <span class="confidence">${s.confidence}%</span>
                  </div>
                </div>
                <div class="ai-reasoning">${esc(s.reasoning)}</div>
              </div>
            `).join("")
          }
        </div>
      </div>
    </div>
  `;
}

// ============================================================================
// View: Portfolio
// ============================================================================

async function renderPortfolio() {
  const portfolios = await loadPortfolios();
  const portfolio = portfolios[0];
  if (!portfolio) return '<div class="loading">Loading...</div>';
  
  const holdings = await loadHoldings(portfolio.id);
  const symbols = holdings.map(h => h.symbol);
  const quotes = symbols.length ? await loadQuotes(symbols) : {};
  
  const withPnL = holdings.map(h => {
    const q = quotes[h.symbol];
    const currentValue = q ? q.price * h.shares : 0;
    const costBasis = h.shares * h.avg_cost;
    return { ...h, currentPrice: q?.price || h.avg_cost, currentValue, pnl: currentValue - costBasis };
  });
  
  const totalValue = withPnL.reduce((sum, h) => sum + h.currentValue, 0);
  const totalPnL = withPnL.reduce((sum, h) => sum + h.pnl, 0);
  const totalWithCash = totalValue + portfolio.cash_balance;
  
  return `
    <div class="section">
      <h1 class="page-title">Portfolio</h1>
      <p class="page-subtitle">${esc(portfolio.name)}</p>
    </div>
    
    <div class="section grid-3">
      <div class="market-card">
        <div class="market-card-name">Total Value</div>
        <div class="market-card-price">$${totalWithCash.toLocaleString(undefined, { minimumFractionDigits: 2 })}</div>
      </div>
      <div class="market-card">
        <div class="market-card-name">Cash</div>
        <div class="market-card-price">$${portfolio.cash_balance.toLocaleString()}</div>
      </div>
      <div class="market-card">
        <div class="market-card-name">P&L</div>
        <div class="market-card-price ${totalPnL >= 0 ? "green" : "red"}">
          ${totalPnL >= 0 ? "+" : ""}$${totalPnL.toFixed(2)}
        </div>
      </div>
    </div>
    
    <div class="section card">
      <div class="card-header">Holdings</div>
      <div class="card-body">
        ${withPnL.length === 0 
          ? '<div class="empty">No holdings yet. Start trading to build your portfolio.</div>'
          : `
            <table>
              <thead>
                <tr>
                  <th>Symbol</th>
                  <th style="text-align: right">Shares</th>
                  <th style="text-align: right">Avg Cost</th>
                  <th style="text-align: right">Current</th>
                  <th style="text-align: right">Value</th>
                  <th style="text-align: right">P&L</th>
                </tr>
              </thead>
              <tbody>
                ${withPnL.map(h => `
                  <tr>
                    <td>${esc(h.symbol)}</td>
                    <td style="text-align: right">${h.shares}</td>
                    <td style="text-align: right">$${h.avg_cost.toFixed(2)}</td>
                    <td style="text-align: right">$${h.currentPrice?.toFixed(2)}</td>
                    <td style="text-align: right">$${h.currentValue?.toFixed(2)}</td>
                    <td style="text-align: right" class="${h.pnl >= 0 ? "green" : "red"}">
                      ${h.pnl >= 0 ? "+" : ""}$${h.pnl?.toFixed(2)}
                    </td>
                  </tr>
                `).join("")}
              </tbody>
            </table>
          `
        }
      </div>
    </div>
  `;
}

// ============================================================================
// View: Trading
// ============================================================================

let tradeState = { symbol: "", side: "buy", quantity: 1 };

function renderTrading() {
  const symbol = state.data.tradeSymbol || "";
  const quote = getQuote(symbol);
  const signal = state.data.tradeSignal;
  
  return `
    <div class="section">
      <h1 class="page-title">Trading</h1>
      <p class="page-subtitle">Paper trade with virtual money</p>
    </div>
    
    <div class="section trade-panel">
      <div>
        <div class="card mb-4">
          <div class="card-header">Search Symbol</div>
          <div class="card-body" style="padding: 1rem;">
            <input type="text" id="search-input" placeholder="Search stocks..." oninput="handleSearch(this.value)">
            <div id="search-results" class="mt-4"></div>
          </div>
        </div>
        
        ${symbol ? `
          <div class="card" id="quote-panel">
            <div class="card-body" style="padding: 1rem;">
              <div class="flex-between mb-4">
                <div>
                  <div class="market-card-price">${esc(symbol)}</div>
                  <div class="market-card-name">${esc(quote?.name || "Loading...")}</div>
                </div>
                <div style="text-align: right">
                  <div class="market-card-price">$${formatPrice(quote?.price)}</div>
                  <div class="market-card-change ${quote?.change_percent >= 0 ? "green" : "red"}">
                    ${formatChange(quote?.change_percent)}
                  </div>
                </div>
              </div>
              ${signal ? `
                <div style="display: flex; align-items: center; gap: 0.5rem;">
                  <span class="signal ${"signal-" + signalClass(signal.signal)}">${signalLabel(signal.signal)}</span>
                  <span class="confidence">${signal.confidence}% confidence</span>
                </div>
              ` : ""}
            </div>
          </div>
        ` : ""}
      </div>
      
      <div class="card">
        <div class="card-header">Place Order</div>
        <div class="card-body" style="padding: 1rem;">
          <div class="btn-group mb-4">
            <button class="btn ${tradeState.side === 'buy' ? 'btn-primary' : 'btn-secondary'}" onclick="setTradeSide('buy')">Buy</button>
            <button class="btn ${tradeState.side === 'sell' ? 'btn-danger' : 'btn-secondary'}" onclick="setTradeSide('sell')">Sell</button>
          </div>
          
          <div class="form-group mb-4">
            <label>Quantity</label>
            <input type="number" id="trade-quantity" value="${tradeState.quantity}" min="1" onchange="tradeState.quantity = parseInt(this.value) || 1">
          </div>
          
          <div class="order-summary mb-4">
            <div class="order-row">
              <span>Estimated Total</span>
              <span>$${((quote?.price || 0) * tradeState.quantity).toFixed(2)}</span>
            </div>
          </div>
          
          <button class="btn ${tradeState.side === 'buy' ? 'btn-primary' : 'btn-danger'}" 
                  style="width: 100%;" 
                  onclick="executeTrade()" 
                  ${!symbol ? 'disabled style="opacity: 0.5; cursor: not-allowed;"' : ''}>
            ${tradeState.side === 'buy' ? 'Buy' : 'Sell'} ${symbol || "---"}
          </button>
        </div>
      </div>
    </div>
  `;
}

async function handleSearch(query) {
  if (query.length < 1) {
    document.getElementById("search-results").innerHTML = "";
    return;
  }
  
  const results = await searchSymbols(query);
  const html = results.slice(0, 5).map(r => `
    <div class="watchlist-item" onclick="selectSymbol('${esc(r.symbol)}')" style="cursor: pointer;">
      <span class="watchlist-symbol">${esc(r.symbol)}</span>
      <span class="watchlist-name">${esc(r.name)}</span>
    </div>
  `).join("");
  
  document.getElementById("search-results").innerHTML = html;
}

async function selectSymbol(symbol) {
  state.data.tradeSymbol = symbol;
  const [quote, signal] = await Promise.all([
    api(`/market/quote/${symbol}`).catch(() => null),
    api(`/ai/${symbol}`).catch(() => null),
  ]);
  state.data.quotes = { ...state.data.quotes, [symbol]: quote };
  state.data.tradeSignal = signal;
  render();
}

async function setTradeSide(side) {
  tradeState.side = side;
  render();
}

async function executeTrade() {
  const symbol = state.data.tradeSymbol;
  const portfolios = await loadPortfolios();
  const quote = getQuote(symbol);
  
  if (!portfolios[0] || !symbol || !quote) return;
  
  await api(`/portfolio/${portfolios[0].id}/holdings`, {
    method: "POST",
    body: {
      symbol,
      shares: tradeState.side === "buy" ? tradeState.quantity : -tradeState.quantity,
      avg_cost: quote.price,
    },
  });
  
  tradeState = { symbol: "", side: "buy", quantity: 1 };
  state.data.tradeSymbol = "";
  navigate("portfolio");
}

// ============================================================================
// View: AI Signals
// ============================================================================

function renderAI() {
  const { signals } = state.data;
  
  return `
    <div class="section flex-between">
      <div>
        <h1 class="page-title">AI Signals</h1>
        <p class="page-subtitle">Intelligent trade recommendations powered by technical analysis</p>
      </div>
      <button class="btn btn-secondary" onclick="loadData()">Refresh</button>
    </div>
    
    <div class="card">
      <div class="card-header">AI Analysis Feed</div>
      <div class="card-body">
        ${(!signals || signals.length === 0)
          ? '<div class="empty">No signals available. Add stocks to watchlist first.</div>'
          : signals.map(s => `
            <div class="ai-signal ${signalClass(s.signal)}">
              <div class="ai-signal-header">
                <span class="ai-symbol">${esc(s.symbol)}</span>
                <div>
                  <span class="signal ${"signal-" + signalClass(s.signal)}">${signalLabel(s.signal)}</span>
                  <span class="confidence">${s.confidence}%</span>
                </div>
              </div>
              <div class="ai-reasoning">${esc(s.reasoning)}</div>
            </div>
          `).join("")
        }
      </div>
    </div>
  `;
}

// ============================================================================
// View: Settings
// ============================================================================

function renderSettings() {
  const { watchlist, alerts } = state.data;
  
  return `
    <div class="section">
      <h1 class="page-title">Settings</h1>
      <p class="page-subtitle">Manage watchlist and alerts</p>
    </div>
    
    <div class="section card">
      <div class="card-header">Watchlist</div>
      <div class="card-body" style="padding: 1rem;">
        <div class="form-group mb-4">
          <input type="text" id="new-symbol" placeholder="Add symbol (e.g., AAPL)"
                 onkeydown="if(event.key==='Enter')addSymbol()">
          <button class="btn btn-primary mt-4" onclick="addSymbol()">Add Symbol</button>
        </div>
        <div class="tags">
          ${(watchlist?.symbols || []).map(s => `
            <span class="tag">${esc(s)}</span>
          `).join("")}
        </div>
      </div>
    </div>
    
    <div class="section card">
      <div class="card-header">Price Alerts</div>
      <div class="card-body" style="padding: 1rem;">
        <div class="form-group mb-4">
          <input type="text" id="alert-symbol" placeholder="Symbol">
          <select id="alert-condition">
            <option value="price">Price</option>
            <option value="change">% Change</option>
          </select>
          <select id="alert-type">
            <option value="above">Above</option>
            <option value="below">Below</option>
          </select>
          <input type="number" id="alert-value" placeholder="Threshold">
          <button class="btn btn-primary" onclick="createAlert()">Create Alert</button>
        </div>
        <div>
          ${(alerts || []).map(a => `
            <div style="display: flex; justify-content: space-between; padding: 0.5rem 0; border-bottom: 1px solid var(--border);">
              <span>${esc(a.symbol)} ${esc(a.type)} ${esc(a.condition)} ${esc(String(a.threshold))}</span>
              <button class="btn btn-secondary" onclick="deleteAlert('${esc(a.id)}')">Delete</button>
            </div>
          `).join("")}
        </div>
      </div>
    </div>
  `;
}

async function addSymbol() {
  const input = document.getElementById("new-symbol");
  const symbol = input.value.trim().toUpperCase();
  if (!symbol) return;
  
  await api("/watchlist", { method: "POST", body: { symbols: [symbol] } });
  input.value = "";
  loadData();
}

async function createAlert() {
  const symbol = document.getElementById("alert-symbol").value.trim().toUpperCase();
  const type = document.getElementById("alert-condition").value;
  const condition = document.getElementById("alert-type").value;
  const threshold = parseFloat(document.getElementById("alert-value").value);
  
  if (!symbol || !threshold) return;
  
  await api("/alerts", {
    method: "POST",
    body: { symbol, type, condition, threshold },
  });
  
  loadData();
}

async function deleteAlert(id) {
  await api(`/alerts/${id}`, { method: "DELETE" });
  loadData();
}

// ============================================================================
// Navigation & Render
// ============================================================================

function renderNav() {
  const pages = [
    { id: "dashboard", label: "Dashboard", icon: "📊" },
    { id: "portfolio", label: "Portfolio", icon: "💼" },
    { id: "trading", label: "Trading", icon: "💰" },
    { id: "ai", label: "AI Signals", icon: "🤖" },
    { id: "settings", label: "Settings", icon: "⚙️" },
  ];
  
  return pages.map(p => `
    <a href="#${p.id}" class="${state.page === p.id ? 'active' : ''}">
      <span>${p.icon}</span> ${p.label}
    </a>
  `).join("");
}

async function render() {
  const nav = document.getElementById("nav");
  if (nav) nav.innerHTML = renderNav();
  
  const app = document.getElementById("app");
  if (!app) return;
  
  switch (state.page) {
    case "dashboard": app.innerHTML = renderDashboard(); break;
    case "portfolio": app.innerHTML = await renderPortfolio(); break;
    case "trading": app.innerHTML = renderTrading(); break;
    case "ai": app.innerHTML = renderAI(); break;
    case "settings": app.innerHTML = renderSettings(); break;
    default: app.innerHTML = renderDashboard();
  }
  
  document.getElementById("last-update").textContent = new Date().toLocaleTimeString();
}

async function loadData() {
  try {
    const [watchlist, quotes, signals, alerts] = await Promise.all([
      loadWatchlist(),
      loadWatchlist().then(w => loadQuotes(w?.symbols)),
      loadAISignals(),
      loadAlerts(),
    ]);
    
    state.data = { watchlist, quotes: quotes || {}, signals, alerts };
  } catch (e) {
    console.error("Load error:", e);
  }
  render();
}

// ============================================================================
// Init
// ============================================================================

document.addEventListener("DOMContentLoaded", async () => {
  const hash = window.location.hash.slice(1) || "dashboard";
  state.page = hash;
  
  await loadData();
  
  // Auto-refresh every 30 seconds
  setInterval(loadData, 30000);
});

window.onhashchange = () => {
  const page = window.location.hash.slice(1) || "dashboard";
  state.page = page;
  render();
};

window.navigate = navigate;
window.viewQuote = selectSymbol;
window.handleSearch = handleSearch;
window.selectSymbol = selectSymbol;
window.setTradeSide = setTradeSide;
window.executeTrade = executeTrade;
window.addSymbol = addSymbol;
window.createAlert = createAlert;
window.deleteAlert = deleteAlert;
window.loadData = loadData;