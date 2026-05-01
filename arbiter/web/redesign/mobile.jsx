// Mobile dashboard — single-screen tab bar + key views.
const { useState: mobUseState } = React;

function MobileDashboard() {
  const { t, scanner, setScanner, tradingMode } = window.useApp();
  const [tab, setTab] = mobUseState('home');
  const [meSub, setMeSub] = mobUseState(null); // 'funds' | 'pnl' | 'health' | 'settings' | null
  const M = window.MOCK;
  const totalBal = M.balances.kalshi.balance + M.balances.polymarket.balance;
  const totalPnl = M.pnl.recorded_trading_pnl.kalshi + M.pnl.recorded_trading_pnl.polymarket;

  // When switching primary tabs, clear sub-page
  const switchTab = (k) => { setTab(k); setMeSub(null); };

  return (
    <div style={{ width: '100%', height: '100%', background: t.bg, color: t.text, fontFamily: window.FONTS.sans, display:'flex', flexDirection:'column', overflow:'hidden' }}>
      <div style={{ padding: '14px 18px 12px', display:'flex', alignItems:'center', gap: 10 }}>
        {meSub ? (
          <button onClick={() => setMeSub(null)} style={{ background:'none', border:'none', color: t.text, fontSize: 18, cursor:'pointer', padding: 0, marginRight: 4 }}>‹</button>
        ) : (
          <div style={{ width: 26, height: 26, borderRadius: 7, background: t.text, color: t.bgCard, display:'flex', alignItems:'center', justifyContent:'center', fontSize: 12, fontWeight: 700 }}>A</div>
        )}
        <div style={{ fontSize: 13, fontWeight: 600 }}>{meSub ? ({ funds:'Funds & Deposits', pnl:'P&L', health:'System Health', settings:'Settings' }[meSub]) : 'Arbiter'}</div>
        <div style={{ marginLeft:'auto', display:'flex', alignItems:'center', gap: 4, padding: '3px 8px', background: tradingMode === 'live' ? t.greenSoft : t.amberSoft, color: tradingMode === 'live' ? t.green : t.amber, borderRadius: 99, fontSize: 10.5, fontWeight: 600 }}>
          <span style={{ width: 5, height: 5, borderRadius:'50%', background: tradingMode === 'live' ? t.green : t.amber, animation: 'pulse 2s infinite' }}/>
          {tradingMode === 'live' ? 'LIVE' : 'STANDBY'}
        </div>
      </div>

      <div style={{ flex: 1, overflow:'auto', padding: '0 16px 80px' }}>
        {meSub === 'funds' && <MobFunds/>}
        {meSub === 'pnl' && <MobPnL/>}
        {meSub === 'health' && <MobHealth/>}
        {meSub === 'settings' && <MobSettings/>}
        {!meSub && tab === 'home' && <MobHome totalBal={totalBal} totalPnl={totalPnl}/>}
        {!meSub && tab === 'opps' && <MobOpps/>}
        {!meSub && tab === 'trades' && <MobTrades/>}
        {!meSub && tab === 'maps' && <MobMappings/>}
        {!meSub && tab === 'me' && <MobMe onSub={setMeSub}/>}
      </div>

      {/* Tab bar */}
      <div style={{ position:'absolute', bottom: 0, left: 0, right: 0, padding: '8px 8px 24px', background: t.bgCard, borderTop: `1px solid ${t.border}`, display:'flex', justifyContent:'space-around' }}>
        {[
          ['home','Home','⌂'],['opps','Edge','◇'],['trades','Trades','↗'],['maps','Maps','⇄'],['me','Account','◐'],
        ].map(([k, l, ic]) => (
          <button key={k} onClick={() => switchTab(k)} style={{ flex: 1, padding: '6px 4px', background:'none', border:'none', cursor:'pointer', display:'flex', flexDirection:'column', alignItems:'center', gap: 2, color: tab === k && !meSub ? t.accent : t.textMuted, fontSize: 10 }}>
            <span style={{ fontSize: 18, fontFamily: window.FONTS.mono }}>{ic}</span>
            <span style={{ fontWeight: tab === k && !meSub ? 600 : 400 }}>{l}</span>
          </button>
        ))}
      </div>
    </div>
  );
}

function MobHome({ totalBal, totalPnl }) {
  const { t, setScanner, scanner } = window.useApp();
  const M = window.MOCK;
  return (
    <div>
      <div style={{ padding: '12px 4px 18px' }}>
        <div style={{ fontSize: 11, color: t.textMuted, letterSpacing:'0.06em', textTransform:'uppercase' }}>Total balance</div>
        <div style={{ fontSize: 36, fontWeight: 600, color: t.text, fontFamily: window.FONTS.mono, letterSpacing:'-0.02em', marginTop: 4 }}>{window.fmt$(totalBal)}</div>
        <div style={{ fontSize: 13, color: totalPnl >= 0 ? t.green : t.red, marginTop: 4 }}>{window.fmt$Sign(totalPnl)} ({window.fmtPct(totalPnl/1000)}) since inception</div>
      </div>

      <div style={{ background: t.bgCard, border: `1px solid ${t.border}`, borderRadius: 12, padding: '14px 14px 4px', marginBottom: 12 }}>
        <div style={{ display:'flex', alignItems:'center', justifyContent:'space-between', marginBottom: 8 }}>
          <span style={{ fontSize: 12, fontWeight: 600 }}>Equity</span>
          <span style={{ fontSize: 10.5, color: t.textMuted, fontFamily: window.FONTS.mono }}>24h</span>
        </div>
        <div style={{ height: 100 }}>
          <window.AreaChart data={M.equity} stroke={t.accent} grid={t.border} showAxis={false}/>
        </div>
      </div>

      <div style={{ display:'grid', gridTemplateColumns:'1fr 1fr', gap: 8, marginBottom: 12 }}>
        <MobMini label="Kalshi" value={window.fmt$(M.balances.kalshi.balance)} dot={t.green}/>
        <MobMini label="Polymarket" value={window.fmt$(M.balances.polymarket.balance)} dot={t.purple}/>
      </div>

      <div style={{ background: scanner ? t.greenSoft : t.bgSubtle, border: `1px solid ${scanner ? t.green + '30' : t.border}`, borderRadius: 12, padding: 14, marginBottom: 12, display:'flex', alignItems:'center', gap: 10 }}>
        <span style={{ width: 8, height: 8, borderRadius:'50%', background: scanner ? t.green : t.textMuted, animation: scanner ? 'pulse 2s infinite' : 'none' }}/>
        <div style={{ flex: 1 }}>
          <div style={{ fontSize: 12, fontWeight: 600, color: scanner ? t.green : t.textDim }}>Scanner {scanner ? 'live' : 'paused'}</div>
          <div style={{ fontSize: 10.5, color: t.textDim }}>{scanner ? '14 tradable · best 4.8¢ · 214ms' : 'Resume to monitor markets'}</div>
        </div>
        <button onClick={() => setScanner(!scanner)} style={{ padding:'5px 10px', background: t.bgCard, border: `1px solid ${t.border}`, borderRadius: 6, fontSize: 11, fontWeight: 600, color: t.text, cursor:'pointer' }}>{scanner ? 'Pause' : 'Resume'}</button>
      </div>

      <div style={{ fontSize: 11, color: t.textMuted, letterSpacing:'0.08em', padding: '4px', marginTop: 8, marginBottom: 8 }}>TOP OPPORTUNITIES</div>
      {M.opportunities.slice(0, 3).map((o, i) => (
        <div key={i} style={{ background: t.bgCard, border: `1px solid ${t.border}`, borderRadius: 12, padding: 14, marginBottom: 8 }}>
          <div style={{ display:'flex', alignItems:'flex-start', gap: 10, marginBottom: 8 }}>
            <div style={{ flex: 1, fontSize: 12.5, color: t.text, lineHeight: 1.35 }}>{o.description}</div>
            <div style={{ fontSize: 14, fontWeight: 700, color: t.green, fontFamily: window.FONTS.mono, flexShrink: 0 }}>{window.fmtC(o.net_edge_cents)}</div>
          </div>
          <div style={{ display:'flex', alignItems:'center', gap: 6, fontSize: 10.5 }}>
            <window.PlatformChip name={o.yes_platform} side="Y"/>
            <window.PlatformChip name={o.no_platform} side="N"/>
            <span style={{ marginLeft:'auto', color: t.textDim, fontFamily: window.FONTS.mono }}>+{window.fmt$(o.expected_profit)}</span>
          </div>
        </div>
      ))}
    </div>
  );
}

function MobMini({ label, value, dot }) {
  const { t } = window.useApp();
  return (
    <div style={{ background: t.bgCard, border: `1px solid ${t.border}`, borderRadius: 10, padding: 12 }}>
      <div style={{ display:'flex', alignItems:'center', gap: 6, marginBottom: 6 }}>
        <span style={{ width: 6, height: 6, borderRadius:'50%', background: dot }}/>
        <span style={{ fontSize: 10.5, color: t.textMuted, letterSpacing:'0.04em', textTransform:'uppercase' }}>{label}</span>
      </div>
      <div style={{ fontSize: 15, fontWeight: 600, color: t.text, fontFamily: window.FONTS.mono }}>{value}</div>
    </div>
  );
}

function MobOpps() {
  const { t } = window.useApp();
  const M = window.MOCK;
  return (
    <div style={{ paddingTop: 8 }}>
      <div style={{ fontSize: 18, fontWeight: 600, color: t.text, marginBottom: 12, padding: '0 4px' }}>Opportunities</div>
      {M.opportunities.slice(0, 8).map((o, i) => (
        <div key={i} style={{ background: t.bgCard, border: `1px solid ${t.border}`, borderRadius: 12, padding: 14, marginBottom: 8 }}>
          <div style={{ display:'flex', alignItems:'flex-start', gap: 10, marginBottom: 8 }}>
            <div style={{ flex: 1 }}>
              <div style={{ fontSize: 12.5, color: t.text, lineHeight: 1.35, marginBottom: 4 }}>{o.description}</div>
              <div style={{ fontSize: 10, color: t.textMuted, fontFamily: window.FONTS.mono }}>{o.canonical_id}</div>
            </div>
            <div style={{ textAlign:'right', flexShrink: 0 }}>
              <div style={{ fontSize: 14, fontWeight: 700, color: o.net_edge_cents >= 2 ? t.green : t.amber, fontFamily: window.FONTS.mono }}>{window.fmtC(o.net_edge_cents)}</div>
              <div style={{ fontSize: 10, color: t.textDim, fontFamily: window.FONTS.mono }}>+{window.fmt$(o.expected_profit)}</div>
            </div>
          </div>
          <div style={{ display:'flex', alignItems:'center', flexWrap:'wrap', gap: 6, paddingTop: 10, borderTop: `1px solid ${t.border}` }}>
            <window.PlatformChip name={o.yes_platform} side="Y"/>
            <window.PlatformChip name={o.no_platform} side="N"/>
            <window.Pill tone={o.status === 'tradable' ? 'green' : 'blue'} size="sm">{o.status}</window.Pill>
          </div>
        </div>
      ))}
    </div>
  );
}

function MobTrades() {
  const { t, setModal } = window.useApp();
  const M = window.MOCK;
  return (
    <div style={{ paddingTop: 8 }}>
      <div style={{ fontSize: 18, fontWeight: 600, color: t.text, marginBottom: 12, padding: '0 4px' }}>Trades</div>
      {M.executions.slice(0, 8).map((r, i) => (
        <button type="button" key={i} onClick={() => setModal({ kind:'trade', payload: r })} aria-label={`Open trade ${r.arb_id || i}`} style={{ width:'100%', textAlign:'left', background: t.bgCard, border: `1px solid ${t.border}`, borderRadius: 12, padding: 14, marginBottom: 8, color: t.text, cursor:'pointer', display:'block' }}>
          <div style={{ display:'flex', alignItems:'center', gap: 8, marginBottom: 6 }}>
            <span style={{ fontSize: 11, fontFamily: window.FONTS.mono, fontWeight: 700, color: t.text }}>{r.arb_id}</span>
            <window.Pill tone={r.status === 'filled' ? 'green' : r.status === 'recovering' ? 'amber' : 'red'} size="sm">{r.status}</window.Pill>
            <span style={{ marginLeft:'auto', fontSize: 11, color: t.textDim, fontFamily: window.FONTS.mono }}>{window.ago(r.timestamp)}</span>
          </div>
          <div style={{ fontSize: 12, color: t.text, marginBottom: 8, lineHeight: 1.35 }}>{r.opportunity.description}</div>
          <div style={{ display:'flex', alignItems:'center', justifyContent:'space-between', paddingTop: 8, borderTop: `1px solid ${t.border}` }}>
            <span style={{ fontSize: 10.5, color: t.textMuted, fontFamily: window.FONTS.mono }}>{r.leg_yes.fill_qty} pairs</span>
            <span style={{ fontSize: 13, fontWeight: 700, color: r.realized_pnl >= 0 ? t.green : t.red, fontFamily: window.FONTS.mono }}>{window.fmt$Sign(r.realized_pnl)}</span>
          </div>
        </button>
      ))}
    </div>
  );
}

function MobMe({ onSub }) {
  const { t, name, toggle, setModal, signOut, profile } = window.useApp();
  return (
    <div style={{ paddingTop: 16 }}>
      <div onClick={() => setModal({ kind:'profile' })} style={{ display:'flex', alignItems:'center', gap: 12, marginBottom: 22, padding: '12px 4px', cursor:'pointer' }}>
        <div style={{ width: 48, height: 48, borderRadius: 12, background: t.accent, color:'#fff', display:'flex', alignItems:'center', justifyContent:'center', fontSize: 18, fontWeight: 700 }}>{profile.initials}</div>
        <div style={{ flex: 1 }}>
          <div style={{ fontSize: 15, fontWeight: 600, color: t.text }}>{profile.name}</div>
          <div style={{ fontSize: 11.5, color: t.textDim }}>{profile.email}</div>
        </div>
        <span style={{ color: t.textMuted, fontSize: 13 }}>›</span>
      </div>

      <div style={{ background: t.bgCard, border: `1px solid ${t.border}`, borderRadius: 12, overflow:'hidden', marginBottom: 12 }}>
        <MobRow icon="◐" label="Funds & deposits" sub="$1,099.50 across both platforms" onClick={() => onSub('funds')}/>
        <MobRow icon="$" label="P&L" sub="−$0.50 trading · $100 deposits" onClick={() => onSub('pnl')}/>
        <MobRow icon="✓" label="System health" sub="7/7 readiness gates" tone="green" onClick={() => onSub('health')}/>
        <MobRow icon="⚙" label="Settings" onClick={() => onSub('settings')} last/>
      </div>

      <div style={{ background: t.bgCard, border: `1px solid ${t.border}`, borderRadius: 12, overflow:'hidden', marginBottom: 12 }}>
        <MobRow icon={name === 'light' ? '☾' : '☀'} label={name === 'light' ? 'Dark mode' : 'Light mode'} onClick={toggle}/>
        <MobRow icon="⏏" label="Sign out" tone="red" onClick={signOut} last/>
      </div>

      <div style={{ textAlign:'center', fontSize: 10, color: t.textMuted, marginTop: 16, fontFamily: window.FONTS.mono }}>Arbiter v2.4.1 · build 2026.04.27</div>
    </div>
  );
}

// ── Mobile sub-screens (Funds, P&L, Health, Settings) ──────────────
function MobFunds() {
  const { t, setModal } = window.useApp();
  const M = window.MOCK;
  const total = M.balances.kalshi.balance + M.balances.polymarket.balance;
  // Available = balance minus a synthetic ~7% locked-in-trades reserve
  const kBal = M.balances.kalshi.balance, pBal = M.balances.polymarket.balance;
  const platforms = [
    { name: 'Kalshi', dot: t.green, bal: kBal, avail: +(kBal * 0.93).toFixed(2), locked: +(kBal * 0.07).toFixed(2) },
    { name: 'Polymarket', dot: t.purple, bal: pBal, avail: +(pBal * 0.93).toFixed(2), locked: +(pBal * 0.07).toFixed(2) },
  ];
  return (
    <div style={{ paddingTop: 8 }}>
      <div style={{ padding: '6px 4px 18px' }}>
        <div style={{ fontSize: 11, color: t.textMuted, letterSpacing:'0.06em', textTransform:'uppercase' }}>Total balance</div>
        <div style={{ fontSize: 32, fontWeight: 600, fontFamily: window.FONTS.mono, letterSpacing:'-0.02em', marginTop: 4 }}>{window.fmt$(total)}</div>
        <div style={{ fontSize: 11.5, color: t.textDim, marginTop: 4 }}>Across {platforms.length} connected platforms</div>
      </div>

      {platforms.map((p, i) => (
        <div key={i} style={{ background: t.bgCard, border: `1px solid ${t.border}`, borderRadius: 12, padding: 14, marginBottom: 10 }}>
          <div style={{ display:'flex', alignItems:'center', gap: 8, marginBottom: 12 }}>
            <span style={{ width: 8, height: 8, borderRadius:'50%', background: p.dot }}/>
            <span style={{ fontSize: 13, fontWeight: 600 }}>{p.name}</span>
            <span style={{ marginLeft:'auto', fontSize: 14, fontFamily: window.FONTS.mono, fontWeight: 700 }}>{window.fmt$(p.bal)}</span>
          </div>
          <div style={{ display:'grid', gridTemplateColumns:'1fr 1fr', gap: 8, marginBottom: 12 }}>
            <div style={{ background: t.bgSubtle, padding: '8px 10px', borderRadius: 7 }}>
              <div style={{ fontSize: 9.5, color: t.textMuted, letterSpacing:'0.06em', textTransform:'uppercase' }}>Available</div>
              <div style={{ fontSize: 13, fontFamily: window.FONTS.mono, fontWeight: 600, marginTop: 2 }}>{window.fmt$(p.avail)}</div>
            </div>
            <div style={{ background: t.bgSubtle, padding: '8px 10px', borderRadius: 7 }}>
              <div style={{ fontSize: 9.5, color: t.textMuted, letterSpacing:'0.06em', textTransform:'uppercase' }}>In trades</div>
              <div style={{ fontSize: 13, fontFamily: window.FONTS.mono, fontWeight: 600, marginTop: 2 }}>{window.fmt$(p.locked)}</div>
            </div>
          </div>
          <div style={{ display:'flex', gap: 6 }}>
            <button onClick={() => setModal({ kind:'deposit', payload: p.name.toLowerCase() })} style={{ flex: 1, padding: '9px', background: t.text, color: t.bgCard, border:'none', borderRadius: 7, fontSize: 12, fontWeight: 600, cursor:'pointer' }}>Deposit</button>
            <button onClick={() => setModal({ kind:'withdraw', payload: p.name.toLowerCase() })} style={{ flex: 1, padding: '9px', background:'transparent', border:`1px solid ${t.border}`, borderRadius: 7, fontSize: 12, fontWeight: 600, color: t.text, cursor:'pointer' }}>Withdraw</button>
          </div>
        </div>
      ))}

      <div style={{ fontSize: 11, color: t.textMuted, letterSpacing:'0.08em', padding: '12px 4px 8px' }}>RECENT DEPOSITS</div>
      {M.deposits.deposits.slice(0, 4).map((d, i) => (
        <div key={i} style={{ display:'flex', alignItems:'center', justifyContent:'space-between', padding: '11px 14px', background: t.bgCard, border: `1px solid ${t.border}`, borderRadius: 10, marginBottom: 6 }}>
          <div>
            <div style={{ fontSize: 12, fontWeight: 500, textTransform:'capitalize' }}>{d.platform} · {d.type}</div>
            <div style={{ fontSize: 10.5, color: t.textDim, fontFamily: window.FONTS.mono, marginTop: 1 }}>{window.tsDate(d.timestamp)}</div>
          </div>
          <div style={{ fontSize: 13, fontFamily: window.FONTS.mono, fontWeight: 600, color: d.amount > 0 ? t.green : t.text }}>{window.fmt$Sign(d.amount)}</div>
        </div>
      ))}
    </div>
  );
}

function MobPnL() {
  const { t } = window.useApp();
  const M = window.MOCK;
  const tradePnl = M.pnl.recorded_trading_pnl.kalshi + M.pnl.recorded_trading_pnl.polymarket;
  const dep = M.pnl.total_deposits_all_platforms;
  // Synthetic fees total — sum of ~2% on filled-trade notional
  const fees = M.executions.filter(e => e.status === 'filled').reduce((s, e) => s + (e.leg_yes.fill_qty * e.leg_yes.fill_price + e.leg_no.fill_qty * e.leg_no.fill_price) * 0.022, 0);
  return (
    <div style={{ paddingTop: 8 }}>
      <div style={{ padding: '6px 4px 18px' }}>
        <div style={{ fontSize: 11, color: t.textMuted, letterSpacing:'0.06em', textTransform:'uppercase' }}>Trading P&L</div>
        <div style={{ fontSize: 32, fontWeight: 600, fontFamily: window.FONTS.mono, letterSpacing:'-0.02em', marginTop: 4, color: tradePnl >= 0 ? t.green : t.red }}>{window.fmt$Sign(tradePnl)}</div>
        <div style={{ fontSize: 11.5, color: t.textDim, marginTop: 4 }}>Net of fees · since inception</div>
      </div>

      <div style={{ background: t.bgCard, border:`1px solid ${t.border}`, borderRadius: 12, padding: 14, marginBottom: 10 }}>
        <div style={{ display:'flex', justifyContent:'space-between', marginBottom: 10 }}>
          <span style={{ fontSize: 12, fontWeight: 600 }}>Equity curve</span>
          <span style={{ fontSize: 10.5, color: t.textDim, fontFamily: window.FONTS.mono }}>30d</span>
        </div>
        <div style={{ height: 130 }}>
          <window.AreaChart data={M.equity} stroke={t.accent} grid={t.border} showAxis={false}/>
        </div>
      </div>

      <div style={{ display:'grid', gridTemplateColumns:'1fr 1fr', gap: 8, marginBottom: 12 }}>
        <PnLStat label="Kalshi P&L" value={M.pnl.recorded_trading_pnl.kalshi} t={t}/>
        <PnLStat label="Polymarket P&L" value={M.pnl.recorded_trading_pnl.polymarket} t={t}/>
        <PnLStat label="Deposits" value={dep} t={t} neutral/>
        <PnLStat label="Fees paid" value={-fees} t={t}/>
      </div>

      <div style={{ background: t.bgCard, border:`1px solid ${t.border}`, borderRadius: 12, padding: 14 }}>
        <div style={{ fontSize: 12, fontWeight: 600, marginBottom: 10 }}>Win rate breakdown</div>
        {[
          ['Filled & profitable', 71, t.green],
          ['Filled & break-even', 18, t.textDim],
          ['Recovering / partial', 8, t.amber],
          ['Failed', 3, t.red],
        ].map(([l, v, c]) => (
          <div key={l} style={{ marginBottom: 8 }}>
            <div style={{ display:'flex', justifyContent:'space-between', fontSize: 11, marginBottom: 3 }}>
              <span>{l}</span><span style={{ fontFamily: window.FONTS.mono, color: c }}>{v}%</span>
            </div>
            <div style={{ height: 5, background: t.bgSubtle, borderRadius: 99, overflow:'hidden' }}>
              <div style={{ height:'100%', width: `${v}%`, background: c }}/>
            </div>
          </div>
        ))}
      </div>
    </div>
  );
}

function PnLStat({ label, value, t, neutral }) {
  const tone = neutral ? t.text : value >= 0 ? t.green : t.red;
  return (
    <div style={{ background: t.bgCard, border:`1px solid ${t.border}`, borderRadius: 10, padding: 12 }}>
      <div style={{ fontSize: 10, color: t.textMuted, letterSpacing:'0.06em', textTransform:'uppercase' }}>{label}</div>
      <div style={{ fontSize: 16, fontWeight: 600, fontFamily: window.FONTS.mono, color: tone, marginTop: 4 }}>{neutral ? window.fmt$(value) : window.fmt$Sign(value)}</div>
    </div>
  );
}

function MobHealth() {
  const { t, scanner, setScanner } = window.useApp();
  const M = window.MOCK;
  const gates = [
    { l: 'Scanner heartbeat', v: 'Live · 1s ago', ok: scanner },
    { l: 'Kalshi auth', v: 'Connected · 8h', ok: true },
    { l: 'Polymarket auth', v: 'Connected · 8h', ok: true },
    { l: 'Risk limits', v: '$200 / $500 daily', ok: true },
    { l: 'Liquidity gate', v: 'Pass · $1,099 ≥ $200', ok: true },
    { l: 'Auto-execute', v: 'Armed', ok: true },
    { l: 'Webhooks', v: 'No errors · 24h', ok: true },
  ];
  return (
    <div style={{ paddingTop: 8 }}>
      <div style={{ background: t.greenSoft, border: `1px solid ${t.green}30`, borderRadius: 12, padding: 16, marginBottom: 12, display:'flex', alignItems:'center', gap: 12 }}>
        <div style={{ width: 40, height: 40, borderRadius:'50%', background: t.green, color:'#fff', display:'flex', alignItems:'center', justifyContent:'center', fontSize: 18 }}>✓</div>
        <div style={{ flex: 1 }}>
          <div style={{ fontSize: 14, fontWeight: 600, color: t.green }}>All systems operational</div>
          <div style={{ fontSize: 11.5, color: t.green, opacity: 0.8 }}>{gates.filter(g => g.ok).length}/{gates.length} readiness gates passing</div>
        </div>
      </div>

      <div style={{ background: t.bgCard, border:`1px solid ${t.border}`, borderRadius: 12, overflow:'hidden', marginBottom: 12 }}>
        {gates.map((g, i) => (
          <div key={i} style={{ padding: '13px 14px', borderBottom: i < gates.length - 1 ? `1px solid ${t.border}` : 'none', display:'flex', alignItems:'center', gap: 12 }}>
            <span style={{ width: 8, height: 8, borderRadius:'50%', background: g.ok ? t.green : t.red, flexShrink: 0 }}/>
            <div style={{ flex: 1, minWidth: 0 }}>
              <div style={{ fontSize: 12.5, color: t.text, fontWeight: 500 }}>{g.l}</div>
              <div style={{ fontSize: 10.5, color: t.textDim, fontFamily: window.FONTS.mono, marginTop: 1 }}>{g.v}</div>
            </div>
          </div>
        ))}
      </div>

      <div style={{ display:'grid', gridTemplateColumns:'1fr 1fr', gap: 8, marginBottom: 12 }}>
        <button onClick={() => setScanner(!scanner)} style={{ padding: '11px', background: scanner ? t.amberSoft : t.greenSoft, color: scanner ? t.amber : t.green, border: `1px solid ${scanner ? t.amber : t.green}40`, borderRadius: 10, fontSize: 12, fontWeight: 600, cursor:'pointer' }}>
          {scanner ? 'Pause scanner' : 'Resume scanner'}
        </button>
        <button style={{ padding: '11px', background: t.bgCard, border:`1px solid ${t.border}`, borderRadius: 10, fontSize: 12, fontWeight: 600, color: t.text, cursor:'pointer' }}>Run health check</button>
      </div>

      <div style={{ background: t.redSoft, border:`1px solid ${t.red}40`, borderRadius: 12, padding: 14 }}>
        <div style={{ fontSize: 12, fontWeight: 600, color: t.red, marginBottom: 4 }}>Emergency kill switch</div>
        <div style={{ fontSize: 11, color: t.red, opacity: 0.8, marginBottom: 10 }}>Halts scanner, cancels all open orders, locks trading. Requires confirmation.</div>
        <button style={{ width:'100%', padding: '10px', background: t.red, color:'#fff', border:'none', borderRadius: 8, fontSize: 12, fontWeight: 700, cursor:'pointer', letterSpacing:'0.04em' }}>HALT ALL TRADING</button>
      </div>
    </div>
  );
}

function MobSettings() {
  const { t, setModal, autoExec, setAutoExec, minEdge, setMinEdge, tradingMode, setTradingMode } = window.useApp();
  const M = window.MOCK;
  return (
    <div style={{ paddingTop: 8 }}>
      <div style={{ fontSize: 11, color: t.textMuted, letterSpacing:'0.08em', padding: '0 4px 8px' }}>TRADING MODE</div>
      <div style={{ background: t.bgCard, border:`1px solid ${t.border}`, borderRadius: 12, padding: 4, display:'flex', gap: 4, marginBottom: 16 }}>
        {['live','standby','paper'].map(m => (
          <button key={m} onClick={() => setTradingMode(m)} style={{ flex: 1, padding: '10px', background: tradingMode === m ? t.text : 'transparent', color: tradingMode === m ? t.bgCard : t.textDim, border:'none', borderRadius: 8, fontSize: 11.5, fontWeight: 600, cursor:'pointer', textTransform:'capitalize' }}>{m}</button>
        ))}
      </div>

      <div style={{ fontSize: 11, color: t.textMuted, letterSpacing:'0.08em', padding: '0 4px 8px' }}>STRATEGY</div>
      <div style={{ background: t.bgCard, border:`1px solid ${t.border}`, borderRadius: 12, overflow:'hidden', marginBottom: 16 }}>
        <div style={{ padding: '14px 16px', borderBottom:`1px solid ${t.border}`, display:'flex', alignItems:'center', justifyContent:'space-between' }}>
          <div style={{ flex: 1 }}>
            <div style={{ fontSize: 13, fontWeight: 500 }}>Auto-execute</div>
            <div style={{ fontSize: 11, color: t.textDim, marginTop: 1 }}>Auto-fire when all gates pass</div>
          </div>
          <Switch on={autoExec} onChange={() => setAutoExec(!autoExec)}/>
        </div>
        <MobRow icon="◇" label="Min edge threshold" sub={`${minEdge.toFixed(1)}¢ — tap to edit`} onClick={() => setModal({ kind:'editSetting', payload: { key:'minEdge', label:'Min edge', kind:'slider', min:1, max:6, step:0.1, value: minEdge, onSave: (v) => setMinEdge(v) } })}/>
        <MobRow icon="$" label="Max position size" sub="$200 per leg" onClick={() => setModal({ kind:'editSetting', payload: { key:'maxPos', label:'Max position', kind:'slider', min:50, max:500, step:25, value: 200, prefix:'$' } })}/>
        <MobRow icon="⚐" label="Daily loss limit" sub="$500 hard stop" onClick={() => setModal({ kind:'editSetting', payload: { key:'dailyLoss', label:'Daily loss limit', kind:'slider', min:100, max:2000, step:50, value: 500, prefix:'$' } })} last/>
      </div>

      <div style={{ fontSize: 11, color: t.textMuted, letterSpacing:'0.08em', padding: '0 4px 8px' }}>CONNECTIONS</div>
      <div style={{ background: t.bgCard, border:`1px solid ${t.border}`, borderRadius: 12, overflow:'hidden', marginBottom: 16 }}>
        <MobRow icon="●" label="Kalshi API" sub="Connected · keys rotate Jul 14" tone="green" onClick={() => setModal({ kind:'apiKey', payload: 'kalshi' })}/>
        <MobRow icon="●" label="Polymarket API" sub="Connected · keys rotate Aug 02" tone="green" onClick={() => setModal({ kind:'apiKey', payload: 'polymarket' })} last/>
      </div>

      <div style={{ fontSize: 11, color: t.textMuted, letterSpacing:'0.08em', padding: '0 4px 8px' }}>NOTIFICATIONS</div>
      <div style={{ background: t.bgCard, border:`1px solid ${t.border}`, borderRadius: 12, overflow:'hidden', marginBottom: 16 }}>
        <MobRow icon="✉" label="Email alerts" sub="Fills, errors, kill events" onClick={() => setModal({ kind:'notifPrefs' })}/>
        <MobRow icon="◔" label="Push alerts" sub="High-edge opps (>4¢)" onClick={() => setModal({ kind:'notifPrefs' })} last/>
      </div>

      <div style={{ fontSize: 11, color: t.textMuted, letterSpacing:'0.08em', padding: '0 4px 8px' }}>SECURITY</div>
      <div style={{ background: t.bgCard, border:`1px solid ${t.border}`, borderRadius: 12, overflow:'hidden', marginBottom: 16 }}>
        <MobRow icon="◈" label="Two-factor auth" sub="Authenticator · enabled" tone="green" onClick={() => setModal({ kind:'reset2fa' })}/>
        <MobRow icon="◌" label="Session timeout" sub="30 minutes" onClick={() => setModal({ kind:'editSetting', payload: { key:'sessionTimeout', label:'Session timeout', kind:'options', value:30, options:[{v:15,l:'15 min'},{v:30,l:'30 min'},{v:60,l:'1 hour'},{v:240,l:'4 hours'}] } })} last/>
      </div>
    </div>
  );
}

function Switch({ on, onChange }) {
  const { t } = window.useApp();
  return (
    <button onClick={onChange} style={{ width: 38, height: 22, background: on ? t.green : t.bgSubtle, border:`1px solid ${on ? t.green : t.border}`, borderRadius: 99, padding: 0, position:'relative', cursor:'pointer', transition:'all 0.15s', flexShrink: 0 }}>
      <span style={{ position:'absolute', top: 2, left: on ? 18 : 2, width: 16, height: 16, background:'#fff', borderRadius:'50%', transition:'all 0.15s', boxShadow:'0 1px 2px rgba(0,0,0,0.2)' }}/>
    </button>
  );
}

function MobRow({ icon, label, sub, tone, onClick, last }) {
  const { t } = window.useApp();
  const c = tone === 'green' ? t.green : tone === 'red' ? t.red : t.text;
  return (
    <div onClick={onClick} style={{ padding: '14px 16px', borderBottom: last ? 'none' : `1px solid ${t.border}`, display:'flex', alignItems:'center', gap: 12, cursor: onClick ? 'pointer' : 'default' }}>
      <div style={{ width: 30, height: 30, borderRadius: 7, background: t.bgSubtle, color: c, display:'flex', alignItems:'center', justifyContent:'center', fontSize: 13, fontFamily: window.FONTS.mono }}>{icon}</div>
      <div style={{ flex: 1 }}>
        <div style={{ fontSize: 13, color: c, fontWeight: 500 }}>{label}</div>
        {sub && <div style={{ fontSize: 11, color: t.textDim, marginTop: 1 }}>{sub}</div>}
      </div>
      <span style={{ color: t.textMuted, fontSize: 13 }}>›</span>
    </div>
  );
}

function MobMappings() {
  const { t, setModal, toast } = window.useApp();
  // ⚠ BACKEND NOTE: same source as desktop PageMappings — should pull from
  // /api/mappings/candidates. Keep mobile + desktop fed by the same endpoint
  // so verdicts apply across both.
  const candidates = [
    { score: 0.97, k: 'TRUMP-2028-NOM-Y', p: 'will-trump-win-2028-gop-nomination', s: 'confirmed' },
    { score: 0.94, k: 'BTC-150K-DEC2026', p: 'will-bitcoin-reach-150k-by-2026', s: 'confirmed' },
    { score: 0.89, k: 'FED-JUN26-CUT-Y', p: 'fed-rate-cut-june-2026-meeting', s: 'pending' },
    { score: 0.82, k: 'GPT-6-RELEASE-Q4', p: 'gpt-6-released-before-q4-2026', s: 'pending' },
    { score: 0.71, k: 'SENATE-DEM-26', p: 'democrats-control-senate-after-2026-midterms', s: 'review' },
    { score: 0.64, k: 'AAPL-AR-GLASS-26', p: 'apple-launches-ar-headset-2026', s: 'review' },
    { score: 0.43, k: 'OPENAI-DOD-26', p: 'openai-government-defense-deal-2026', s: 'rejected' },
  ];
  const reviewable = candidates.filter(c => c.s === 'pending' || c.s === 'review');
  return (
    <div style={{ paddingTop: 8 }}>
      <div style={{ fontSize: 18, fontWeight: 600, color: t.text, marginBottom: 4, padding: '0 4px' }}>Mappings</div>
      <div style={{ fontSize: 11.5, color: t.textDim, marginBottom: 14, padding: '0 4px' }}>312 confirmed · {reviewable.length} pending agent review</div>

      {/* Action buttons — full feature parity with desktop */}
      <div style={{ display:'grid', gridTemplateColumns:'1fr 1fr', gap: 8, marginBottom: 14 }}>
        <button onClick={() => setModal({ kind:'refetchMappings' })} style={{ padding: '11px', background: t.bgCard, border:`1px solid ${t.border}`, borderRadius: 10, fontSize: 12, color: t.text, fontWeight: 600, cursor:'pointer', display:'flex', alignItems:'center', justifyContent:'center', gap: 6 }}>
          <span style={{ fontFamily: window.FONTS.mono }}>↻</span> Re-fetch
        </button>
        <button onClick={() => { const f = reviewable[0]; if (f) setModal({ kind:'agentValidate', payload: f }); else toast('No candidates pending'); }} style={{ padding: '11px', background: t.accent, border:'none', borderRadius: 10, fontSize: 12, color:'#fff', fontWeight: 600, cursor:'pointer', display:'flex', alignItems:'center', justifyContent:'center', gap: 6 }}>
          <span style={{ fontFamily: window.FONTS.mono }}>◇</span> Validate all
        </button>
      </div>

      {/* Candidates list */}
      {candidates.map((c, i) => {
        const tone = c.s === 'confirmed' ? 'green' : c.s === 'pending' ? 'amber' : c.s === 'review' ? 'blue' : 'red';
        const actionable = c.s === 'pending' || c.s === 'review';
        const openCard = () => setModal({ kind:'agentValidate', payload: c });
        return (
          <div key={i} role="button" tabIndex={0} aria-label={`Open mapping validation ${c.k || i}`} onClick={openCard} onKeyDown={(e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); openCard(); } }} style={{ background: t.bgCard, border: `1px solid ${t.border}`, borderRadius: 12, padding: 14, marginBottom: 8, cursor: 'pointer' }}>
            <div style={{ display:'flex', alignItems:'center', gap: 10, marginBottom: 8 }}>
              <div style={{ width: 36, height: 36, borderRadius:'50%', background: c.score >= 0.9 ? t.greenSoft : c.score >= 0.7 ? t.amberSoft : t.redSoft, display:'flex', alignItems:'center', justifyContent:'center', fontSize: 11.5, fontWeight: 700, color: c.score >= 0.9 ? t.green : c.score >= 0.7 ? t.amber : t.red, fontFamily: window.FONTS.mono, flexShrink: 0 }}>{c.score.toFixed(2)}</div>
              <div style={{ flex: 1, minWidth: 0 }}>
                <div style={{ fontSize: 11.5, color: t.text, fontFamily: window.FONTS.mono, fontWeight: 600, whiteSpace:'nowrap', overflow:'hidden', textOverflow:'ellipsis' }}>{c.k}</div>
                <div style={{ fontSize: 10.5, color: t.textDim, fontFamily: window.FONTS.mono, whiteSpace:'nowrap', overflow:'hidden', textOverflow:'ellipsis' }}>⇄ {c.p}</div>
              </div>
              <window.Pill tone={tone} size="sm">{c.s}</window.Pill>
            </div>
            {actionable && (
              <div style={{ display:'flex', gap: 6, paddingTop: 8, borderTop: `1px solid ${t.border}` }}>
                <button onClick={(e) => { e.stopPropagation(); toast('Mapping rejected', { sub: c.k }); }} style={{ flex: 1, padding: '8px', background:'transparent', border:`1px solid ${t.border}`, borderRadius: 7, fontSize: 11.5, color: t.textDim, fontWeight: 500, cursor:'pointer' }}>Reject</button>
                <button onClick={(e) => { e.stopPropagation(); setModal({ kind:'agentValidate', payload: c }); }} style={{ flex: 1, padding: '8px', background: t.bgSubtle, border:`1px solid ${t.border}`, borderRadius: 7, fontSize: 11.5, color: t.text, fontWeight: 600, cursor:'pointer' }}>◇ Validate</button>
              </div>
            )}
          </div>
        );
      })}

      <div style={{ marginTop: 12, padding: '10px 12px', background: t.bgSubtle, border:`1px dashed ${t.border}`, borderRadius: 8, fontSize: 10.5, color: t.textDim, lineHeight: 1.5 }}>
        Tap any mapping to inspect validation history and run a read-only Claude Opus 4.7 verifier pass with every tool call and verdict.
      </div>
    </div>
  );
}

window.MobileDashboard = MobileDashboard;
