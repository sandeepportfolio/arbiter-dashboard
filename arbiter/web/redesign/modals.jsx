// Execute trade modal, trade detail modal, deposit modal.
const { useState: mUseState } = React;

function Modal() {
  const { modal } = window.useApp();
  if (!modal) return null;
  if (modal.kind === 'execute') return <ExecuteModal/>;
  if (modal.kind === 'trade') return <TradeDetailModal/>;
  if (modal.kind === 'deposit') return <DepositModal/>;
  if (modal.kind === 'withdraw') return <WithdrawModal/>;
  if (modal.kind === 'profile') return <ProfileModal/>;
  if (modal.kind === 'editSetting') return <EditSettingModal/>;
  if (modal.kind === 'apiKey') return <ApiKeyModal/>;
  if (modal.kind === 'notifPrefs') return <NotifPrefsModal/>;
  if (modal.kind === 'reset2fa') return <Reset2faModal/>;
  return null;
}

// ── Generic helpers ────────────────────────────────────────────────
function ModalHeader({ title, sub }) {
  const { t, setModal } = window.useApp();
  return (
    <div style={{ padding: '18px 22px', borderBottom: `1px solid ${t.border}`, display:'flex', alignItems:'center', justifyContent:'space-between' }}>
      <div>
        <div style={{ fontSize: 15, fontWeight: 600, color: t.text }}>{title}</div>
        {sub && <div style={{ fontSize: 11.5, color: t.textDim, marginTop: 2 }}>{sub}</div>}
      </div>
      <button onClick={() => setModal(null)} style={{ background:'none', border:'none', color: t.textMuted, fontSize: 18, cursor:'pointer', padding: 4, lineHeight: 1 }}>×</button>
    </div>
  );
}
function ModalFooter({ primary, secondary, onPrimary, onSecondary, danger }) {
  const { t, setModal } = window.useApp();
  return (
    <div style={{ padding: '14px 22px', borderTop: `1px solid ${t.border}`, display:'flex', justifyContent:'flex-end', gap: 8, background: t.bgSubtle }}>
      <button onClick={onSecondary || (() => setModal(null))} style={{ padding: '8px 14px', background:'transparent', border:`1px solid ${t.border}`, borderRadius: 7, fontSize: 12, fontWeight: 600, color: t.text, cursor:'pointer' }}>{secondary || 'Cancel'}</button>
      <button onClick={onPrimary} style={{ padding: '8px 14px', background: danger ? t.red : t.text, color: danger ? '#fff' : t.bgCard, border:'none', borderRadius: 7, fontSize: 12, fontWeight: 600, cursor:'pointer' }}>{primary}</button>
    </div>
  );
}
function Field({ label, children }) {
  const { t } = window.useApp();
  return (
    <div style={{ marginBottom: 14 }}>
      <div style={{ fontSize: 11, color: t.textMuted, letterSpacing:'0.06em', textTransform:'uppercase', marginBottom: 6 }}>{label}</div>
      {children}
    </div>
  );
}

// ── Withdraw ───────────────────────────────────────────────────────
function WithdrawModal() {
  const { t, modal, setModal, toast } = window.useApp();
  const platform = modal.payload || 'kalshi';
  const [amt, setAmt] = mUseState('100');
  const [dest, setDest] = mUseState('bank');
  return (
    <Shell width={460}>
      <ModalHeader title={`Withdraw from ${platform}`} sub="Funds arrive in 1–3 business days"/>
      <div style={{ padding: 22 }}>
        <Field label="Amount (USD)">
          <input value={amt} onChange={e => setAmt(e.target.value)} style={{ width:'100%', padding: '10px 12px', background: t.bgSubtle, border:`1px solid ${t.border}`, borderRadius: 7, fontSize: 16, fontFamily: window.FONTS.mono, color: t.text, fontWeight: 600 }}/>
        </Field>
        <Field label="Destination">
          <div style={{ display:'flex', gap: 6 }}>
            {[['bank','Bank account · ••3421'],['wallet','USDC wallet · 0x4f…a2']].map(([k, l]) => (
              <button key={k} onClick={() => setDest(k)} style={{ flex: 1, padding: '10px', background: dest === k ? t.bgSubtle : 'transparent', border:`1px solid ${dest === k ? t.text : t.border}`, borderRadius: 7, fontSize: 12, color: t.text, cursor:'pointer', textAlign:'left' }}>{l}</button>
            ))}
          </div>
        </Field>
        <div style={{ background: t.amberSoft, border:`1px solid ${t.amber}30`, borderRadius: 7, padding: 10, fontSize: 11, color: t.amber }}>Open trades will be paused while withdrawal processes.</div>
      </div>
      <ModalFooter primary={`Withdraw $${amt}`} onPrimary={() => { setModal(null); toast(`Withdrawal queued · $${amt}`, { sub:'Confirmation sent to your email' }); }}/>
    </Shell>
  );
}

// ── Profile ────────────────────────────────────────────────────────
function ProfileModal() {
  const { t, profile, signOut, setModal, toast } = window.useApp();
  return (
    <Shell width={420}>
      <ModalHeader title="Profile"/>
      <div style={{ padding: 22, textAlign:'center' }}>
        <div style={{ width: 72, height: 72, borderRadius:'50%', background: t.accent, color:'#fff', display:'flex', alignItems:'center', justifyContent:'center', fontSize: 26, fontWeight: 700, margin:'0 auto 14px' }}>{profile.initials}</div>
        <div style={{ fontSize: 17, fontWeight: 600 }}>{profile.name}</div>
        <div style={{ fontSize: 12, color: t.textDim, marginTop: 2 }}>{profile.email}</div>
        <div style={{ fontSize: 11, color: t.textMuted, marginTop: 8, fontFamily: window.FONTS.mono }}>Member since Mar 2025 · Pro plan</div>
      </div>
      <div style={{ padding: '0 22px 18px', display:'grid', gridTemplateColumns:'1fr 1fr', gap: 8 }}>
        <button onClick={() => toast('Edit profile · coming soon')} style={{ padding: '10px', background: t.bgSubtle, border:`1px solid ${t.border}`, borderRadius: 7, fontSize: 12, fontWeight: 600, color: t.text, cursor:'pointer' }}>Edit profile</button>
        <button onClick={() => { setModal(null); signOut(); }} style={{ padding: '10px', background: t.redSoft, border:`1px solid ${t.red}40`, borderRadius: 7, fontSize: 12, fontWeight: 600, color: t.red, cursor:'pointer' }}>Sign out</button>
      </div>
    </Shell>
  );
}

// ── Edit setting (slider or options) ────────────────────────────────
function EditSettingModal() {
  const { t, modal, setModal, toast } = window.useApp();
  const p = modal.payload;
  const [val, setVal] = mUseState(p.value);
  const onSave = () => { p.onSave && p.onSave(val); setModal(null); toast(`${p.label} updated`); };
  return (
    <Shell width={420}>
      <ModalHeader title={`Edit ${p.label}`}/>
      <div style={{ padding: 22 }}>
        {p.kind === 'slider' && (
          <>
            <div style={{ fontSize: 32, fontWeight: 600, fontFamily: window.FONTS.mono, textAlign:'center', marginBottom: 16, color: t.accent }}>{p.prefix || ''}{typeof val === 'number' ? val.toFixed(p.step < 1 ? 1 : 0) : val}</div>
            <input type="range" min={p.min} max={p.max} step={p.step} value={val} onChange={e => setVal(parseFloat(e.target.value))} style={{ width:'100%' }}/>
            <div style={{ display:'flex', justifyContent:'space-between', fontSize: 10.5, color: t.textMuted, fontFamily: window.FONTS.mono, marginTop: 4 }}>
              <span>{p.prefix || ''}{p.min}</span><span>{p.prefix || ''}{p.max}</span>
            </div>
          </>
        )}
        {p.kind === 'options' && (
          <div style={{ display:'flex', flexDirection:'column', gap: 6 }}>
            {p.options.map(o => (
              <button key={o.v} onClick={() => setVal(o.v)} style={{ padding: '11px 14px', background: val === o.v ? t.bgSubtle : 'transparent', border:`1px solid ${val === o.v ? t.text : t.border}`, borderRadius: 7, fontSize: 13, color: t.text, cursor:'pointer', textAlign:'left', fontWeight: val === o.v ? 600 : 400 }}>{o.l}</button>
            ))}
          </div>
        )}
      </div>
      <ModalFooter primary="Save" onPrimary={onSave}/>
    </Shell>
  );
}

// ── API key ────────────────────────────────────────────────────────
function ApiKeyModal() {
  const { t, modal, setModal, toast } = window.useApp();
  const platform = modal.payload || 'kalshi';
  const [showKey, setShowKey] = mUseState(false);
  return (
    <Shell width={500}>
      <ModalHeader title={`${platform} API credentials`} sub="Stored encrypted · never logged"/>
      <div style={{ padding: 22 }}>
        <Field label="API key ID">
          <div style={{ padding: '10px 12px', background: t.bgSubtle, border:`1px solid ${t.border}`, borderRadius: 7, fontSize: 12, fontFamily: window.FONTS.mono, color: t.text }}>ak_live_4f2b8e9c12a64d</div>
        </Field>
        <Field label="API secret">
          <div style={{ display:'flex', gap: 6 }}>
            <div style={{ flex: 1, padding: '10px 12px', background: t.bgSubtle, border:`1px solid ${t.border}`, borderRadius: 7, fontSize: 12, fontFamily: window.FONTS.mono, color: t.text }}>{showKey ? 'sk_live_8a4c2e1b7f9d3a6e2c1b' : '••••••••••••••••••'}</div>
            <button onClick={() => setShowKey(!showKey)} style={{ padding: '0 14px', background: t.bgSubtle, border:`1px solid ${t.border}`, borderRadius: 7, fontSize: 11, color: t.text, cursor:'pointer' }}>{showKey ? 'Hide' : 'Show'}</button>
          </div>
        </Field>
        <div style={{ display:'grid', gridTemplateColumns:'1fr 1fr', gap: 10, marginBottom: 12 }}>
          <div><div style={{ fontSize: 10, color: t.textMuted, letterSpacing:'0.06em', textTransform:'uppercase' }}>Last used</div><div style={{ fontSize: 12, color: t.text, marginTop: 3, fontFamily: window.FONTS.mono }}>2 minutes ago</div></div>
          <div><div style={{ fontSize: 10, color: t.textMuted, letterSpacing:'0.06em', textTransform:'uppercase' }}>Rotates</div><div style={{ fontSize: 12, color: t.text, marginTop: 3, fontFamily: window.FONTS.mono }}>Jul 14, 2026</div></div>
        </div>
        <div style={{ background: t.amberSoft, border:`1px solid ${t.amber}30`, borderRadius: 7, padding: 10, fontSize: 11, color: t.amber }}>Rotating keys requires re-authenticating · trading will pause for ~30s.</div>
      </div>
      <ModalFooter primary="Rotate keys" danger onPrimary={() => { setModal(null); toast('Keys rotated', { sub: 'New credentials active' }); }} secondary="Close"/>
    </Shell>
  );
}

// ── Notification prefs ─────────────────────────────────────────────
function NotifPrefsModal() {
  const { t, setModal, toast } = window.useApp();
  const [prefs, setPrefs] = mUseState({ fillsEmail: true, errorsEmail: true, killEmail: true, highEdgePush: true, dailySummaryEmail: true, kpiSlack: false });
  const tog = (k) => setPrefs({ ...prefs, [k]: !prefs[k] });
  const items = [
    ['fillsEmail', 'Email · Trade fills', 'Every executed leg'],
    ['errorsEmail', 'Email · Errors', 'Failed orders, gate breaches'],
    ['killEmail', 'Email · Kill events', 'Auto-halts and manual kills'],
    ['highEdgePush', 'Push · High-edge opps', 'Edge ≥ 4¢ that pass all gates'],
    ['dailySummaryEmail', 'Email · Daily summary', 'EOD P&L digest at 4pm ET'],
    ['kpiSlack', 'Slack · KPI breaches', 'Below-threshold win rate, drawdown'],
  ];
  return (
    <Shell width={500}>
      <ModalHeader title="Notification preferences"/>
      <div style={{ padding: '8px 22px 22px' }}>
        {items.map(([k, l, s]) => (
          <div key={k} style={{ padding: '12px 0', borderBottom:`1px solid ${t.border}`, display:'flex', alignItems:'center' }}>
            <div style={{ flex: 1 }}>
              <div style={{ fontSize: 13, color: t.text }}>{l}</div>
              <div style={{ fontSize: 11, color: t.textDim, marginTop: 1 }}>{s}</div>
            </div>
            <button onClick={() => tog(k)} style={{ width: 38, height: 22, background: prefs[k] ? t.green : t.bgSubtle, border:`1px solid ${prefs[k] ? t.green : t.border}`, borderRadius: 99, padding: 0, position:'relative', cursor:'pointer' }}>
              <span style={{ position:'absolute', top: 2, left: prefs[k] ? 18 : 2, width: 16, height: 16, background:'#fff', borderRadius:'50%', boxShadow:'0 1px 2px rgba(0,0,0,0.2)', transition:'all 0.15s' }}/>
            </button>
          </div>
        ))}
      </div>
      <ModalFooter primary="Save preferences" onPrimary={() => { setModal(null); toast('Preferences saved'); }}/>
    </Shell>
  );
}

// ── 2FA reset ──────────────────────────────────────────────────────
function Reset2faModal() {
  const { t, setModal, toast } = window.useApp();
  return (
    <Shell width={440}>
      <ModalHeader title="Two-factor authentication" sub="Authenticator app · enabled Mar 12, 2025"/>
      <div style={{ padding: 22 }}>
        <div style={{ background: t.greenSoft, border:`1px solid ${t.green}30`, borderRadius: 7, padding: 10, fontSize: 11.5, color: t.green, marginBottom: 14, display:'flex', alignItems:'center', gap: 8 }}>
          <span style={{ width: 6, height: 6, borderRadius:'50%', background: t.green }}/>
          2FA active · last verified 8h ago
        </div>
        <Field label="Backup codes">
          <div style={{ background: t.bgSubtle, border:`1px solid ${t.border}`, borderRadius: 7, padding: 12, fontSize: 12, fontFamily: window.FONTS.mono, color: t.text, lineHeight: 1.8 }}>
            <div>4F2B-8E9C · 12A6-4DF7 · 9C2E-7B14</div>
            <div>3A8D-5F1E · 7E4C-2B9A · 8D1F-6C3E</div>
          </div>
        </Field>
        <button onClick={() => toast('New codes generated', { sub:'Old codes invalidated' })} style={{ padding: '8px 14px', background:'transparent', border:`1px solid ${t.border}`, borderRadius: 7, fontSize: 12, color: t.text, cursor:'pointer' }}>Regenerate codes</button>
      </div>
      <ModalFooter primary="Disable 2FA" danger onPrimary={() => { setModal(null); toast('2FA disabled', { sub:'Re-enable from settings' }); }} secondary="Close"/>
    </Shell>
  );
}

function Shell({ children, width = 560 }) {
  const { t, setModal } = window.useApp();
  return (
    <div onClick={() => setModal(null)} style={{ position:'fixed', inset: 0, background: t.overlay, zIndex: 95, display:'flex', justifyContent:'center', alignItems:'center', padding: 40 }}>
      <div onClick={e => e.stopPropagation()} style={{ width, maxHeight: '90vh', background: t.bgCard, border: `1px solid ${t.border}`, borderRadius: 14, boxShadow: t.shadowLg, display:'flex', flexDirection:'column', overflow:'hidden' }}>
        {children}
      </div>
    </div>
  );
}

// ── Execute ─────────────────────────────────────────────────────────
function ExecuteModal() {
  const { t, modal, setModal, setPage } = window.useApp();
  const o = window.MOCK.opportunities[modal.payload];
  const [stage, setStage] = mUseState('confirm'); // confirm | submitting | filled
  const [qty, setQty] = mUseState(o.suggested_qty);
  const cost = (o.yes_price + o.no_price) * qty;
  const fees = (o.yes_price * o.fee_rate_yes + o.no_price * o.fee_rate_no) * qty;
  const profit = qty - cost - fees;

  const submit = () => {
    setStage('submitting');
    setTimeout(() => setStage('filled'), 1800);
  };

  return (
    <Shell width={560}>
      <div style={{ padding: '20px 24px', borderBottom: `1px solid ${t.border}`, display:'flex', alignItems:'center', justifyContent:'space-between' }}>
        <div>
          <div style={{ fontSize: 11, color: t.textMuted, marginBottom: 3 }}>Execute arbitrage</div>
          <div style={{ fontSize: 16, fontWeight: 600, color: t.text }}>{o.description}</div>
        </div>
        <button onClick={() => setModal(null)} style={{ background:'none', border:'none', color: t.textMuted, fontSize: 20, cursor:'pointer' }}>✕</button>
      </div>

      {stage === 'confirm' && (
        <>
          <div style={{ padding: 24 }}>
            <div style={{ fontSize: 11, color: t.textMuted, letterSpacing:'0.06em', textTransform:'uppercase', marginBottom: 10 }}>QUANTITY</div>
            <div style={{ display:'flex', alignItems:'center', gap: 12, marginBottom: 24 }}>
              <input type="range" min="10" max="500" step="10" value={qty} onChange={e => setQty(+e.target.value)} style={{ flex: 1, accentColor: t.accent }}/>
              <div style={{ width: 100, padding: '8px 12px', background: t.bgSubtle, border: `1px solid ${t.border}`, borderRadius: 7, fontFamily: window.FONTS.mono, fontSize: 14, fontWeight: 600, color: t.text, textAlign:'center' }}>{qty} pairs</div>
            </div>

            <div style={{ display:'grid', gridTemplateColumns:'1fr 1fr', gap: 12, marginBottom: 20 }}>
              <ConfirmLeg side="YES" platform={o.yes_platform} price={o.yes_price} qty={qty}/>
              <ConfirmLeg side="NO" platform={o.no_platform} price={o.no_price} qty={qty}/>
            </div>

            <div style={{ background: t.bgSubtle, borderRadius: 10, padding: 16, fontFamily: window.FONTS.mono, fontSize: 12.5, color: t.text }}>
              <Row k="Total cost" v={`−${window.fmt$(cost, 2)}`}/>
              <Row k="Fees" v={`−${window.fmt$(fees, 4)}`}/>
              <Row k="Guaranteed payout" v={`+${window.fmt$(qty, 2)}`}/>
              <div style={{ borderTop: `1px solid ${t.border}`, margin: '8px 0' }}/>
              <Row k="Net profit" v={`+${window.fmt$(profit, 4)}`} bold positive/>
            </div>
          </div>
          <div style={{ padding: '16px 24px', borderTop: `1px solid ${t.border}`, background: t.bgSubtle, display:'flex', alignItems:'center', gap: 8 }}>
            <span style={{ fontSize: 11, color: t.textDim }}>⚡ Both legs submitted atomically. Auto-recovery on partial fill.</span>
            <div style={{ flex: 1 }}/>
            <window.Btn variant="ghost" onClick={() => setModal(null)}>Cancel</window.Btn>
            <window.Btn variant="primary" onClick={submit} icon="→">Submit trade</window.Btn>
          </div>
        </>
      )}

      {stage === 'submitting' && (
        <div style={{ padding: '60px 24px', textAlign:'center' }}>
          <div style={{ width: 50, height: 50, margin: '0 auto 18px', border: `3px solid ${t.border}`, borderTopColor: t.accent, borderRadius:'50%', animation: 'spin 0.8s linear infinite' }}/>
          <div style={{ fontSize: 15, fontWeight: 600, color: t.text, marginBottom: 6 }}>Submitting both legs…</div>
          <div style={{ fontSize: 12, color: t.textDim, fontFamily: window.FONTS.mono }}>YES → {o.yes_platform}  ·  NO → {o.no_platform}</div>
        </div>
      )}

      {stage === 'filled' && (
        <div style={{ padding: '40px 32px', textAlign:'center' }}>
          <div style={{ width: 60, height: 60, margin: '0 auto 16px', borderRadius:'50%', background: t.greenSoft, color: t.green, display:'flex', alignItems:'center', justifyContent:'center', fontSize: 28 }}>✓</div>
          <div style={{ fontSize: 18, fontWeight: 600, color: t.text, marginBottom: 4 }}>Trade filled</div>
          <div style={{ fontSize: 13, color: t.textDim, marginBottom: 24 }}>ARB-09422 · both legs settled within tolerance</div>
          <div style={{ display:'inline-block', padding: 16, background: t.greenSoft, borderRadius: 10, fontFamily: window.FONTS.mono, fontSize: 13, color: t.green, fontWeight: 600 }}>+{window.fmt$(profit, 4)} realized</div>
          <div style={{ marginTop: 28, display:'flex', gap: 8, justifyContent:'center' }}>
            <window.Btn variant="secondary" onClick={() => setModal(null)}>Close</window.Btn>
            <window.Btn variant="primary" icon="↗" onClick={() => { setModal(null); setPage('trades'); }}>View in trades</window.Btn>
          </div>
        </div>
      )}
    </Shell>
  );
}

function ConfirmLeg({ side, platform, price, qty }) {
  const { t } = window.useApp();
  return (
    <div style={{ border: `1px solid ${t.border}`, borderRadius: 10, padding: 14 }}>
      <div style={{ display:'flex', alignItems:'center', justifyContent:'space-between', marginBottom: 8 }}>
        <span style={{ fontSize: 10.5, fontWeight: 600, color: t.textMuted, letterSpacing:'0.06em' }}>{side} LEG</span>
        <window.PlatformChip name={platform}/>
      </div>
      <div style={{ fontFamily: window.FONTS.mono, fontSize: 12, color: t.textDim, lineHeight: 1.7 }}>
        <div>{qty} × ${price.toFixed(2)}</div>
        <div style={{ color: t.text, fontWeight: 600 }}>= {window.fmt$(price * qty)}</div>
      </div>
    </div>
  );
}

function Row({ k, v, bold, positive }) {
  const { t } = window.useApp();
  return (
    <div style={{ display:'flex', justifyContent:'space-between', padding: '3px 0', fontWeight: bold ? 700 : 400 }}>
      <span style={{ color: t.textDim }}>{k}</span>
      <span style={{ color: positive ? t.green : t.text }}>{v}</span>
    </div>
  );
}

// ── Trade detail ────────────────────────────────────────────────────
function TradeDetailModal() {
  const { t, modal, setModal } = window.useApp();
  const r = modal.payload;
  const tl = [
    { ok: true, label: 'Opportunity detected', t: 'T+0.000s', detail: `Net edge ${window.fmtC(4.8)} · 12 persistence scans` },
    { ok: true, label: 'Risk gates passed', t: 'T+0.012s', detail: '7/7 gates · auto-execute armed' },
    { ok: r.leg_yes.status === 'filled' || r.leg_yes.status === 'submitted', label: `YES leg → ${r.opportunity.yes_platform}`, t: 'T+0.032s', detail: `${r.leg_yes.fill_qty} @ $${r.leg_yes.fill_price.toFixed(2)} · ${r.leg_yes.status}` },
    { ok: r.leg_no.status === 'filled' || r.leg_no.status === 'submitted', label: `NO leg → ${r.opportunity.no_platform}`, t: 'T+0.041s', detail: `${r.leg_no.fill_qty} @ $${r.leg_no.fill_price.toFixed(2)} · ${r.leg_no.status}` },
    { ok: r.status === 'filled', label: 'Reconciled', t: 'T+1.840s', detail: r.status === 'filled' ? `Both legs settled · P&L ${window.fmt$Sign(r.realized_pnl)}` : r.status === 'recovering' ? 'Recovery flow in progress' : 'Failed leg refunded' },
  ];

  return (
    <Shell width={620}>
      <div style={{ padding: '20px 24px', borderBottom: `1px solid ${t.border}`, display:'flex', alignItems:'center', justifyContent:'space-between' }}>
        <div>
          <div style={{ display:'flex', alignItems:'center', gap: 10, marginBottom: 4 }}>
            <span style={{ fontFamily: window.FONTS.mono, fontSize: 13, fontWeight: 700, color: t.text }}>{r.arb_id}</span>
            <window.Pill tone={r.status === 'filled' ? 'green' : r.status === 'recovering' ? 'amber' : 'red'}>{r.status}</window.Pill>
          </div>
          <div style={{ fontSize: 13, color: t.textDim }}>{r.opportunity.description}</div>
        </div>
        <button onClick={() => setModal(null)} style={{ background:'none', border:'none', color: t.textMuted, fontSize: 20, cursor:'pointer' }}>✕</button>
      </div>

      <div style={{ padding: 24, overflow:'auto' }}>
        <div style={{ display:'grid', gridTemplateColumns:'repeat(3, 1fr)', gap: 12, marginBottom: 24 }}>
          <Tile label="Realized P&L" value={window.fmt$Sign(r.realized_pnl)} tone={r.realized_pnl >= 0 ? 'green' : 'red'}/>
          <Tile label="Submitted" value={window.tsTime(r.timestamp)} sub={window.tsDate(r.timestamp)}/>
          <Tile label="Total notional" value={window.fmt$((r.leg_yes.fill_qty * r.leg_yes.fill_price) + (r.leg_no.fill_qty * r.leg_no.fill_price))}/>
        </div>

        <div style={{ fontSize: 11, color: t.textMuted, letterSpacing:'0.06em', textTransform:'uppercase', marginBottom: 12 }}>EXECUTION TIMELINE</div>
        <div style={{ position:'relative' }}>
          {tl.map((step, i) => (
            <div key={i} style={{ display:'flex', gap: 14, paddingBottom: 18, position:'relative' }}>
              {i < tl.length - 1 && <div style={{ position:'absolute', left: 9, top: 22, bottom: 0, width: 2, background: t.border }}/>}
              <div style={{ width: 20, height: 20, borderRadius:'50%', background: step.ok ? t.green : t.red, color: '#fff', display:'flex', alignItems:'center', justifyContent:'center', fontSize: 11, fontWeight: 700, flexShrink: 0, zIndex: 1 }}>{step.ok ? '✓' : '✕'}</div>
              <div style={{ flex: 1, paddingTop: 0 }}>
                <div style={{ display:'flex', alignItems:'center', gap: 10, marginBottom: 2 }}>
                  <span style={{ fontSize: 13, color: t.text, fontWeight: 500 }}>{step.label}</span>
                  <span style={{ fontSize: 10.5, color: t.textMuted, fontFamily: window.FONTS.mono, marginLeft:'auto' }}>{step.t}</span>
                </div>
                <div style={{ fontSize: 11.5, color: t.textDim, fontFamily: window.FONTS.mono }}>{step.detail}</div>
              </div>
            </div>
          ))}
        </div>
      </div>

      <div style={{ padding: '14px 24px', borderTop: `1px solid ${t.border}`, background: t.bgSubtle, display:'flex', justifyContent:'flex-end', gap: 8 }}>
        <window.Btn variant="ghost" size="sm">Copy ID</window.Btn>
        <window.Btn variant="secondary" size="sm">View raw JSON</window.Btn>
      </div>
    </Shell>
  );
}

function Tile({ label, value, sub, tone }) {
  const { t } = window.useApp();
  const tones = { green: t.green, red: t.red };
  return (
    <div style={{ background: t.bgSubtle, padding: 14, borderRadius: 8 }}>
      <div style={{ fontSize: 10, color: t.textMuted, letterSpacing:'0.06em', textTransform:'uppercase', marginBottom: 4 }}>{label}</div>
      <div style={{ fontSize: 16, fontWeight: 600, color: tones[tone] || t.text, fontFamily: window.FONTS.mono }}>{value}</div>
      {sub && <div style={{ fontSize: 10.5, color: t.textMuted, marginTop: 2 }}>{sub}</div>}
    </div>
  );
}

// ── Deposit ─────────────────────────────────────────────────────────
function DepositModal() {
  const { t, modal, setModal } = window.useApp();
  const platform = modal.payload;
  const [amount, setAmount] = mUseState(100);
  const [stage, setStage] = mUseState('input');

  return (
    <Shell width={460}>
      <div style={{ padding: '20px 24px', borderBottom: `1px solid ${t.border}`, display:'flex', alignItems:'center', justifyContent:'space-between' }}>
        <div style={{ display:'flex', alignItems:'center', gap: 10 }}>
          <window.PlatformChip name={platform}/>
          <div style={{ fontSize: 16, fontWeight: 600, color: t.text }}>Deposit funds</div>
        </div>
        <button onClick={() => setModal(null)} style={{ background:'none', border:'none', color: t.textMuted, fontSize: 20, cursor:'pointer' }}>✕</button>
      </div>

      {stage === 'input' && (
        <>
          <div style={{ padding: 24 }}>
            <div style={{ fontSize: 11, color: t.textMuted, letterSpacing:'0.06em', textTransform:'uppercase', marginBottom: 10 }}>AMOUNT (USD)</div>
            <div style={{ display:'flex', alignItems:'center', background: t.bgSubtle, border: `1px solid ${t.border}`, borderRadius: 9, padding: '4px 14px', marginBottom: 12 }}>
              <span style={{ fontSize: 24, color: t.textDim, fontFamily: window.FONTS.mono }}>$</span>
              <input type="number" value={amount} onChange={e => setAmount(+e.target.value)} style={{ flex: 1, padding: '12px 8px', background:'transparent', border:'none', outline:'none', fontSize: 28, fontWeight: 600, color: t.text, fontFamily: window.FONTS.mono }}/>
            </div>
            <div style={{ display:'flex', gap: 6, marginBottom: 20 }}>
              {[50, 100, 250, 500, 1000].map(v => (
                <button key={v} onClick={() => setAmount(v)} style={{ flex: 1, padding: '6px 8px', background: amount === v ? t.bgSubtle : t.bgCard, border: `1px solid ${t.border}`, borderRadius: 6, fontSize: 11.5, color: amount === v ? t.text : t.textDim, fontWeight: 500, cursor:'pointer', fontFamily: window.FONTS.mono }}>${v}</button>
              ))}
            </div>
            <div style={{ background: t.bgSubtle, borderRadius: 8, padding: 14, fontSize: 11.5, color: t.textDim, lineHeight: 1.55 }}>
              {platform === 'kalshi' ? 'ACH transfer · 1–2 business days · no fee.' : 'USDC bridge · settles in ~30s · 0.05% fee on chain.'}
            </div>
          </div>
          <div style={{ padding: '14px 24px', borderTop: `1px solid ${t.border}`, display:'flex', gap: 8, justifyContent:'flex-end' }}>
            <window.Btn variant="ghost" onClick={() => setModal(null)}>Cancel</window.Btn>
            <window.Btn variant="primary" icon="↓" onClick={() => { setStage('done'); }}>Deposit ${amount}</window.Btn>
          </div>
        </>
      )}

      {stage === 'done' && (
        <div style={{ padding: '40px 24px', textAlign:'center' }}>
          <div style={{ width: 56, height: 56, margin: '0 auto 14px', borderRadius:'50%', background: t.greenSoft, color: t.green, display:'flex', alignItems:'center', justifyContent:'center', fontSize: 24 }}>✓</div>
          <div style={{ fontSize: 16, fontWeight: 600, color: t.text, marginBottom: 4 }}>Deposit initiated</div>
          <div style={{ fontSize: 12, color: t.textDim, marginBottom: 20, fontFamily: window.FONTS.mono }}>${amount.toFixed(2)} → {platform}</div>
          <window.Btn variant="primary" onClick={() => setModal(null)}>Done</window.Btn>
        </div>
      )}
    </Shell>
  );
}

window.Modal = Modal;
