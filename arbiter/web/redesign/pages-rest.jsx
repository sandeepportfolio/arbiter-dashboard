// Trades + P&L + Markets + Mappings + Scanner + Audit + Funds + Settings
const { useState: pUseState, useMemo: pUseMemo } = React;

// ── Trades ──────────────────────────────────────────────────────────
function PageTrades() {
  const { t, setModal } = window.useApp();
  const M = window.MOCK;
  const [statusFilter, setStatusFilter] = pUseState('all');
  const filtered = statusFilter === 'all' ? M.executions : M.executions.filter(e => e.status === statusFilter);
  const totalPnl = M.executions.reduce((s, e) => s + e.realized_pnl, 0);
  const filled = M.executions.filter(e => e.status === 'filled').length;

  return (
    <div style={{ padding: '28px 32px', maxWidth: 1400, margin: '0 auto' }}>
      <window.PageHeader kicker="Execution ledger" title="Trades" sub="Every leg pair the executor has touched. Click a trade for the submission → fill → settle timeline."/>

      <div style={{ display:'grid', gridTemplateColumns:'repeat(4, 1fr)', gap: 14, marginBottom: 18 }}>
        <window.Card padding={18}><window.Stat label="Total trades" value={M.executions.length} sub={`${filled} filled · ${M.executions.length - filled} other`} mono/></window.Card>
        <window.Card padding={18}><window.Stat label="Realized P&L" value={window.fmt$Sign(totalPnl)} tone={totalPnl >= 0 ? 'green' : 'red'} sub="Net of fees, all platforms" mono/></window.Card>
        <window.Card padding={18}><window.Stat label="Fill rate" value={`${(filled/M.executions.length*100).toFixed(0)}%`} sub="Both legs filled within tolerance" mono/></window.Card>
        <window.Card padding={18}><window.Stat label="Avg edge captured" value="2.4¢" sub="vs 2.7¢ scanner edge" mono/></window.Card>
      </div>

      <div style={{ display:'flex', alignItems:'center', gap: 4, padding: 4, background: t.bgCard, border: `1px solid ${t.border}`, borderRadius: 9, marginBottom: 14, width:'fit-content' }}>
        {['all','filled','recovering','failed'].map(s => (
          <button key={s} onClick={() => setStatusFilter(s)} style={{ padding: '6px 12px', background: statusFilter === s ? t.bgSubtle : 'transparent', border:'none', fontSize: 12, color: statusFilter === s ? t.text : t.textDim, fontWeight: statusFilter === s ? 600 : 400, borderRadius: 6, cursor:'pointer', textTransform:'capitalize' }}>{s}</button>
        ))}
      </div>

      <window.Card padding={0}>
        <window.DataTable columns={[
          { label: 'Trade ID', w: '120px', render: r => <span style={{ fontFamily: window.FONTS.mono, fontSize: 12, color: t.text, fontWeight: 600 }}>{r.arb_id}</span> },
          { label: 'Time', w: '110px', render: r => <span style={{ fontFamily: window.FONTS.mono, color: t.textDim, fontSize: 11.5 }}>{window.ago(r.timestamp)}</span> },
          { label: 'Market', render: r => (
            <div>
              <div style={{ fontSize: 12.5, color: t.text, marginBottom: 2 }}>{r.opportunity.description}</div>
              <div style={{ fontSize: 10.5, color: t.textMuted, fontFamily: window.FONTS.mono }}>{r.opportunity.canonical_id}</div>
            </div>
          )},
          { label: 'YES', w: '160px', render: r => <LegCell leg={r.leg_yes} platform={r.opportunity.yes_platform}/> },
          { label: 'NO', w: '160px', render: r => <LegCell leg={r.leg_no} platform={r.opportunity.no_platform}/> },
          { label: 'Status', w: '100px', render: r => {
            const tone = r.status === 'filled' ? 'green' : r.status === 'recovering' ? 'amber' : 'red';
            return <window.Pill tone={tone}>{r.status}</window.Pill>;
          }},
          { label: 'P&L', w: '100px', align: 'right', render: r => <span style={{ fontFamily: window.FONTS.mono, fontSize: 12.5, color: r.realized_pnl > 0 ? t.green : r.realized_pnl < 0 ? t.red : t.textDim, fontWeight: 600 }}>{window.fmt$Sign(r.realized_pnl)}</span> },
        ]} rows={filtered} onRowClick={(r) => setModal({ kind: 'trade', payload: r })}/>
      </window.Card>
    </div>
  );
}

function LegCell({ leg, platform }) {
  const { t } = window.useApp();
  const tone = leg.status === 'filled' ? 'green' : leg.status === 'submitted' ? 'amber' : leg.status === 'failed' ? 'red' : 'default';
  return (
    <div>
      <window.PlatformChip name={platform}/>
      <div style={{ display:'flex', alignItems:'baseline', gap: 6, marginTop: 4, fontFamily: window.FONTS.mono, fontSize: 11 }}>
        <window.Pill tone={tone} size="sm">{leg.status}</window.Pill>
        <span style={{ color: t.textDim }}>{leg.fill_qty}@${leg.fill_price.toFixed(2)}</span>
      </div>
    </div>
  );
}

// ── P&L ─────────────────────────────────────────────────────────────
function PagePnL() {
  const { t } = window.useApp();
  const M = window.MOCK;
  const totalDeposits = M.pnl.total_deposits.kalshi + M.pnl.total_deposits.polymarket;
  const tradingPnl = M.pnl.recorded_trading_pnl.kalshi + M.pnl.recorded_trading_pnl.polymarket;

  return (
    <div style={{ padding: '28px 32px', maxWidth: 1400, margin: '0 auto' }}>
      <window.PageHeader kicker="Performance" title="P&L" sub="Reconciled trading P&L by platform. Deposits are subtracted from balance changes to isolate true trading performance.">
        <window.TimeRange/>
      </window.PageHeader>

      <window.Card padding={0} style={{ marginBottom: 18 }}>
        <div style={{ padding: '24px 28px', display:'flex', alignItems:'baseline', gap: 32, borderBottom: `1px solid ${t.border}` }}>
          <div>
            <div style={{ fontSize: 11, color: t.textMuted, letterSpacing:'0.06em', textTransform:'uppercase', marginBottom: 6 }}>Total balance</div>
            <div style={{ fontSize: 36, fontWeight: 600, color: t.text, fontFamily: window.FONTS.mono, letterSpacing:'-0.02em' }}>{window.fmt$(M.pnl.total_balance)}</div>
          </div>
          <div>
            <div style={{ fontSize: 11, color: t.textMuted, letterSpacing:'0.06em', textTransform:'uppercase', marginBottom: 6 }}>Trading P&L</div>
            <div style={{ fontSize: 24, fontWeight: 600, color: tradingPnl >= 0 ? t.green : t.red, fontFamily: window.FONTS.mono }}>{window.fmt$Sign(tradingPnl)}</div>
            <div style={{ fontSize: 11, color: t.textDim, marginTop: 2 }}>{window.fmtPct(tradingPnl/1000)} since inception</div>
          </div>
          <div>
            <div style={{ fontSize: 11, color: t.textMuted, letterSpacing:'0.06em', textTransform:'uppercase', marginBottom: 6 }}>Total deposits</div>
            <div style={{ fontSize: 24, fontWeight: 600, color: t.text, fontFamily: window.FONTS.mono }}>{window.fmt$(totalDeposits)}</div>
          </div>
          <div>
            <div style={{ fontSize: 11, color: t.textMuted, letterSpacing:'0.06em', textTransform:'uppercase', marginBottom: 6 }}>Annualized</div>
            <div style={{ fontSize: 24, fontWeight: 600, color: tradingPnl >= 0 ? t.green : t.red, fontFamily: window.FONTS.mono }}>{tradingPnl >= 0 ? '+' : ''}{(tradingPnl/1000*365).toFixed(1)}%</div>
          </div>
        </div>
        <div style={{ height: 320, padding: 16 }}>
          <window.AreaChart data={M.equity} width={1320} height={300} stroke={t.accent} grid={t.border}/>
        </div>
      </window.Card>

      <div style={{ display:'grid', gridTemplateColumns:'1fr 1fr', gap: 14 }}>
        <window.Card title="Per-platform P&L" padding={0}>
          <PlatformPnL platform="kalshi" color={t.green}/>
          <PlatformPnL platform="polymarket" color={t.purple} last/>
        </window.Card>
        <window.Card title="Reconciliation" padding={20}>
          <ReconRow label="Reconciliations run" value={M.health.reconciliation.reconciliation_count.toLocaleString()}/>
          <ReconRow label="Outstanding flags" value={M.health.reconciliation.flag_count} ok/>
          <ReconRow label="Audit score" value={(M.health.audit.audit_score * 100).toFixed(2) + '%'} ok/>
          <ReconRow label="Profitability verdict" value={M.health.profitability.verdict} pillTone="amber"/>
          <ReconRow label="Cumulative pnl (audit)" value={window.fmt$Sign(M.health.profitability.cumulative_pnl)}/>
          <div style={{ marginTop: 16, padding: 14, background: t.bgSubtle, borderRadius: 8, fontSize: 11.5, color: t.textDim, lineHeight: 1.55 }}>
            Trading P&L is computed as <span style={{ fontFamily: window.FONTS.mono, color: t.text }}>(current_balance − starting_balance − deposits)</span>, so deposit/withdrawal events do not pollute performance metrics.
          </div>
        </window.Card>
      </div>
    </div>
  );
}

function PlatformPnL({ platform, color, last }) {
  const { t } = window.useApp();
  const M = window.MOCK;
  const start = M.pnl.starting_balances[platform];
  const cur = M.pnl.current_balances[platform];
  const dep = M.pnl.total_deposits[platform];
  const pnl = M.pnl.recorded_trading_pnl[platform];
  return (
    <div style={{ padding: '18px 22px', borderBottom: last ? 'none' : `1px solid ${t.border}` }}>
      <div style={{ display:'flex', alignItems:'center', gap: 8, marginBottom: 12 }}>
        <span style={{ width: 8, height: 8, borderRadius:'50%', background: color }}/>
        <div style={{ fontSize: 14, fontWeight: 600, color: t.text, textTransform:'capitalize' }}>{platform}</div>
        <span style={{ marginLeft:'auto', fontSize: 16, fontWeight: 600, color: pnl >= 0 ? t.green : t.red, fontFamily: window.FONTS.mono }}>{window.fmt$Sign(pnl)}</span>
      </div>
      <div style={{ display:'grid', gridTemplateColumns:'repeat(4, minmax(0, 1fr))', gap: 12, fontFamily: window.FONTS.mono, fontVariantNumeric: 'tabular-nums' }}>
        <PnlMini label="Start" value={window.fmt$(start)}/>
        <PnlMini label="Deposits" value={window.fmt$(dep)}/>
        <PnlMini label="Current" value={window.fmt$(cur)}/>
        <PnlMini label="Δ vs start" value={window.fmt$Sign(cur - start - dep)} tone={pnl >= 0 ? 'green' : 'red'}/>
      </div>
    </div>
  );
}
function PnlMini({ label, value, tone }) {
  const { t } = window.useApp();
  const c = tone === 'green' ? t.green : tone === 'red' ? t.red : t.text;
  return (
    <div style={{ minWidth: 0 }}>
      <div style={{ fontSize: 10, color: t.textMuted, letterSpacing:'0.06em', textTransform:'uppercase', marginBottom: 4, height: 13, lineHeight: '13px', whiteSpace:'nowrap', overflow:'hidden', textOverflow:'ellipsis' }}>{label}</div>
      <div style={{ fontSize: 13, color: c, fontWeight: 500, fontVariantNumeric:'tabular-nums', whiteSpace:'nowrap', overflow:'hidden', textOverflow:'ellipsis' }}>{value}</div>
    </div>
  );
}
function ReconRow({ label, value, ok, pillTone }) {
  const { t } = window.useApp();
  return (
    <div style={{ display:'flex', alignItems:'center', justifyContent:'space-between', padding: '9px 0', borderBottom: `1px solid ${t.border}` }}>
      <span style={{ fontSize: 12, color: t.textDim }}>{label}</span>
      {pillTone ? <window.Pill tone={pillTone}>{value}</window.Pill> : <span style={{ fontSize: 13, color: ok ? t.green : t.text, fontWeight: 600, fontFamily: window.FONTS.mono }}>{value}</span>}
    </div>
  );
}

// ── Markets ─────────────────────────────────────────────────────────
function PageMarkets() {
  const { t } = window.useApp();
  const M = window.MOCK;
  return (
    <div style={{ padding: '28px 32px', maxWidth: 1400, margin: '0 auto' }}>
      <window.PageHeader kicker="Mapped universe" title="Markets" sub="312 confirmed mappings across 8 categories. Click any market to see live prices on both platforms.">
        <window.Btn variant="secondary" size="sm" icon="↓">Export</window.Btn>
        <window.Btn variant="primary" size="sm" icon="+">New mapping</window.Btn>
      </window.PageHeader>

      <div style={{ display:'grid', gridTemplateColumns:'repeat(8, 1fr)', gap: 8, marginBottom: 16 }}>
        {[
          ['Politics', 84], ['Crypto', 52], ['Macro', 47], ['Sports', 39],
          ['Tech', 36], ['Geo', 28], ['Entertainment', 16], ['Other', 10],
        ].map(([n, c]) => (
          <div key={n} style={{ background: t.bgCard, border: `1px solid ${t.border}`, borderRadius: 8, padding: '12px 14px' }}>
            <div style={{ fontSize: 10.5, color: t.textMuted, letterSpacing:'0.06em', textTransform:'uppercase' }}>{n}</div>
            <div style={{ fontSize: 18, fontWeight: 600, color: t.text, fontFamily: window.FONTS.mono, marginTop: 4 }}>{c}</div>
          </div>
        ))}
      </div>

      <window.Card padding={0}>
        <window.DataTable columns={[
          { label: 'Market', render: r => (
            <div>
              <div style={{ fontSize: 12.5, color: t.text, marginBottom: 3 }}>{r.description}</div>
              <div style={{ fontSize: 10.5, color: t.textMuted, fontFamily: window.FONTS.mono }}>{r.canonical_id}</div>
            </div>
          )},
          { label: 'Kalshi', w: '130px', render: r => <PriceCell yes={r.yes_platform === 'kalshi' ? r.yes_price : 1 - r.no_price}/> },
          { label: 'Polymarket', w: '130px', render: r => <PriceCell yes={r.yes_platform === 'polymarket' ? r.yes_price : 1 - r.no_price}/> },
          { label: 'Spread', w: '90px', align:'right', render: r => <span style={{ fontFamily: window.FONTS.mono, fontSize: 12, color: t.text }}>{((r.yes_price - (1 - r.no_price)) * 100).toFixed(1)}¢</span> },
          { label: 'Volume 24h', w: '120px', align:'right', render: r => <span style={{ fontFamily: window.FONTS.mono, fontSize: 12, color: t.textDim }}>{(r.yes_volume + r.no_volume).toLocaleString()}</span> },
          { label: 'Edge', w: '90px', align:'right', render: r => <span style={{ fontFamily: window.FONTS.mono, fontSize: 12.5, color: r.net_edge_cents >= 1 ? t.green : t.textDim, fontWeight: 600 }}>{window.fmtC(r.net_edge_cents)}</span> },
        ]} rows={M.opportunities}/>
      </window.Card>
    </div>
  );
}

function PriceCell({ yes }) {
  const { t } = window.useApp();
  return (
    <div style={{ fontFamily: window.FONTS.mono, fontSize: 12 }}>
      <div style={{ color: t.text }}>YES ${yes.toFixed(2)}</div>
      <div style={{ color: t.textDim, fontSize: 11 }}>NO ${(1 - yes).toFixed(2)}</div>
    </div>
  );
}

// ── Mappings ────────────────────────────────────────────────────────
function PageMappings() {
  const { t, setModal, toast } = window.useApp();
  const M = window.MOCK;
  // ⚠ BACKEND NOTE: hardcoded list — real impl pulls from /api/mappings/candidates
  // with filters (status=pending|review). The "Validate with agent" button on each
  // row should POST /api/mappings/validate/{id} which streams agent reasoning back
  // via SSE — see agent-validate.jsx for the full event schema and tool list.
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
    <div style={{ padding: '28px 32px', maxWidth: 1400, margin: '0 auto' }}>
      <window.PageHeader kicker="Cross-platform" title="Mappings" sub="Semantic matches between Kalshi and Polymarket markets, scored by description similarity, dates, and resolution criteria.">
        <window.Btn variant="secondary" size="sm" icon="↻" onClick={() => setModal({ kind:'refetchMappings' })}>Re-fetch mappings</window.Btn>
        <window.Btn variant="primary" size="sm" icon="◇" onClick={() => {
          // ⚠ BACKEND NOTE: this validates each pending candidate with an agent in
          // sequence. Real impl should fan out concurrent agents (cap=4) and stream
          // a multi-pane progress view. For now we open the first reviewable.
          const first = reviewable[0];
          if (first) setModal({ kind:'agentValidate', payload: first });
          else toast('No candidates pending review');
        }}>Validate all pending</window.Btn>
      </window.PageHeader>

      <div style={{ display:'grid', gridTemplateColumns:'repeat(4, 1fr)', gap: 14, marginBottom: 18 }}>
        <window.Card padding={18}><window.Stat label="Confirmed" value="312" tone="green" sub="Actively scanned" mono/></window.Card>
        <window.Card padding={18}><window.Stat label="Pending review" value={reviewable.length} tone="amber" sub="Score 0.6–0.95" mono/></window.Card>
        <window.Card padding={18}><window.Stat label="Avg score" value="0.91" sub="Across confirmed pool" mono/></window.Card>
        <window.Card padding={18}><window.Stat label="Last refetch" value="2h ago" sub="3,160 markets · 47 new" mono/></window.Card>
      </div>

      <window.Card title="Candidate mappings" padding={0} action={<span style={{ fontSize: 11, color: t.textMuted }}>{candidates.length} total · {reviewable.length} actionable</span>}>
        <window.DataTable columns={[
          { label: 'Score', w: '90px', render: r => (
            <div style={{ display:'flex', alignItems:'center', gap: 6 }}>
              <div style={{ width: 32, height: 32, borderRadius:'50%', background: r.score >= 0.9 ? t.greenSoft : r.score >= 0.7 ? t.amberSoft : t.redSoft, display:'flex', alignItems:'center', justifyContent:'center', fontSize: 11, fontWeight: 700, color: r.score >= 0.9 ? t.green : r.score >= 0.7 ? t.amber : t.red, fontFamily: window.FONTS.mono }}>{r.score.toFixed(2)}</div>
            </div>
          )},
          { label: 'Kalshi ticker', render: r => <span style={{ fontFamily: window.FONTS.mono, fontSize: 12, color: t.text }}>{r.k}</span> },
          { label: 'Polymarket slug', render: r => <span style={{ fontFamily: window.FONTS.mono, fontSize: 11.5, color: t.textDim }}>{r.p}</span> },
          { label: 'Status', w: '110px', render: r => <window.Pill tone={r.s === 'confirmed' ? 'green' : r.s === 'pending' ? 'amber' : r.s === 'review' ? 'blue' : 'red'}>{r.s}</window.Pill> },
          { label: '', w: '230px', align:'right', render: r => {
            if (r.s === 'confirmed') return <span style={{ fontSize: 11, color: t.textMuted }}>Active · scanned every 5s</span>;
            if (r.s === 'rejected') return <window.Btn variant="ghost" size="sm" onClick={(e) => { e.stopPropagation(); toast('Re-opened for review', { sub: r.k }); }}>Re-open</window.Btn>;
            // pending / review → show "Validate with agent" + accept/reject quick actions
            return (
              <div style={{ display:'flex', gap: 6, justifyContent:'flex-end' }}>
                <window.Btn variant="ghost" size="sm" onClick={(e) => { e.stopPropagation(); toast('Mapping rejected', { sub: r.k }); }}>Reject</window.Btn>
                <window.Btn variant="secondary" size="sm" icon="◇" onClick={(e) => { e.stopPropagation(); setModal({ kind:'agentValidate', payload: r }); }}>Validate</window.Btn>
              </div>
            );
          }},
        ]} rows={candidates} onRowClick={(r) => setModal({ kind:'agentValidate', payload: r })}/>
      </window.Card>

      <div style={{ marginTop: 12, padding: '12px 14px', background: t.bgSubtle, border:`1px dashed ${t.border}`, borderRadius: 8, fontSize: 11, color: t.textDim, display:'flex', alignItems:'center', gap: 10 }}>
        <span style={{ width: 6, height: 6, borderRadius:'50%', background: t.accent }}/>
        Click any mapping row to inspect validation history and run a read-only Claude Opus 4.7 verifier pass with every backend step, tool result, metric, and verdict shown.
      </div>
    </div>
  );
}

// ── Scanner ─────────────────────────────────────────────────────────
function PageScanner() {
  const { t, scanner, setScanner } = window.useApp();
  const M = window.MOCK;
  return (
    <div style={{ padding: '28px 32px', maxWidth: 1400, margin: '0 auto' }}>
      <window.PageHeader kicker="Real-time" title="Scanner" sub="Continuously polls Kalshi and Polymarket order books, computes net edge, and publishes opportunities crossing the threshold.">
        <window.Btn variant={scanner ? 'danger' : 'accent'} size="sm" onClick={() => setScanner(!scanner)} icon={scanner ? '⏸' : '▶'}>{scanner ? 'Pause scanner' : 'Resume scanner'}</window.Btn>
      </window.PageHeader>

      <div style={{ display:'grid', gridTemplateColumns:'repeat(4, 1fr)', gap: 14, marginBottom: 18 }}>
        <window.Card padding={18}><window.Stat label="Total scans" value={M.health.scanner.scan_count.toLocaleString()} sub={`${(M.health.uptime_seconds/3600).toFixed(1)}h uptime`} mono/></window.Card>
        <window.Card padding={18}><window.Stat label="Avg latency" value={`${M.health.scanner.scan_time_ms}ms`} sub="P50 across all markets" mono/></window.Card>
        <window.Card padding={18}><window.Stat label="Published" value={M.health.scanner.published} sub="Above threshold" tone="green" mono/></window.Card>
        <window.Card padding={18}><window.Stat label="Status" value={scanner ? 'Live' : 'Paused'} tone={scanner ? 'green' : 'amber'} sub="Heartbeat 1s ago" mono/></window.Card>
      </div>

      <window.Card title="Best edge — last 5 minutes" padding={0} style={{ marginBottom: 14 }}>
        <div style={{ height: 180, padding: 18 }}>
          <window.AreaChart data={M.health.scanner.history.map((h, i) => ({ t: i, v: h.best_edge_cents }))} width={1320} height={160} stroke={t.green} grid={t.border} currency={false}/>
        </div>
      </window.Card>

      <div style={{ display:'grid', gridTemplateColumns:'1fr 1fr', gap: 14 }}>
        <window.Card title="Per-market scan latency" padding={20}>
          <div style={{ height: 200 }}>
            <window.BarChart data={[
              { range:'0-100ms', count: 184 },
              { range:'100-200ms', count: 96 },
              { range:'200-300ms', count: 26 },
              { range:'300-500ms', count: 5 },
              { range:'500ms+', count: 1 },
            ]} color={t.bgHover} accent={t.accent} labelColor={t.textDim}/>
          </div>
        </window.Card>
        <window.Card title="Active scan engine" padding={20}>
          <div style={{ fontFamily: window.FONTS.mono, fontSize: 11, color: t.textDim, lineHeight: 1.7 }}>
            <KV k="Scan interval" v="5.0s"/>
            <KV k="Concurrency" v="32 markets / scan"/>
            <KV k="Min edge threshold" v="1.0¢"/>
            <KV k="Min volume" v="500 shares"/>
            <KV k="Persistence" v="3 consecutive scans"/>
            <KV k="Max position" v="$200 / pair"/>
            <KV k="Slippage budget" v="0.5¢"/>
          </div>
        </window.Card>
      </div>
    </div>
  );
}
function KV({ k, v }) {
  const { t } = window.useApp();
  return (
    <div style={{ display:'flex', justifyContent:'space-between', padding: '6px 0', borderBottom: `1px solid ${t.border}` }}>
      <span style={{ color: t.textDim }}>{k}</span>
      <span style={{ color: t.text, fontWeight: 600 }}>{v}</span>
    </div>
  );
}

// ── Audit ───────────────────────────────────────────────────────────
function PageAudit() {
  const { t, tradingMode } = window.useApp();
  const M = window.MOCK;
  const gates = [
    { name: 'Configuration valid', ok: true, detail: 'All required env vars set; API keys reachable.' },
    { name: 'Authentication healthy', ok: true, detail: 'Kalshi token refreshed 8m ago · Polymarket signed via wallet.' },
    { name: 'Profitability verdict', ok: true, detail: `Verdict: profitable · cumulative ${window.fmt$Sign(M.health.profitability.cumulative_pnl)}` },
    { name: 'Reconciliation flags', ok: true, detail: '0 outstanding discrepancies · last run 12s ago.' },
    { name: 'Scanner heartbeat', ok: true, detail: `${M.health.scanner.scan_time_ms}ms p50 · last scan 1s ago.` },
    { name: 'Audit score', ok: true, detail: `${(M.health.audit.audit_score * 100).toFixed(2)}% above 99% threshold.` },
    { name: 'Kill switch armed', ok: true, detail: 'Manual override available · automatic halt on flag count > 5.' },
  ];

  return (
    <div style={{ padding: '28px 32px', maxWidth: 1400, margin: '0 auto' }}>
      <window.PageHeader kicker="System integrity" title="Audit & Readiness" sub="Live trading is gated behind these 7 health checks. Failure of any gate forces standby mode."/>

      <div style={{ background: t.bgCard, border: `1px solid ${t.border}`, borderRadius: 12, padding: 28, marginBottom: 18, display:'flex', alignItems:'center', gap: 24 }}>
        <div style={{ width: 96, height: 96, borderRadius: '50%', background: t.greenSoft, border: `3px solid ${t.green}`, display:'flex', alignItems:'center', justifyContent:'center', flexShrink: 0 }}>
          <div style={{ fontSize: 36, fontWeight: 600, color: t.green, fontFamily: window.FONTS.mono }}>✓</div>
        </div>
        <div style={{ flex: 1 }}>
          <div style={{ fontSize: 11, color: t.textMuted, letterSpacing:'0.06em', textTransform:'uppercase', marginBottom: 4 }}>Status</div>
          <div style={{ fontSize: 24, fontWeight: 600, color: t.text, marginBottom: 4 }}>Live trading authorized</div>
          <div style={{ fontSize: 13, color: t.textDim }}>All 7 readiness gates passing · audit score {(M.health.audit.audit_score * 100).toFixed(2)}% · uptime {(M.health.uptime_seconds/3600).toFixed(1)}h</div>
        </div>
        <window.Btn variant="danger" size="md">⏹ Engage kill switch</window.Btn>
      </div>

      <window.Card title="Readiness gates" padding={0}>
        {gates.map((g, i) => (
          <div key={i} style={{ padding: '16px 22px', borderBottom: i < gates.length - 1 ? `1px solid ${t.border}` : 'none', display:'flex', alignItems:'center', gap: 16 }}>
            <div style={{ width: 28, height: 28, borderRadius:'50%', background: g.ok ? t.greenSoft : t.redSoft, color: g.ok ? t.green : t.red, display:'flex', alignItems:'center', justifyContent:'center', fontSize: 14, fontWeight: 700, flexShrink: 0 }}>{g.ok ? '✓' : '✕'}</div>
            <div style={{ flex: 1 }}>
              <div style={{ fontSize: 13, color: t.text, fontWeight: 500, marginBottom: 2 }}>{g.name}</div>
              <div style={{ fontSize: 11.5, color: t.textDim }}>{g.detail}</div>
            </div>
            <window.Pill tone={g.ok ? 'green' : 'red'}>{g.ok ? 'pass' : 'fail'}</window.Pill>
          </div>
        ))}
      </window.Card>
    </div>
  );
}

// ── Funds / Deposits ────────────────────────────────────────────────
function PageDeposits() {
  const { t, setModal } = window.useApp();
  const M = window.MOCK;
  return (
    <div style={{ padding: '28px 32px', maxWidth: 1400, margin: '0 auto' }}>
      <window.PageHeader kicker="Capital" title="Funds" sub="Deposit history and platform balances. Deposits are excluded from trading P&L computations.">
        <window.Btn variant="secondary" size="sm" icon="↓" onClick={() => setModal({ kind: 'deposit', payload: 'kalshi' })}>Deposit Kalshi</window.Btn>
        <window.Btn variant="primary" size="sm" icon="↓" onClick={() => setModal({ kind: 'deposit', payload: 'polymarket' })}>Deposit Polymarket</window.Btn>
      </window.PageHeader>

      <div style={{ display:'grid', gridTemplateColumns:'1fr 1fr', gap: 14, marginBottom: 18 }}>
        <PlatformBalCard platform="kalshi" color={t.green}/>
        <PlatformBalCard platform="polymarket" color={t.purple}/>
      </div>

      <window.Card title="Deposit history" padding={0}>
        <window.DataTable columns={[
          { label: 'Date', w: '130px', render: r => <span style={{ fontFamily: window.FONTS.mono, fontSize: 12, color: t.textDim }}>{window.tsDate(r.timestamp)} {window.tsTime(r.timestamp)}</span> },
          { label: 'Type', w: '110px', render: r => <window.Pill tone="blue">{r.type}</window.Pill> },
          { label: 'Platform', w: '160px', render: r => <window.PlatformChip name={r.platform}/> },
          { label: 'Amount', align:'right', render: r => <span style={{ fontFamily: window.FONTS.mono, fontSize: 13, color: t.green, fontWeight: 600 }}>+{window.fmt$(r.amount)}</span> },
          { label: 'Status', w: '100px', render: r => <window.Pill tone="green">settled</window.Pill> },
        ]} rows={M.deposits.deposits}/>
      </window.Card>
    </div>
  );
}

function PlatformBalCard({ platform, color }) {
  const { t, setModal } = window.useApp();
  const M = window.MOCK;
  const bal = M.balances[platform].balance;
  const dep = M.pnl.total_deposits[platform];
  const start = M.pnl.starting_balances[platform];
  const pnl = bal - start - dep;
  return (
    <div style={{ background: t.bgCard, border: `1px solid ${t.border}`, borderRadius: 12, padding: 24 }}>
      <div style={{ display:'flex', alignItems:'center', gap: 10, marginBottom: 18 }}>
        <span style={{ width: 10, height: 10, borderRadius: '50%', background: color }}/>
        <div style={{ fontSize: 16, fontWeight: 600, color: t.text, textTransform:'capitalize' }}>{platform}</div>
        <window.Pill tone="green">connected</window.Pill>
      </div>
      <div style={{ fontSize: 36, fontWeight: 600, color: t.text, fontFamily: window.FONTS.mono, letterSpacing:'-0.02em', marginBottom: 4 }}>{window.fmt$(bal)}</div>
      <div style={{ fontSize: 12, color: pnl >= 0 ? t.green : t.red, marginBottom: 16 }}>{window.fmt$Sign(pnl)} ({window.fmtPct(pnl/start)}) since inception</div>
      <div style={{ display:'flex', gap: 8 }}>
        <window.Btn variant="secondary" size="sm" onClick={() => setModal({ kind: 'deposit', payload: platform })}>Deposit</window.Btn>
        <window.Btn variant="ghost" size="sm">Withdraw</window.Btn>
      </div>
    </div>
  );
}

// ── Settings ────────────────────────────────────────────────────────
function PageSettings() {
  const { t, settings, setModal, toast } = window.useApp();
  const fmt$ = window.fmt$;
  return (
    <div style={{ padding: '28px 32px', maxWidth: 900, margin: '0 auto' }}>
      <window.PageHeader kicker="Configuration" title="Settings" sub="Strategy parameters, API connections, and operator preferences."/>

      <window.Card title="Strategy" padding={20} style={{ marginBottom: 14 }}>
        <SettingRow label="Min net edge" value={`${settings.minEdge.toFixed(1)}¢`} desc="Opportunities below this threshold are not published." onEdit={() => setModal({ kind:'editSetting', payload: { key:'minEdge', label:'Min net edge', kind:'slider', min:0.5, max:5, step:0.1, unit:'¢', desc:'Net edge required after fees, slippage, and gas. Lower = more opportunities but thinner margins.' } })}/>
        <SettingRow label="Min volume" value={`${settings.minVolume} shares`} desc="Required liquidity on both legs combined." onEdit={() => setModal({ kind:'editSetting', payload: { key:'minVolume', label:'Min volume', kind:'slider', min:100, max:5000, step:100, unit:' shares', desc:'Minimum combined orderbook depth across both legs. Protects against thin markets.' } })}/>
        <SettingRow label="Persistence" value={`${settings.persistence} scans`} desc="Edge must hold for this many consecutive 5s scans." onEdit={() => setModal({ kind:'editSetting', payload: { key:'persistence', label:'Persistence', kind:'slider', min:1, max:10, step:1, unit:' scans', desc:'How many consecutive scans the edge must persist before publishing. Higher = fewer false positives.' } })}/>
        <SettingRow label="Max position size" value={fmt$(settings.maxPos)} desc="Per arbitrage pair. Hard cap regardless of edge." onEdit={() => setModal({ kind:'editSetting', payload: { key:'maxPos', label:'Max position size', kind:'slider', min:50, max:1000, step:25, unit:'', desc:'Hard cap per arbitrage pair. Applied even if edge is large.' } })}/>
        <SettingRow label="Auto-execute" value={settings.autoExec === 'on' ? 'Enabled' : 'Disabled'} pill={settings.autoExec === 'on' ? 'green' : 'gray'} desc="Automatically place trades when all gates pass." onEdit={() => setModal({ kind:'editSetting', payload: { key:'autoExec', label:'Auto-execute', kind:'toggle', desc:'Off = opportunities show in queue but require manual approve. On = trades fire when all gates pass.' } })} last/>
      </window.Card>

      <window.Card title="API connections" padding={20} style={{ marginBottom: 14 }}>
        <SettingRow label="Kalshi" value="Connected" pill="green" desc="Token expires in 4h 12m · auto-refresh enabled." editLabel="Manage" onEdit={() => toast('Reauthenticating Kalshi…', { sub:'token will refresh in ~3s' })}/>
        <SettingRow label="Polymarket" value="Connected" pill="green" desc="Wallet 0x4a7…f2c9 · CLOB API authorized." editLabel="Manage" onEdit={() => toast('Polymarket connection healthy', { sub:'CLOB authorization valid' })}/>
        <SettingRow label="LLM provider" value="Anthropic" desc="Used for mapping similarity scoring." onEdit={() => setModal({ kind:'editSetting', payload: { key:'llm', label:'LLM provider', kind:'options', options:['Anthropic','OpenAI','Local'], desc:'Provider used for canonical-id matching across platforms.' } })} last/>
      </window.Card>

      <window.Card title="Operator" padding={20}>
        <SettingRow label="Email alerts" value="On" desc="kill-switch trips, recovery flows, daily P&L summary." editLabel="Configure" onEdit={() => setModal({ kind:'notifPrefs' })}/>
        <SettingRow label="2FA" value="TOTP" pill="green" desc="Required for kill-switch override and large deposits." editLabel="Reset" onEdit={() => toast('2FA reset link sent', { sub:'check your email' })}/>
        <SettingRow label="API key" value="••••••••sk-arb-08FE" mono desc="Read-only key for external dashboards." editLabel="View" onEdit={() => setModal({ kind:'apiKey' })} last/>
      </window.Card>

      <div style={{ display:'grid', gridTemplateColumns:'1fr 1fr', gap: 14, marginTop: 14 }}>
        <window.Card title="Team" padding={20}>
          <div style={{ fontSize: 12, color: t.textDim, lineHeight: 1.5, marginBottom: 14 }}>4 members · 1 admin · invitations pending: 0</div>
          <window.Btn variant="secondary" size="sm" onClick={() => setModal({ kind:'team' })}>Manage team</window.Btn>
        </window.Card>
        <window.Card title="Billing" padding={20}>
          <div style={{ fontSize: 12, color: t.textDim, lineHeight: 1.5, marginBottom: 14 }}>Pro plan · $99/mo · next renewal Apr 28</div>
          <window.Btn variant="secondary" size="sm" onClick={() => setModal({ kind:'billing' })}>Manage billing</window.Btn>
        </window.Card>
      </div>
    </div>
  );
}

function SettingRow({ label, value, desc, mono, pill, last, onEdit, editLabel }) {
  const { t } = window.useApp();
  return (
    <div style={{ padding: '14px 0', borderBottom: last ? 'none' : `1px solid ${t.border}`, display:'flex', alignItems:'center', gap: 16 }}>
      <div style={{ flex: 1, minWidth: 0 }}>
        <div style={{ fontSize: 13, color: t.text, fontWeight: 500, marginBottom: 2 }}>{label}</div>
        <div style={{ fontSize: 11.5, color: t.textDim, lineHeight: 1.45 }}>{desc}</div>
      </div>
      <div style={{ minWidth: 140, display:'flex', justifyContent:'flex-end' }}>
        {pill ? <window.Pill tone={pill}>{value}</window.Pill> :
          <div style={{ fontSize: 13, color: t.text, fontFamily: mono ? window.FONTS.mono : window.FONTS.sans, fontWeight: 500, fontVariantNumeric:'tabular-nums', textAlign:'right' }}>{value}</div>}
      </div>
      <window.Btn variant="ghost" size="sm" onClick={onEdit}>{editLabel || 'Edit'}</window.Btn>
    </div>
  );
}

window.PageTrades = PageTrades;
window.PagePnL = PagePnL;
window.PageMarkets = PageMarkets;
window.PageMappings = PageMappings;
window.PageScanner = PageScanner;
window.PageAudit = PageAudit;
window.PageDeposits = PageDeposits;
window.PageSettings = PageSettings;
