// App shell: sidebar, topbar, command palette, alerts inbox, theme toggle.
const { useState: useS, useEffect: useE, useMemo: useM, createContext, useContext } = React;

const AppCtx = createContext(null);
window.useApp = () => useContext(AppCtx);

const ARB_PAGE_KEY = 'arbiter-current-page';
const ARB_PAGE_IDS = ['overview', 'opportunities', 'trades', 'pnl', 'markets', 'mappings', 'scanner', 'audit', 'deposits', 'settings'];

function normalizePageId(value) {
  const raw = String(value || '').replace(/^#\/?/, '').trim();
  return ARB_PAGE_IDS.includes(raw) ? raw : 'overview';
}

function readStoredPage() {
  const hashPage = String(window.location.hash || '').replace(/^#\/?/, '').trim();
  if (ARB_PAGE_IDS.includes(hashPage)) return hashPage;
  try { return normalizePageId(localStorage.getItem(ARB_PAGE_KEY)); } catch { return 'overview'; }
}

function persistPage(page) {
  const next = normalizePageId(page);
  try { localStorage.setItem(ARB_PAGE_KEY, next); } catch {}
  try {
    const desired = next === 'overview' ? window.location.pathname + window.location.search : '#' + next;
    const current = window.location.hash ? window.location.hash : window.location.pathname + window.location.search;
    if (current !== desired) window.history.replaceState(null, '', desired);
  } catch {}
}

function useTheme() {
  const [name, setName] = useS(() => {
    const saved = localStorage.getItem('arbiter-theme');
    if (saved) return saved;
    return window.matchMedia?.('(prefers-color-scheme: dark)').matches ? 'dark' : 'light';
  });
  useE(() => { localStorage.setItem('arbiter-theme', name); }, [name]);
  const t = window.THEMES[name];
  return { t, name, setName, toggle: () => setName(n => n === 'light' ? 'dark' : 'light') };
}

function AppProvider({ children, onSignOut }) {
  const theme = useTheme();
  const [page, setPageState] = useS(readStoredPage);
  const [drawer, setDrawer] = useS(null);   // { kind, payload }
  const [modal, setModal] = useS(null);
  const [palette, setPalette] = useS(false);
  const [alerts, setAlerts] = useS(false);
  const [scanner, setScanner] = useS(true);
  const [tradingMode, setTradingMode] = useS('live'); // 'live' | 'standby' | 'killed'
  const [minEdge, setMinEdge] = useS(1.0);
  const [autoExec, setAutoExec] = useS(true);
  const [toasts, setToasts] = useS([]);
  const [confirm, setConfirm] = useS(null);
  const [userMenu, setUserMenu] = useS(false);
  const [profile, setProfile] = useS({ name:'Sam Park', email:'sam@arbiter.app', initials:'SP' });
  const [watchlist, setWatchlist] = useS(['BTC-150K-EOY-2026']);
  const [filters, setFilters] = useS({ trades: {}, markets: {}, audit: {} });
  const [settings, setSettings] = useS({
    minEdge: 1.0, minVolume: 500, persistence: 3, maxPos: 200, autoExec: 'on',
    slippage: 0.5, scanInterval: 5.0, concurrency: 32,
  });
  const [notifPrefs, setNotifPrefs] = useS({
    killSwitch: true, recovery: true, fail: true, daily: true, mapping: false, deposit: true,
  });

  const toast = (msg, opts = {}) => {
    const id = Math.random().toString(36).slice(2);
    setToasts(t => [...t, { id, msg, ...opts }]);
    setTimeout(() => setToasts(t => t.filter(x => x.id !== id)), opts.duration || 2800);
  };
  const signOut = () => { onSignOut?.(); };
  const setPage = (next) => {
    const pageId = normalizePageId(next);
    persistPage(pageId);
    setPageState(pageId);
  };

  useE(() => {
    const onKey = (e) => {
      if ((e.metaKey || e.ctrlKey) && e.key === 'k') { e.preventDefault(); setPalette(p => !p); }
      if (e.key === 'Escape') { setPalette(false); setDrawer(null); setModal(null); setAlerts(false); setConfirm(null); setUserMenu(false); }
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, []);

  useE(() => {
    const onHash = () => {
      const next = readStoredPage();
      try { localStorage.setItem(ARB_PAGE_KEY, next); } catch {}
      setPageState(next);
    };
    window.addEventListener('hashchange', onHash);
    return () => window.removeEventListener('hashchange', onHash);
  }, []);

  const value = { ...theme, page, setPage, drawer, setDrawer, modal, setModal,
    palette, setPalette, alerts, setAlerts,
    scanner, setScanner, tradingMode, setTradingMode, minEdge, setMinEdge, autoExec, setAutoExec,
    toasts, setToasts, toast, confirm, setConfirm,
    userMenu, setUserMenu, profile, setProfile, signOut,
    watchlist, setWatchlist, filters, setFilters,
    settings, setSettings, notifPrefs, setNotifPrefs,
  };
  return <AppCtx.Provider value={value}>{children}</AppCtx.Provider>;
}

const NAV = [
  { id: 'overview',     label: 'Overview',      icon: '⌂' },
  { id: 'opportunities',label: 'Opportunities', icon: '◇', badgeKey: 'oppCount' },
  { id: 'trades',       label: 'Trades',        icon: '↗' },
  { id: 'pnl',          label: 'P&L',           icon: '$' },
  { id: 'markets',      label: 'Markets',       icon: '◈' },
  { id: 'mappings',     label: 'Mappings',      icon: '⇄' },
  { id: 'scanner',      label: 'Scanner',       icon: '⚡' },
  { id: 'audit',        label: 'Audit',         icon: '✓' },
  { id: 'deposits',     label: 'Funds',         icon: '◐' },
  { id: 'settings',     label: 'Settings',      icon: '⚙' },
];

function Sidebar() {
  const { t, page, setPage } = window.useApp();
  return (
    <div style={{ width: 220, background: t.bgCard, borderRight: `1px solid ${t.border}`, padding: '20px 14px', display:'flex', flexDirection:'column', flexShrink: 0 }}>
      <div style={{ display:'flex', alignItems:'center', gap: 10, padding: '4px 8px 22px', cursor:'pointer' }} onClick={() => setPage('overview')}>
        <div style={{ width: 28, height: 28, borderRadius: 7, background: t.text, display:'flex', alignItems:'center', justifyContent:'center', color: t.bgCard, fontSize: 13, fontWeight: 700 }}>A</div>
        <div style={{ fontSize: 14, fontWeight: 600, letterSpacing: '-0.01em', color: t.text }}>Arbiter</div>
      </div>
      <div style={{ fontSize: 10.5, fontWeight: 600, color: t.textMuted, letterSpacing: '0.08em', padding: '0 8px 8px' }}>WORKSPACE</div>
      {NAV.map(n => (
        <div key={n.id} onClick={() => setPage(n.id)} style={{
          display:'flex', alignItems:'center', gap: 10, padding: '7px 10px', borderRadius: 6, cursor: 'pointer',
          background: page === n.id ? t.bgSubtle : 'transparent',
          color: page === n.id ? t.text : t.textDim,
          fontSize: 13, fontWeight: page === n.id ? 500 : 400, marginBottom: 2,
        }}>
          <span style={{ width: 14, fontSize: 13, color: page === n.id ? t.accent : t.textMuted, fontFamily: window.FONTS.mono, textAlign:'center' }}>{n.icon}</span>
          <span>{n.label}</span>
          {n.id === 'opportunities' && <span style={{ marginLeft:'auto', fontSize: 10, padding: '1px 6px', background: t.accent, color: '#fff', borderRadius: 99, fontWeight: 500 }}>14</span>}
        </div>
      ))}
      <div style={{ marginTop: 'auto' }}>
        <ReadinessMini/>
      </div>
    </div>
  );
}

function ReadinessMini() {
  const { t, setPage, tradingMode } = window.useApp();
  const ok = tradingMode === 'live';
  const c = ok ? t.green : tradingMode === 'standby' ? t.amber : t.red;
  const cs = ok ? t.greenSoft : tradingMode === 'standby' ? t.amberSoft : t.redSoft;
  const txt = ok ? 'Live trading' : tradingMode === 'standby' ? 'Standby' : 'KILLED';
  const sub = ok ? '7/7 readiness gates' : tradingMode === 'standby' ? 'Auto-exec disabled' : 'Manual restart required';
  return (
    <div onClick={() => setPage('audit')} style={{ padding: 12, background: cs, border: `1px solid ${c}30`, borderRadius: 8, cursor:'pointer' }}>
      <div style={{ display:'flex', alignItems:'center', gap: 6, fontSize: 11, color: c, fontWeight: 600, marginBottom: 4 }}>
        <span style={{ width: 6, height: 6, borderRadius:'50%', background: c, animation: ok ? 'pulse 2s infinite' : 'none' }}/>
        {txt}
      </div>
      <div style={{ fontSize: 10.5, color: t.textDim, lineHeight: 1.4 }}>{sub}</div>
    </div>
  );
}

function TopBar({ title, sub, actions }) {
  const { t, setPalette, setAlerts, toggle, name, scanner, setScanner, tradingMode, setTradingMode, setUserMenu, profile } = window.useApp();
  return (
    <div style={{ padding: '14px 28px', borderBottom: `1px solid ${t.border}`, display:'flex', alignItems:'center', justifyContent:'space-between', background: t.bg }}>
      <div>
        <div style={{ fontSize: 11, color: t.textMuted, marginBottom: 2 }}>{sub || 'Workspace · Live'}</div>
        <div style={{ fontSize: 19, fontWeight: 600, letterSpacing: '-0.01em', color: t.text }}>{title}</div>
      </div>
      <div style={{ display:'flex', alignItems:'center', gap: 8 }}>
        <ScannerToggle scanner={scanner} setScanner={setScanner} />
        <ModeToggle mode={tradingMode} setMode={setTradingMode} />
        <button onClick={() => setPalette(true)} style={{ display:'flex', alignItems:'center', gap: 8, padding: '7px 12px', background: t.bgCard, border: `1px solid ${t.border}`, borderRadius: 8, fontSize: 12, color: t.textDim, width: 240, cursor: 'pointer' }}>
          <span>⌕</span><span>Search markets, trades…</span>
          <span style={{ marginLeft:'auto', fontSize: 10, color: t.textMuted, fontFamily: window.FONTS.mono, padding: '1px 5px', border:`1px solid ${t.border}`, borderRadius: 3 }}>⌘K</span>
        </button>
        <IconBtn onClick={() => setAlerts(true)} t={t} badge="3">⏷</IconBtn>
        <IconBtn onClick={toggle} t={t}>{name === 'light' ? '☾' : '☀'}</IconBtn>
        <button onClick={(e) => { e.stopPropagation(); setUserMenu(v => !v); }} style={{ width: 30, height: 30, borderRadius: 7, background: t.accent, border:'none', display:'flex', alignItems:'center', justifyContent:'center', color: '#fff', fontSize: 11, fontWeight: 700, cursor:'pointer' }}>{profile.initials}</button>
      </div>
    </div>
  );
}

function IconBtn({ children, onClick, t, badge }) {
  return (
    <button onClick={onClick} style={{ position:'relative', width: 32, height: 32, background: t.bgCard, border: `1px solid ${t.border}`, borderRadius: 8, fontSize: 14, color: t.text, cursor: 'pointer', display:'flex', alignItems:'center', justifyContent:'center' }}>
      {children}
      {badge && <span style={{ position:'absolute', top: -4, right: -4, minWidth: 16, height: 16, padding: '0 4px', background: t.red, color: '#fff', borderRadius: 99, fontSize: 9.5, fontWeight: 600, display:'flex', alignItems:'center', justifyContent:'center' }}>{badge}</span>}
    </button>
  );
}

function ScannerToggle({ scanner, setScanner }) {
  const { t } = window.useApp();
  return (
    <button onClick={() => setScanner(!scanner)} style={{ display:'flex', alignItems:'center', gap: 6, padding: '6px 10px', background: scanner ? t.greenSoft : t.bgCard, border: `1px solid ${scanner ? t.green + '40' : t.border}`, borderRadius: 8, fontSize: 11.5, color: scanner ? t.green : t.textDim, fontWeight: 500, cursor: 'pointer' }}>
      <span style={{ width: 6, height: 6, borderRadius:'50%', background: scanner ? t.green : t.textMuted, animation: scanner ? 'pulse 2s infinite' : 'none' }}/>
      {scanner ? 'Scanner on' : 'Paused'}
    </button>
  );
}

function ModeToggle({ mode, setMode }) {
  const { t } = window.useApp();
  const cfg = mode === 'live' ? [t.green, t.greenSoft, 'Live'] : mode === 'standby' ? [t.amber, t.amberSoft, 'Standby'] : [t.red, t.redSoft, 'Killed'];
  return (
    <button onClick={() => setMode(mode === 'live' ? 'standby' : mode === 'standby' ? 'killed' : 'live')} style={{ padding: '6px 10px', background: cfg[1], border: `1px solid ${cfg[0]}40`, borderRadius: 8, fontSize: 11.5, color: cfg[0], fontWeight: 600, cursor:'pointer', textTransform:'uppercase', letterSpacing:'0.06em' }}>
      {cfg[2]}
    </button>
  );
}

function CommandPalette() {
  const { t, palette, setPalette, setPage, setDrawer } = window.useApp();
  const [q, setQ] = useS('');
  if (!palette) return null;
  const items = [
    { kind: 'page', label: 'Go to Overview', action: () => setPage('overview') },
    { kind: 'page', label: 'Go to Opportunities', action: () => setPage('opportunities') },
    { kind: 'page', label: 'Go to Trades', action: () => setPage('trades') },
    { kind: 'page', label: 'Go to P&L', action: () => setPage('pnl') },
    { kind: 'page', label: 'Go to Markets', action: () => setPage('markets') },
    { kind: 'page', label: 'Go to Mappings', action: () => setPage('mappings') },
    { kind: 'page', label: 'Go to Scanner', action: () => setPage('scanner') },
    { kind: 'page', label: 'Go to Audit', action: () => setPage('audit') },
    { kind: 'page', label: 'Go to Funds', action: () => setPage('deposits') },
    { kind: 'page', label: 'Go to Settings', action: () => setPage('settings') },
    ...window.MOCK.opportunities.slice(0, 8).map((o, i) => ({ kind: 'opp', label: o.description, sub: o.canonical_id, action: () => { setPage('opportunities'); setDrawer({ kind: 'opp', payload: i }); } })),
  ];
  const filtered = q ? items.filter(i => i.label.toLowerCase().includes(q.toLowerCase()) || (i.sub || '').toLowerCase().includes(q.toLowerCase())) : items;
  return (
    <div onClick={() => setPalette(false)} style={{ position:'fixed', inset: 0, background: t.overlay, display:'flex', justifyContent:'center', alignItems:'flex-start', paddingTop: 100, zIndex: 100 }}>
      <div onClick={e => e.stopPropagation()} style={{ width: 560, background: t.bgCard, border: `1px solid ${t.border}`, borderRadius: 12, boxShadow: t.shadowLg, overflow:'hidden' }}>
        <input autoFocus placeholder="Search pages, markets, trades…" value={q} onChange={e => setQ(e.target.value)} style={{ width: '100%', padding: '14px 18px', border:'none', background:'transparent', outline:'none', fontSize: 14, color: t.text, borderBottom: `1px solid ${t.border}` }}/>
        <div style={{ maxHeight: 360, overflow:'auto' }}>
          {filtered.slice(0, 12).map((i, idx) => (
            <div key={idx} onClick={() => { i.action(); setPalette(false); setQ(''); }} style={{ padding: '10px 18px', cursor:'pointer', display:'flex', alignItems:'center', gap: 10, fontSize: 13, color: t.text, borderBottom: idx < filtered.length - 1 ? `1px solid ${t.border}` : 'none' }} onMouseEnter={e => e.currentTarget.style.background = t.bgSubtle} onMouseLeave={e => e.currentTarget.style.background = 'transparent'}>
              <span style={{ fontSize: 10, padding: '2px 6px', background: i.kind === 'opp' ? t.accentSoft : t.bgSubtle, color: i.kind === 'opp' ? t.accent : t.textDim, borderRadius: 4, fontWeight: 600, letterSpacing: '0.04em' }}>{i.kind === 'opp' ? 'OPP' : 'GO'}</span>
              <span style={{ flex: 1 }}>{i.label}</span>
              {i.sub && <span style={{ fontSize: 10.5, color: t.textMuted, fontFamily: window.FONTS.mono }}>{i.sub}</span>}
            </div>
          ))}
        </div>
        <div style={{ padding: '10px 18px', background: t.bgSubtle, fontSize: 10.5, color: t.textMuted, display:'flex', gap: 14 }}>
          <span>↑↓ navigate</span><span>↵ select</span><span>esc close</span>
        </div>
      </div>
    </div>
  );
}

function AlertsInbox() {
  const { t, alerts, setAlerts, setModal, toast, setPage } = window.useApp();
  if (!alerts) return null;
  const items = [
    { sev: 'info', title: 'Scanner heartbeat clean', body: '18,420 scans · 214ms avg latency', ts: '2m', icon: '⚡', go:'scanner', detail:'Last 12 scans within 220ms · 0 errors · published 14 opportunities' },
    { sev: 'warn', title: 'Polymarket fee rate updated', body: 'GPT-6 market — 2.0% → 2.4% on YES leg', ts: '14m', icon: '$', go:'markets', detail:'effective fee_rate_yes: 0.020 → 0.024\nimpact: net edge reduced by 0.4¢ on this market' },
    { sev: 'info', title: 'New mapping confirmed', body: 'BTC-150K-EOY-2026 score 0.94 → confirmed', ts: '38m', icon: '⇄', go:'mappings', detail:'kalshi: BTC-150K-DEC2026\npoly: will-bitcoin-reach-150k-by-2026\nsimilarity: 0.94' },
    { sev: 'ok',   title: 'Deposit settled', body: '+$100.00 to Polymarket · tx confirmed', ts: '2h', icon: '◐', go:'deposits', detail:'tx: 0x4a7…f2c9\nblock: 14,392,108\namount: $100.00 USDC' },
    { sev: 'warn', title: 'Recovery on ARB-09412', body: 'YES leg filled, NO leg submitted — within tolerance', ts: '3h', icon: '↗', go:'trades', detail:'auto-recovery engaged · NO leg slippage budget within 0.5¢' },
    { sev: 'err',  title: 'ARB-09405 failed', body: 'Polymarket NO leg rejected · refunded $32.40', ts: '6h', icon: '✕', go:'trades', detail:'reason: orderbook moved · refund_id: rf_4928f\namount: $32.40 returned to Polymarket balance' },
  ];
  const colorFor = s => s === 'err' ? t.red : s === 'warn' ? t.amber : s === 'ok' ? t.green : t.accent;
  const bgFor = s => s === 'err' ? t.redSoft : s === 'warn' ? t.amberSoft : s === 'ok' ? t.greenSoft : t.accentSoft;
  return (
    <div onClick={() => setAlerts(false)} style={{ position:'fixed', inset: 0, background: t.overlay, zIndex: 90, display:'flex', justifyContent:'flex-end' }}>
      <div onClick={e => e.stopPropagation()} style={{ width: 420, height: '100%', background: t.bgCard, borderLeft: `1px solid ${t.border}`, display:'flex', flexDirection:'column' }}>
        <div style={{ padding: '18px 22px', borderBottom: `1px solid ${t.border}`, display:'flex', alignItems:'center', justifyContent:'space-between' }}>
          <div>
            <div style={{ fontSize: 11, color: t.textMuted }}>Last 24 hours</div>
            <div style={{ fontSize: 17, fontWeight: 600, color: t.text }}>Alerts & activity</div>
          </div>
          <div style={{ display:'flex', gap: 6 }}>
            <button onClick={() => { setAlerts(false); toast('All alerts marked read'); }} style={{ padding:'4px 10px', background: t.bgCard, border:`1px solid ${t.border}`, borderRadius: 6, fontSize: 11, color: t.text, cursor:'pointer' }}>Mark all read</button>
            <button onClick={() => { setAlerts(false); setPage('settings'); setModal({ kind:'notifPrefs' }); }} style={{ padding:'4px 8px', background: t.bgCard, border:`1px solid ${t.border}`, borderRadius: 6, fontSize: 11, color: t.text, cursor:'pointer' }}>⚙</button>
            <button onClick={() => setAlerts(false)} style={{ background:'none', border:'none', fontSize: 18, color: t.textMuted, cursor:'pointer', padding: '0 4px' }}>✕</button>
          </div>
        </div>
        <div style={{ flex: 1, overflow:'auto', padding: 12 }}>
          {items.map((it, i) => (
            <div key={i} onClick={() => { setAlerts(false); setModal({ kind:'notification', payload: it }); }} style={{ padding: 14, borderRadius: 8, marginBottom: 8, background: t.bgSubtle, display:'flex', gap: 12, cursor:'pointer' }} onMouseEnter={e => e.currentTarget.style.background = t.bgHover || t.bgSubtle} onMouseLeave={e => e.currentTarget.style.background = t.bgSubtle}>
              <div style={{ width: 30, height: 30, borderRadius: 8, background: bgFor(it.sev), color: colorFor(it.sev), display:'flex', alignItems:'center', justifyContent:'center', fontSize: 14, flexShrink: 0 }}>{it.icon}</div>
              <div style={{ flex: 1, minWidth: 0 }}>
                <div style={{ display:'flex', alignItems:'center', justifyContent:'space-between', gap: 8 }}>
                  <div style={{ fontSize: 13, fontWeight: 500, color: t.text }}>{it.title}</div>
                  <div style={{ fontSize: 10.5, color: t.textMuted, fontFamily: window.FONTS.mono, flexShrink: 0 }}>{it.ts} ago</div>
                </div>
                <div style={{ fontSize: 11.5, color: t.textDim, marginTop: 2, lineHeight: 1.4 }}>{it.body}</div>
              </div>
            </div>
          ))}
        </div>
      </div>
    </div>
  );
}

window.AppProvider = AppProvider;
window.Sidebar = Sidebar;
window.TopBar = TopBar;
window.IconBtn = IconBtn;
window.CommandPalette = CommandPalette;
window.AlertsInbox = AlertsInbox;
window.NAV = NAV;
