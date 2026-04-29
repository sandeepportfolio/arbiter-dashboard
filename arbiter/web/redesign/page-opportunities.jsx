// Opportunities page + opportunity drawer + execute modal.
const { useState: oppUseState, useMemo: oppUseMemo } = React;

function PageOpportunities() {
  const { t, setDrawer, minEdge, setMinEdge, autoExec, setAutoExec } = window.useApp();
  const M = window.MOCK;
  const [filter, setFilter] = oppUseState('all'); // all | tradable | candidate | review
  const [sort, setSort] = oppUseState('edge');
  const [search, setSearch] = oppUseState('');

  const filtered = oppUseMemo(() => {
    let r = M.opportunities;
    if (filter !== 'all') r = r.filter(o => o.status === filter);
    if (search) r = r.filter(o => o.description.toLowerCase().includes(search.toLowerCase()) || o.canonical_id.toLowerCase().includes(search.toLowerCase()));
    if (sort === 'edge') r = [...r].sort((a, b) => b.net_edge_cents - a.net_edge_cents);
    if (sort === 'profit') r = [...r].sort((a, b) => b.expected_profit - a.expected_profit);
    return r;
  }, [M, filter, sort, search]);

  const counts = {
    all: M.opportunities.length,
    tradable: M.opportunities.filter(o => o.status === 'tradable').length,
    candidate: M.opportunities.filter(o => o.status === 'candidate').length,
    review: M.opportunities.filter(o => o.status === 'review').length,
    illiquid: M.opportunities.filter(o => o.status === 'illiquid' || o.status === 'stale').length,
  };

  return (
    <div style={{ padding: '28px 32px', maxWidth: 1400, margin: '0 auto' }}>
      <window.PageHeader kicker="Live scan" title="Opportunities" sub="Mispriced YES/NO pairs across Kalshi and Polymarket. Click any row to inspect leg-by-leg math.">
        <ThresholdControl/>
        <AutoExecToggle/>
      </window.PageHeader>

      {/* Filter tabs */}
      <div style={{ display:'flex', alignItems:'center', gap: 4, padding: '4px', background: t.bgCard, border: `1px solid ${t.border}`, borderRadius: 9, marginBottom: 14, width: 'fit-content' }}>
        {[
          ['all', 'All'], ['tradable', 'Tradable'], ['candidate', 'Candidate'], ['review', 'Review'], ['illiquid', 'Stale / illiquid'],
        ].map(([k, l]) => (
          <button key={k} onClick={() => setFilter(k)} style={{
            padding: '6px 12px', background: filter === k ? t.bgSubtle : 'transparent', border: 'none', fontSize: 12, color: filter === k ? t.text : t.textDim, fontWeight: filter === k ? 600 : 400, borderRadius: 6, cursor:'pointer', display:'flex', alignItems:'center', gap: 6,
          }}>
            {l}
            <span style={{ fontSize: 10, color: filter === k ? t.accent : t.textMuted, fontFamily: window.FONTS.mono }}>{counts[k]}</span>
          </button>
        ))}
      </div>

      {/* Toolbar */}
      <div style={{ display:'flex', gap: 8, marginBottom: 12, alignItems:'center' }}>
        <input placeholder="Filter by description or canonical ID…" value={search} onChange={e => setSearch(e.target.value)} style={{ flex: 1, padding: '8px 12px', background: t.bgCard, border: `1px solid ${t.border}`, borderRadius: 7, fontSize: 12.5, color: t.text, outline: 'none' }} />
        <window.Btn variant="secondary" size="sm">Sort: {sort === 'edge' ? 'Net edge ↓' : 'Expected profit ↓'}</window.Btn>
        <window.Btn variant="secondary" size="sm" icon="↓">Export CSV</window.Btn>
      </div>

      {/* Table */}
      <window.Card padding={0}>
        <window.DataTable columns={[
          { label: 'Market', render: r => (
            <div>
              <div style={{ fontSize: 12.5, color: t.text, fontWeight: 500, marginBottom: 3 }}>{r.description}</div>
              <div style={{ fontSize: 10.5, color: t.textMuted, fontFamily: window.FONTS.mono }}>{r.canonical_id}</div>
            </div>
          )},
          { label: 'YES Leg', w: '180px', render: r => (
            <div>
              <window.PlatformChip name={r.yes_platform}/>
              <div style={{ fontSize: 12, color: t.text, fontFamily: window.FONTS.mono, marginTop: 4 }}>${r.yes_price.toFixed(2)} <span style={{ color: t.textMuted, fontSize: 10 }}>· {(r.fee_rate_yes*100).toFixed(1)}%</span></div>
            </div>
          )},
          { label: 'NO Leg', w: '180px', render: r => (
            <div>
              <window.PlatformChip name={r.no_platform}/>
              <div style={{ fontSize: 12, color: t.text, fontFamily: window.FONTS.mono, marginTop: 4 }}>${r.no_price.toFixed(2)} <span style={{ color: t.textMuted, fontSize: 10 }}>· {(r.fee_rate_no*100).toFixed(1)}%</span></div>
            </div>
          )},
          { label: 'Net edge', w: '100px', align: 'right', render: r => (
            <div style={{ textAlign:'right' }}>
              <div style={{ fontSize: 14, fontWeight: 600, color: r.net_edge_cents >= 2 ? t.green : r.net_edge_cents >= 1 ? t.amber : t.textDim, fontFamily: window.FONTS.mono }}>{window.fmtC(r.net_edge_cents)}</div>
              <div style={{ fontSize: 10, color: t.textMuted, fontFamily: window.FONTS.mono }}>{r.persistence_scans} scans</div>
            </div>
          )},
          { label: 'Expected', w: '100px', align: 'right', render: r => (
            <div style={{ textAlign:'right' }}>
              <div style={{ fontSize: 12.5, fontWeight: 500, color: t.text, fontFamily: window.FONTS.mono }}>+{window.fmt$(r.expected_profit)}</div>
              <div style={{ fontSize: 10, color: t.textMuted, fontFamily: window.FONTS.mono }}>{r.suggested_qty} sh</div>
            </div>
          )},
          { label: 'Status', w: '110px', render: r => {
            const tone = r.status === 'tradable' ? 'green' : r.status === 'candidate' ? 'blue' : r.status === 'review' ? 'amber' : 'default';
            return <window.Pill tone={tone}>{r.status}</window.Pill>;
          }},
          { label: '', w: '90px', align: 'right', render: r => r.status === 'tradable' ? <window.Btn variant="primary" size="sm">Execute →</window.Btn> : <span style={{ fontSize: 11, color: t.textMuted }}>—</span> },
        ]} rows={filtered} onRowClick={(r) => setDrawer({ kind: 'opp', payload: M.opportunities.indexOf(r) })} />
      </window.Card>
    </div>
  );
}

function ThresholdControl() {
  const { t, minEdge, setMinEdge } = window.useApp();
  return (
    <div style={{ display:'flex', alignItems:'center', gap: 8, padding: '5px 10px 5px 12px', background: t.bgCard, border: `1px solid ${t.border}`, borderRadius: 7 }}>
      <span style={{ fontSize: 11, color: t.textDim }}>Min edge</span>
      <input type="range" min="0" max="5" step="0.1" value={minEdge} onChange={e => setMinEdge(+e.target.value)} style={{ width: 80, accentColor: t.accent }}/>
      <span style={{ fontSize: 11.5, color: t.text, fontFamily: window.FONTS.mono, fontWeight: 600, width: 30 }}>{minEdge.toFixed(1)}¢</span>
    </div>
  );
}

function AutoExecToggle() {
  const { t, autoExec, setAutoExec } = window.useApp();
  return (
    <button onClick={() => setAutoExec(!autoExec)} style={{ display:'flex', alignItems:'center', gap: 8, padding: '6px 12px', background: autoExec ? t.accentSoft : t.bgCard, border: `1px solid ${autoExec ? t.accent + '50' : t.border}`, borderRadius: 7, fontSize: 12, color: autoExec ? t.accent : t.textDim, fontWeight: 500, cursor:'pointer' }}>
      <span style={{ width: 22, height: 12, borderRadius: 99, background: autoExec ? t.accent : t.borderBright, position:'relative', display:'inline-block' }}>
        <span style={{ position:'absolute', top: 1, left: autoExec ? 11 : 1, width: 10, height: 10, borderRadius:'50%', background:'#fff', transition: 'left 0.15s' }}/>
      </span>
      Auto-execute
    </button>
  );
}

// ── Opportunity drawer ───────────────────────────────────────────────
function OppDrawer() {
  const { t, drawer, setDrawer, setModal } = window.useApp();
  if (!drawer || drawer.kind !== 'opp') return null;
  const o = window.MOCK.opportunities[drawer.payload];
  if (!o) return null;
  const grossYes = (1 - o.yes_price) * o.suggested_qty;
  const grossNo = (1 - o.no_price) * o.suggested_qty;
  const cost = (o.yes_price + o.no_price) * o.suggested_qty;
  const fees = (o.yes_price * o.fee_rate_yes + o.no_price * o.fee_rate_no) * o.suggested_qty;
  const payout = o.suggested_qty;
  const profit = payout - cost - fees;

  return (
    <div onClick={() => setDrawer(null)} style={{ position:'fixed', inset: 0, background: t.overlay, zIndex: 80, display:'flex', justifyContent:'flex-end' }}>
      <div onClick={e => e.stopPropagation()} style={{ width: 520, height: '100%', background: t.bgCard, borderLeft: `1px solid ${t.border}`, display:'flex', flexDirection:'column', boxShadow: t.shadowLg }}>
        <div style={{ padding: '20px 24px', borderBottom: `1px solid ${t.border}` }}>
          <div style={{ display:'flex', alignItems:'center', justifyContent:'space-between', marginBottom: 10 }}>
            <window.Pill tone={o.status === 'tradable' ? 'green' : 'blue'}>{o.status}</window.Pill>
            <button onClick={() => setDrawer(null)} style={{ background:'none', border:'none', color: t.textMuted, fontSize: 18, cursor:'pointer' }}>✕</button>
          </div>
          <div style={{ fontSize: 17, fontWeight: 600, color: t.text, lineHeight: 1.35, marginBottom: 6 }}>{o.description}</div>
          <div style={{ fontSize: 11, color: t.textMuted, fontFamily: window.FONTS.mono }}>{o.canonical_id} · last seen {window.ago(o.last_seen)}</div>
        </div>

        <div style={{ flex: 1, overflow: 'auto', padding: '20px 24px' }}>
          {/* Edge math card */}
          <div style={{ padding: 18, background: t.bgSubtle, borderRadius: 10, marginBottom: 20 }}>
            <div style={{ fontSize: 10.5, color: t.textMuted, letterSpacing: '0.06em', textTransform:'uppercase', marginBottom: 8 }}>NET EDGE BREAKDOWN</div>
            <div style={{ display:'flex', alignItems:'baseline', gap: 4, marginBottom: 12 }}>
              <span style={{ fontSize: 32, fontWeight: 600, color: t.green, fontFamily: window.FONTS.mono, letterSpacing: '-0.02em' }}>{window.fmtC(o.net_edge_cents)}</span>
              <span style={{ fontSize: 12, color: t.textDim }}>per pair · {o.persistence_scans} consecutive scans</span>
            </div>
            <BreakdownLine label="Sum of prices" value={`$${(o.yes_price + o.no_price).toFixed(2)}`} sub={`YES $${o.yes_price.toFixed(2)} + NO $${o.no_price.toFixed(2)}`}/>
            <BreakdownLine label="Gross edge" value={`${((1 - (o.yes_price + o.no_price)) * 100).toFixed(1)}¢`} positive/>
            <BreakdownLine label="Fees" value={`−${(o.fee_rate_yes*o.yes_price*100 + o.fee_rate_no*o.no_price*100).toFixed(2)}¢`} negative/>
            <BreakdownLine label="Net edge" value={window.fmtC(o.net_edge_cents)} positive bold/>
          </div>

          {/* Legs */}
          <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 12, marginBottom: 20 }}>
            <LegCard side="YES" platform={o.yes_platform} price={o.yes_price} fee={o.fee_rate_yes} qty={o.suggested_qty} volume={o.yes_volume}/>
            <LegCard side="NO" platform={o.no_platform} price={o.no_price} fee={o.fee_rate_no} qty={o.suggested_qty} volume={o.no_volume}/>
          </div>

          {/* Execution preview */}
          <div style={{ fontSize: 11, fontWeight: 600, color: t.textMuted, letterSpacing: '0.06em', textTransform:'uppercase', marginBottom: 10 }}>EXECUTION PREVIEW</div>
          <div style={{ background: t.bgSubtle, borderRadius: 10, padding: 16, marginBottom: 20, fontSize: 12, fontFamily: window.FONTS.mono, color: t.textDim, lineHeight: 1.8 }}>
            <PreviewRow label="Quantity" value={`${o.suggested_qty} pairs`}/>
            <PreviewRow label="Total cost" value={`−${window.fmt$(cost, 2)}`}/>
            <PreviewRow label="Fees" value={`−${window.fmt$(fees, 4)}`}/>
            <PreviewRow label="Guaranteed payout" value={`+${window.fmt$(payout, 2)}`}/>
            <div style={{ borderTop: `1px solid ${t.border}`, margin: '8px 0' }}/>
            <PreviewRow label="Net profit" value={`+${window.fmt$(profit, 4)}`} bold positive/>
          </div>

          {/* Risk gates */}
          <div style={{ fontSize: 11, fontWeight: 600, color: t.textMuted, letterSpacing: '0.06em', textTransform:'uppercase', marginBottom: 10 }}>RISK GATES</div>
          <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 8, marginBottom: 8 }}>
            <Gate ok label="Min edge ≥ 1.0¢" detail={`${window.fmtC(o.net_edge_cents)} pass`}/>
            <Gate ok label="Volume ≥ 500" detail={`${(o.yes_volume + o.no_volume).toLocaleString()} on both legs`}/>
            <Gate ok label="Persistence ≥ 3 scans" detail={`${o.persistence_scans} scans seen`}/>
            <Gate ok label="Position ≤ $200" detail={`${window.fmt$(cost, 0)} requested`}/>
          </div>
        </div>

        <div style={{ padding: '16px 24px', borderTop: `1px solid ${t.border}`, display:'flex', gap: 8 }}>
          <window.Btn variant="ghost" onClick={() => setDrawer(null)}>Cancel</window.Btn>
          <div style={{ flex: 1 }}/>
          <window.Btn variant="secondary" icon="◇">Mark for review</window.Btn>
          <window.Btn variant="primary" icon="→" onClick={() => { setDrawer(null); setModal({ kind: 'execute', payload: drawer.payload }); }}>Execute trade</window.Btn>
        </div>
      </div>
    </div>
  );
}

function BreakdownLine({ label, value, sub, positive, negative, bold }) {
  const { t } = window.useApp();
  const c = positive ? t.green : negative ? t.red : t.text;
  return (
    <div style={{ display:'flex', alignItems:'baseline', justifyContent:'space-between', padding: '5px 0', borderTop: bold ? `1px solid ${t.border}` : 'none', marginTop: bold ? 6 : 0, paddingTop: bold ? 9 : 5 }}>
      <div>
        <div style={{ fontSize: 12, color: t.textDim, fontWeight: bold ? 600 : 400 }}>{label}</div>
        {sub && <div style={{ fontSize: 10.5, color: t.textMuted, marginTop: 1 }}>{sub}</div>}
      </div>
      <div style={{ fontSize: bold ? 14 : 12.5, fontFamily: window.FONTS.mono, color: c, fontWeight: bold ? 700 : 500 }}>{value}</div>
    </div>
  );
}

function LegCard({ side, platform, price, fee, qty, volume }) {
  const { t } = window.useApp();
  return (
    <div style={{ border: `1px solid ${t.border}`, borderRadius: 10, padding: 16 }}>
      <div style={{ display:'flex', alignItems:'center', justifyContent:'space-between', marginBottom: 10 }}>
        <span style={{ fontSize: 10, color: t.textMuted, letterSpacing: '0.08em', fontWeight: 600 }}>{side} LEG</span>
        <window.PlatformChip name={platform}/>
      </div>
      <div style={{ fontSize: 24, fontWeight: 600, color: t.text, fontFamily: window.FONTS.mono, letterSpacing:'-0.015em', marginBottom: 8 }}>${price.toFixed(2)}</div>
      <div style={{ fontSize: 10.5, color: t.textDim, lineHeight: 1.6 }}>
        <div>Fee: {(fee * 100).toFixed(2)}%</div>
        <div>Qty: {qty} shares</div>
        <div>24h vol: {volume.toLocaleString()}</div>
      </div>
    </div>
  );
}

function PreviewRow({ label, value, bold, positive }) {
  const { t } = window.useApp();
  return (
    <div style={{ display:'flex', justifyContent:'space-between', alignItems:'baseline', fontWeight: bold ? 700 : 400 }}>
      <span style={{ color: t.textDim }}>{label}</span>
      <span style={{ color: positive ? t.green : t.text, fontWeight: bold ? 700 : 500 }}>{value}</span>
    </div>
  );
}

function Gate({ ok, label, detail }) {
  const { t } = window.useApp();
  return (
    <div style={{ padding: 10, background: ok ? t.greenSoft : t.redSoft, border: `1px solid ${ok ? t.green + '30' : t.red + '30'}`, borderRadius: 7 }}>
      <div style={{ display:'flex', alignItems:'center', gap: 6, fontSize: 11.5, color: ok ? t.green : t.red, fontWeight: 600, marginBottom: 2 }}>
        <span>{ok ? '✓' : '✕'}</span>{label}
      </div>
      <div style={{ fontSize: 10.5, color: t.textDim }}>{detail}</div>
    </div>
  );
}

window.PageOpportunities = PageOpportunities;
window.OppDrawer = OppDrawer;
