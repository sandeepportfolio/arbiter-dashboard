// Overview page — landing dashboard.
const { useState: oUseState } = React;

function PageOverview() {
  const { t, setPage, setDrawer, scanner, minEdge, setMinEdge, autoExec, setAutoExec } = window.useApp();
  const M = window.MOCK;
  const totalBal = M.balances.kalshi.balance + M.balances.polymarket.balance;
  const totalPnl = M.pnl.recorded_trading_pnl.kalshi + M.pnl.recorded_trading_pnl.polymarket;
  const tradable = M.opportunities.filter(o => o.status === 'tradable');
  const last24Pnl = M.executions.reduce((s, e) => s + (e.realized_pnl || 0), 0);

  return (
    <div style={{ padding: '28px 32px', maxWidth: 1400, margin: '0 auto' }}>
      <window.PageHeader kicker="Operator desk" title="Overview" sub="Live arbitrage activity across Kalshi and Polymarket. The scanner is monitoring 312 mapped markets.">
        <window.Btn variant="secondary" size="sm" icon="↓" onClick={() => setPage('deposits')}>Deposit</window.Btn>
        <window.Btn variant="primary" size="sm" icon="◇" onClick={() => setPage('opportunities')}>View opportunities</window.Btn>
      </window.PageHeader>

      {/* KPI strip */}
      <div style={{ display:'grid', gridTemplateColumns: 'repeat(4, 1fr)', gap: 14, marginBottom: 18 }}>
        <KpiCard label="Total balance" value={window.fmt$(totalBal)} sub="Live venue balances" sparkData={M.equity.slice(-48)} sparkColor={t.accent} platformBalances={[
          { name: 'Kalshi', value: window.fmt$(M.balances.kalshi.balance), color: t.green, title: `Kalshi balance ${window.fmt$(M.balances.kalshi.balance)}` },
          { name: 'Polymarket', value: window.fmt$(M.balances.polymarket.balance), color: t.purple, title: `Polymarket balance ${window.fmt$(M.balances.polymarket.balance)}` },
        ]} />
        <KpiCard label="Trading P&L" value={window.fmt$Sign(totalPnl)} sub="Net of fees · since inception" tone={totalPnl >= 0 ? 'green' : 'red'} sparkData={M.equity.slice(-48).map(d => ({ ...d, v: d.v - 1000 }))} sparkColor={totalPnl >= 0 ? t.green : t.red} />
        <KpiCard label="Live opportunities" value={tradable.length} sub={`${M.opportunities.length - tradable.length} candidates · best ${window.fmtC(M.opportunities[0].net_edge_cents)}`} tone="accent" sparkData={M.health.scanner.history.slice(-30).map(h => ({ v: h.tradable }))} sparkColor={t.accent} />
        <KpiCard label="24h fills" value={M.executions.filter(e => e.status === 'filled').length} sub={`${window.fmt$Sign(last24Pnl)} realized`} tone={last24Pnl >= 0 ? 'green' : 'red'} sparkData={M.equity.slice(-48)} sparkColor={t.green} />
      </div>

      {/* Equity + opportunities */}
      <div style={{ display: 'grid', gridTemplateColumns: '1.65fr 1fr', gap: 14, marginBottom: 18 }}>
        <window.Card title="Equity curve" action={<TimeRange/>} padding={0}>
          <div style={{ padding: '18px 22px 8px', display:'flex', alignItems:'baseline', gap: 16 }}>
            <div>
              <div style={{ fontSize: 28, fontWeight: 600, color: t.text, letterSpacing:'-0.02em', fontFamily: window.FONTS.mono }}>{window.fmt$(M.equity[M.equity.length-1].v)}</div>
              <div style={{ fontSize: 11.5, color: totalPnl >= 0 ? t.green : t.red, marginTop: 2 }}>{window.fmt$Sign(totalPnl)} ({window.fmtPct(totalPnl/1000)}) · 24h</div>
            </div>
            <div style={{ marginLeft: 'auto', fontSize: 11, color: t.textMuted }}>Includes $100 deposit at 14:32</div>
          </div>
          <div style={{ height: 240, padding: '0 12px 12px' }}>
            <window.AreaChart data={M.equity} width={780} height={240} stroke={t.accent} grid={t.border} accentDot={true}/>
          </div>
        </window.Card>

        <window.Card title="Top opportunities" action={<a onClick={() => setPage('opportunities')} style={{ fontSize: 11, color: t.accent, cursor:'pointer', textDecoration: 'none' }}>View all →</a>} padding={0}>
          <div>
            {M.opportunities.slice(0, 5).map((o, i) => (
              <div key={i} onClick={() => { setPage('opportunities'); setDrawer({ kind: 'opp', payload: i }); }} style={{
                padding: '14px 18px', borderBottom: i < 4 ? `1px solid ${t.border}` : 'none', cursor:'pointer',
              }} onMouseEnter={e => e.currentTarget.style.background = t.bgSubtle} onMouseLeave={e => e.currentTarget.style.background = 'transparent'}>
                <div style={{ display:'flex', alignItems:'center', justifyContent:'space-between', gap: 12, marginBottom: 6 }}>
                  <div style={{ fontSize: 12.5, color: t.text, fontWeight: 500, lineHeight: 1.35, flex: 1, minWidth: 0, overflow:'hidden', textOverflow:'ellipsis', whiteSpace:'nowrap' }}>{o.description}</div>
                  <div style={{ fontSize: 13, fontWeight: 600, color: o.net_edge_cents >= 2 ? t.green : t.amber, fontFamily: window.FONTS.mono, flexShrink: 0 }}>{window.fmtC(o.net_edge_cents)}</div>
                </div>
                <div style={{ display:'flex', alignItems:'center', gap: 8, fontSize: 10.5, color: t.textDim }}>
                  <window.PlatformChip name={o.yes_platform} side="YES"/>
                  <window.PlatformChip name={o.no_platform} side="NO"/>
                  <span style={{ marginLeft:'auto', fontFamily: window.FONTS.mono }}>+{window.fmt$(o.expected_profit)}</span>
                </div>
              </div>
            ))}
          </div>
        </window.Card>
      </div>

      {/* Bottom row: balances breakdown + scanner activity + edge histogram */}
      <div style={{ display: 'grid', gridTemplateColumns: '1fr 1.3fr 1fr', gap: 14, marginBottom: 18 }}>
        <window.Card title="Balance composition" padding={20}>
          <div style={{ display:'flex', alignItems:'center', gap: 16 }}>
            <div style={{ width: 110, height: 110, flexShrink: 0 }}>
              <window.Donut size={110} thickness={14} slices={[
                { value: M.balances.kalshi.balance, color: t.green },
                { value: M.balances.polymarket.balance, color: t.purple },
              ]} label={window.fmt$(totalBal, 0)} sublabel="TOTAL" labelColor={t.text} dimColor={t.textMuted}/>
            </div>
            <div style={{ flex: 1 }}>
              <Row dot={t.green} label="Kalshi" value={window.fmt$(M.balances.kalshi.balance)} sub={`${(M.balances.kalshi.balance/totalBal*100).toFixed(1)}% · ${window.fmt$Sign(M.pnl.recorded_trading_pnl.kalshi)}`}/>
              <div style={{ height: 12 }}/>
              <Row dot={t.purple} label="Polymarket" value={window.fmt$(M.balances.polymarket.balance)} sub={`${(M.balances.polymarket.balance/totalBal*100).toFixed(1)}% · ${window.fmt$Sign(M.pnl.recorded_trading_pnl.polymarket)}`}/>
            </div>
          </div>
        </window.Card>

        <window.Card title="Scanner activity" action={<span style={{ fontSize: 10.5, color: scanner ? t.green : t.textMuted, fontFamily: window.FONTS.mono }}>{scanner ? 'LIVE' : 'PAUSED'}</span>} padding={0}>
          <div style={{ padding: '14px 18px', display:'grid', gridTemplateColumns: 'repeat(4, 1fr)', gap: 14 }}>
            <Mini label="Scans (24h)" value="18,420" mono/>
            <Mini label="Avg latency" value="214ms" mono/>
            <Mini label="Published" value="312" mono/>
            <Mini label="Best edge" value={window.fmtC(M.opportunities[0].net_edge_cents)} mono tone="green"/>
          </div>
          <div style={{ padding: '0 18px 14px' }}>
            <div style={{ fontSize: 10, color: t.textMuted, letterSpacing: '0.08em', marginBottom: 6 }}>BEST EDGE — LAST 5 MIN, 5s BUCKETS</div>
            <div style={{ height: 56 }}>
              <window.EdgeStrip history={M.health.scanner.history} scale={[t.bgSubtle, t.accentSoft, t.accent + '90', t.green]}/>
            </div>
          </div>
        </window.Card>

        <window.Card title="Edge distribution" action={<span style={{ fontSize: 10.5, color: t.textMuted }}>NOW · 297 markets</span>} padding={20}>
          <div style={{ height: 180 }}>
            <window.BarChart data={M.edgeBuckets} color={t.bgHover} accent={t.accent} labelColor={t.textDim}/>
          </div>
        </window.Card>
      </div>

      {/* Recent activity */}
      <window.Card title="Recent activity" action={<a onClick={() => setPage('trades')} style={{ fontSize: 11, color: t.accent, cursor:'pointer' }}>All trades →</a>} padding={0}>
        <window.DataTable columns={[
          { label: 'Time', w: '90px', render: r => <span style={{ fontFamily: window.FONTS.mono, color: t.textDim, fontSize: 11.5 }}>{window.ago(r.timestamp)}</span> },
          { label: 'ID', w: '110px', render: r => <span style={{ fontFamily: window.FONTS.mono, color: t.text, fontSize: 11.5 }}>{r.arb_id}</span> },
          { label: 'Market', render: r => <span style={{ fontSize: 12.5 }}>{r.opportunity.description}</span> },
          { label: 'Status', w: '120px', render: r => <window.Pill tone={r.status === 'filled' ? 'green' : r.status === 'recovering' ? 'amber' : 'red'}>{r.status}</window.Pill> },
          { label: 'P&L', w: '110px', align: 'right', render: r => <span style={{ fontFamily: window.FONTS.mono, fontSize: 12.5, color: r.realized_pnl >= 0 ? t.green : t.red, fontWeight: 600 }}>{window.fmt$Sign(r.realized_pnl)}</span> },
        ]} rows={M.executions.slice(0, 5)} onRowClick={(r) => { setPage('trades'); }} />
      </window.Card>
    </div>
  );
}

function KpiCard({ label, value, sub, tone, sparkData, sparkColor, platformBalances }) {
  const { t } = window.useApp();
  const tones = { green: t.green, red: t.red, amber: t.amber, accent: t.accent };
  const hasPlatformBalances = Array.isArray(platformBalances) && platformBalances.length > 0;
  return (
    <div style={{ background: t.bgCard, border: `1px solid ${t.border}`, borderRadius: 10, padding: 18, position:'relative', overflow:'hidden' }}>
      <div style={{ fontSize: 11, color: t.textMuted, letterSpacing: '0.04em', textTransform: 'uppercase', marginBottom: 8 }}>{label}</div>
      <div style={{ display:'flex', alignItems:'center', justifyContent:'space-between', gap: 12 }}>
        <div style={{ fontSize: 24, fontWeight: 600, color: tones[tone] || t.text, letterSpacing:'-0.018em', fontFamily: window.FONTS.mono, whiteSpace:'nowrap', overflow:'hidden', textOverflow:'ellipsis' }}>{value}</div>
      </div>
      {hasPlatformBalances ? (
        <div style={{ display:'grid', gap: 6, marginTop: 10 }}>
          {platformBalances.map((row) => (
            <div key={row.name} title={row.title || `${row.name} ${row.value}`} style={{ display:'flex', alignItems:'center', justifyContent:'space-between', gap: 10, minWidth: 0, padding: '6px 8px', borderRadius: 7, background: t.bgSubtle, border: `1px solid ${t.border}` }}>
              <div style={{ display:'flex', alignItems:'center', gap: 7, minWidth: 0 }}>
                <span style={{ width: 7, height: 7, borderRadius: '50%', background: row.color, flexShrink: 0 }}/>
                <span style={{ fontSize: 11.5, color: t.textDim, whiteSpace:'nowrap' }}>{row.name}</span>
              </div>
              <span style={{ fontSize: 12.5, fontWeight: 600, color: t.text, fontFamily: window.FONTS.mono, whiteSpace:'nowrap' }}>{row.value}</span>
            </div>
          ))}
          {sub && <div style={{ fontSize: 10.5, color: t.textMuted, lineHeight: 1.35 }}>{sub}</div>}
        </div>
      ) : (
        <div style={{ fontSize: 11, color: t.textDim, marginTop: 4 }}>{sub}</div>
      )}
      {sparkData && !hasPlatformBalances && (
        <div style={{ position:'absolute', right: 14, top: 14, width: 80, height: 30, opacity: 0.7 }}>
          <window.Sparkline data={sparkData} width={80} height={30} stroke={sparkColor} fill={sparkColor + '22'}/>
        </div>
      )}
    </div>
  );
}

function Row({ dot, label, value, sub }) {
  const { t } = window.useApp();
  return (
    <div>
      <div style={{ display:'flex', alignItems:'center', gap: 8, marginBottom: 3 }}>
        <span style={{ width: 7, height: 7, borderRadius: '50%', background: dot }}/>
        <span style={{ fontSize: 12, color: t.textDim }}>{label}</span>
        <span style={{ marginLeft:'auto', fontSize: 13, fontWeight: 600, color: t.text, fontFamily: window.FONTS.mono }}>{value}</span>
      </div>
      <div style={{ fontSize: 10.5, color: t.textMuted, paddingLeft: 15 }}>{sub}</div>
    </div>
  );
}

function Mini({ label, value, mono, tone }) {
  const { t } = window.useApp();
  const c = tone === 'green' ? t.green : t.text;
  return (
    <div>
      <div style={{ fontSize: 10, color: t.textMuted, letterSpacing: '0.06em', textTransform:'uppercase', marginBottom: 4 }}>{label}</div>
      <div style={{ fontSize: 15, fontWeight: 600, color: c, fontFamily: mono ? window.FONTS.mono : window.FONTS.sans }}>{value}</div>
    </div>
  );
}

function TimeRange() {
  const { t } = window.useApp();
  const [r, setR] = oUseState('24h');
  return (
    <div style={{ display:'flex', background: t.bgSubtle, borderRadius: 6, padding: 2 }}>
      {['1h','6h','24h','7d','All'].map(x => (
        <button key={x} onClick={() => setR(x)} style={{ padding: '4px 10px', background: r === x ? t.bgCard : 'transparent', border: 'none', fontSize: 11, color: r === x ? t.text : t.textDim, fontWeight: r === x ? 500 : 400, borderRadius: 4, cursor:'pointer', boxShadow: r === x ? '0 1px 2px rgba(0,0,0,0.06)' : 'none' }}>{x}</button>
      ))}
    </div>
  );
}

window.PageOverview = PageOverview;
window.TimeRange = TimeRange;
