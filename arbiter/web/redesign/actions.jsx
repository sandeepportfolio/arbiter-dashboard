// Toast system, confirm dialogs, popovers, extra modals (withdraw, market detail,
// new mapping, edit setting, user menu, notification detail).

const { useState: aUseS, useEffect: aUseE, useRef: aUseR } = React;

// ── Toast host ────────────────────────────────────────────────────
function ToastHost() {
  const { t, toasts } = window.useApp();
  if (!toasts || !toasts.length) return null;
  return (
    <div style={{ position:'fixed', bottom: 24, left: '50%', transform:'translateX(-50%)', zIndex: 300, display:'flex', flexDirection:'column', gap: 8, alignItems:'center', pointerEvents:'none' }}>
      {toasts.map(tt => (
        <div key={tt.id} style={{
          minWidth: 280, maxWidth: 440, padding: '11px 16px',
          background: t.text, color: t.bgCard, borderRadius: 10,
          boxShadow: t.shadowLg, fontSize: 12.5, fontWeight: 500,
          display:'flex', alignItems:'center', gap: 10,
          animation: 'toastIn 0.2s ease-out',
        }}>
          <span style={{
            width: 18, height: 18, borderRadius: '50%',
            background: tt.tone === 'error' ? t.red : tt.tone === 'warn' ? t.amber : t.green,
            color: '#fff', fontSize: 11, fontWeight: 700,
            display:'flex', alignItems:'center', justifyContent:'center', flexShrink: 0,
          }}>{tt.tone === 'error' ? '✕' : tt.tone === 'warn' ? '!' : '✓'}</span>
          <span style={{ flex: 1 }}>{tt.msg}</span>
          {tt.sub && <span style={{ opacity: 0.6, fontSize: 11, fontFamily: window.FONTS.mono }}>{tt.sub}</span>}
        </div>
      ))}
    </div>
  );
}

// ── Confirm dialog ────────────────────────────────────────────────
function ConfirmDialog() {
  const { t, confirm, setConfirm } = window.useApp();
  if (!confirm) return null;
  const c = confirm;
  return (
    <div onClick={() => setConfirm(null)} style={{ position:'fixed', inset: 0, background: t.overlay, zIndex: 96, display:'flex', justifyContent:'center', alignItems:'center', padding: 40 }}>
      <div onClick={e => e.stopPropagation()} style={{ width: 440, background: t.bgCard, border: `1px solid ${t.border}`, borderRadius: 12, boxShadow: t.shadowLg, overflow:'hidden' }}>
        <div style={{ padding: 22, display:'flex', gap: 14 }}>
          <div style={{
            width: 36, height: 36, borderRadius: 9, flexShrink: 0,
            background: c.tone === 'danger' ? t.redSoft : t.amberSoft,
            color: c.tone === 'danger' ? t.red : t.amber,
            display:'flex', alignItems:'center', justifyContent:'center', fontSize: 16, fontWeight: 700,
          }}>{c.tone === 'danger' ? '!' : '?'}</div>
          <div style={{ flex: 1 }}>
            <div style={{ fontSize: 15, fontWeight: 600, color: t.text, marginBottom: 6 }}>{c.title}</div>
            <div style={{ fontSize: 12.5, color: t.textDim, lineHeight: 1.5 }}>{c.body}</div>
          </div>
        </div>
        <div style={{ padding: '12px 22px', background: t.bgSubtle, borderTop: `1px solid ${t.border}`, display:'flex', justifyContent:'flex-end', gap: 8 }}>
          <window.Btn variant="ghost" size="sm" onClick={() => setConfirm(null)}>{c.cancelLabel || 'Cancel'}</window.Btn>
          <window.Btn variant={c.tone === 'danger' ? 'danger' : 'primary'} size="sm" onClick={() => { c.onConfirm?.(); setConfirm(null); }}>{c.confirmLabel || 'Confirm'}</window.Btn>
        </div>
      </div>
    </div>
  );
}

// ── Extra modal router ────────────────────────────────────────────
function ExtraModal() {
  const { modal } = window.useApp();
  if (!modal) return null;
  if (modal.kind === 'withdraw') return <WithdrawModal/>;
  if (modal.kind === 'market') return <MarketDetailModal/>;
  if (modal.kind === 'newMapping') return <NewMappingModal/>;
  if (modal.kind === 'editSetting') return <EditSettingModal/>;
  if (modal.kind === 'apiKey') return <ApiKeyModal/>;
  if (modal.kind === 'team') return <TeamModal/>;
  if (modal.kind === 'billing') return <BillingModal/>;
  if (modal.kind === 'notifPrefs') return <NotifPrefsModal/>;
  if (modal.kind === 'notification') return <NotificationModal/>;
  if (modal.kind === 'export') return <ExportModal/>;
  if (modal.kind === 'filter') return <FilterModal/>;
  if (modal.kind === 'profile') return <ProfileModal/>;
  return null;
}

function Shell({ children, width = 480 }) {
  const { t, setModal } = window.useApp();
  return (
    <div onClick={() => setModal(null)} style={{ position:'fixed', inset: 0, background: t.overlay, zIndex: 95, display:'flex', justifyContent:'center', alignItems:'center', padding: 40 }}>
      <div onClick={e => e.stopPropagation()} style={{ width, maxHeight: '90vh', background: t.bgCard, border: `1px solid ${t.border}`, borderRadius: 14, boxShadow: t.shadowLg, display:'flex', flexDirection:'column', overflow:'hidden' }}>
        {children}
      </div>
    </div>
  );
}
function Header({ kicker, title, onClose }) {
  const { t, setModal } = window.useApp();
  return (
    <div style={{ padding: '20px 24px', borderBottom: `1px solid ${t.border}`, display:'flex', alignItems:'center', justifyContent:'space-between' }}>
      <div>
        {kicker && <div style={{ fontSize: 11, color: t.textMuted, marginBottom: 3, letterSpacing:'0.04em', textTransform:'uppercase' }}>{kicker}</div>}
        <div style={{ fontSize: 16, fontWeight: 600, color: t.text }}>{title}</div>
      </div>
      <button onClick={() => { onClose ? onClose() : setModal(null); }} style={{ background:'none', border:'none', color: t.textMuted, fontSize: 20, cursor:'pointer' }}>✕</button>
    </div>
  );
}
function Footer({ children }) {
  const { t } = window.useApp();
  return <div style={{ padding: '14px 24px', borderTop: `1px solid ${t.border}`, background: t.bgSubtle, display:'flex', justifyContent:'flex-end', gap: 8 }}>{children}</div>;
}

// ── Withdraw ──────────────────────────────────────────────────────
function WithdrawModal() {
  const { t, modal, setModal, toast } = window.useApp();
  const platform = modal.payload;
  const max = window.MOCK.balances[platform].balance;
  const [amount, setAmount] = aUseS(Math.min(50, Math.floor(max)));
  const [stage, setStage] = aUseS('input');
  return (
    <Shell width={460}>
      <Header kicker="Withdraw funds" title={platform === 'kalshi' ? 'Withdraw from Kalshi' : 'Withdraw from Polymarket'}/>
      {stage === 'input' && (
        <>
          <div style={{ padding: 24 }}>
            <div style={{ fontSize: 11, color: t.textMuted, letterSpacing:'0.06em', textTransform:'uppercase', marginBottom: 8 }}>AMOUNT</div>
            <div style={{ display:'flex', alignItems:'center', background: t.bgSubtle, border: `1px solid ${t.border}`, borderRadius: 9, padding: '4px 14px', marginBottom: 10 }}>
              <span style={{ fontSize: 24, color: t.textDim, fontFamily: window.FONTS.mono }}>$</span>
              <input type="number" max={max} value={amount} onChange={e => setAmount(Math.min(max, +e.target.value))} style={{ flex: 1, padding: '12px 8px', background:'transparent', border:'none', outline:'none', fontSize: 28, fontWeight: 600, color: t.text, fontFamily: window.FONTS.mono }}/>
              <button onClick={() => setAmount(Math.floor(max))} style={{ padding:'4px 10px', background: t.bgCard, border:`1px solid ${t.border}`, borderRadius: 6, fontSize: 11, color: t.text, cursor:'pointer', fontWeight: 600 }}>MAX</button>
            </div>
            <div style={{ fontSize: 11.5, color: t.textDim, marginBottom: 18 }}>Available: <span style={{ fontFamily: window.FONTS.mono, color: t.text }}>{window.fmt$(max)}</span></div>
            <div style={{ background: t.amberSoft, border:`1px solid ${t.amber}30`, borderRadius: 8, padding: 12, fontSize: 11.5, color: t.amber, lineHeight: 1.5 }}>
              <strong>Withdrawing while trades are open</strong> can leave open positions. Arbiter will pause auto-execute on this platform until withdrawal settles.
            </div>
          </div>
          <Footer>
            <window.Btn variant="ghost" size="sm" onClick={() => setModal(null)}>Cancel</window.Btn>
            <window.Btn variant="primary" size="sm" icon="↑" onClick={() => { setStage('done'); toast(`Withdrawal queued · $${amount.toFixed(2)}`, { tone:'ok', sub: platform }); }}>Withdraw ${amount}</window.Btn>
          </Footer>
        </>
      )}
      {stage === 'done' && (
        <div style={{ padding: '40px 24px', textAlign:'center' }}>
          <div style={{ width: 56, height: 56, margin: '0 auto 14px', borderRadius:'50%', background: t.greenSoft, color: t.green, display:'flex', alignItems:'center', justifyContent:'center', fontSize: 24 }}>✓</div>
          <div style={{ fontSize: 16, fontWeight: 600, color: t.text, marginBottom: 4 }}>Withdrawal queued</div>
          <div style={{ fontSize: 12, color: t.textDim, marginBottom: 20, fontFamily: window.FONTS.mono }}>${amount.toFixed(2)} ← {platform}</div>
          <window.Btn variant="primary" size="sm" onClick={() => setModal(null)}>Done</window.Btn>
        </div>
      )}
    </Shell>
  );
}

// ── Market detail ─────────────────────────────────────────────────
function MarketDetailModal() {
  const { t, modal, setModal, setPage, setDrawer, toast, watchlist, setWatchlist } = window.useApp();
  const M = window.MOCK;
  const o = modal.payload;
  const inList = watchlist.includes(o.canonical_id);
  return (
    <Shell width={620}>
      <Header kicker="Market" title={o.description}/>
      <div style={{ padding: 24, overflow:'auto' }}>
        <div style={{ fontSize: 11, color: t.textMuted, fontFamily: window.FONTS.mono, marginBottom: 18 }}>{o.canonical_id}</div>
        <div style={{ display:'grid', gridTemplateColumns:'1fr 1fr', gap: 12, marginBottom: 22 }}>
          <PriceBox platform={o.yes_platform} side="YES" price={o.yes_price} fee={o.fee_rate_yes} vol={o.yes_volume}/>
          <PriceBox platform={o.no_platform} side="NO" price={o.no_price} fee={o.fee_rate_no} vol={o.no_volume}/>
        </div>
        <div style={{ background: t.bgSubtle, borderRadius: 10, padding: 16, fontFamily: window.FONTS.mono, fontSize: 12.5 }}>
          <Row k="Combined cost" v={`$${(o.yes_price + o.no_price).toFixed(4)} per pair`}/>
          <Row k="Fees" v={`$${(o.yes_price*o.fee_rate_yes + o.no_price*o.fee_rate_no).toFixed(4)} per pair`}/>
          <Row k="Net edge" v={`${o.net_edge_cents.toFixed(2)}¢`} bold tone={o.net_edge_cents >= 1 ? 'green' : 'amber'}/>
          <Row k="Persistence" v={`${o.persistence_count} scans`}/>
        </div>
        <div style={{ height: 120, marginTop: 18 }}>
          <window.AreaChart data={M.equity.slice(-30).map((d,i)=>({t:i,v:o.net_edge_cents + Math.sin(i/3)*0.4}))} stroke={t.green} grid={t.border} currency={false} showAxis={false}/>
        </div>
      </div>
      <Footer>
        <window.Btn variant="ghost" size="sm" onClick={() => { setWatchlist(w => inList ? w.filter(x=>x!==o.canonical_id) : [...w, o.canonical_id]); toast(inList ? 'Removed from watchlist' : 'Added to watchlist'); }}>{inList ? '★ Watching' : '☆ Watch'}</window.Btn>
        <window.Btn variant="secondary" size="sm" onClick={() => { setModal(null); setPage('mappings'); }}>View mapping</window.Btn>
        <window.Btn variant="primary" size="sm" icon="→" onClick={() => { const idx = M.opportunities.indexOf(o); setModal(null); setPage('opportunities'); setDrawer({ kind:'opp', payload: idx >= 0 ? idx : 0 }); }}>Open opportunity</window.Btn>
      </Footer>
    </Shell>
  );
}
function PriceBox({ platform, side, price, fee, vol }) {
  const { t } = window.useApp();
  return (
    <div style={{ border: `1px solid ${t.border}`, borderRadius: 10, padding: 14 }}>
      <div style={{ display:'flex', alignItems:'center', justifyContent:'space-between', marginBottom: 10 }}>
        <span style={{ fontSize: 10.5, fontWeight: 600, color: t.textMuted, letterSpacing:'0.06em' }}>{side} LEG</span>
        <window.PlatformChip name={platform}/>
      </div>
      <div style={{ fontSize: 22, fontWeight: 600, color: t.text, fontFamily: window.FONTS.mono, letterSpacing:'-0.01em' }}>${price.toFixed(2)}</div>
      <div style={{ fontSize: 10.5, color: t.textDim, marginTop: 6, fontFamily: window.FONTS.mono, lineHeight: 1.6 }}>
        <div>Fee {(fee*100).toFixed(2)}%</div>
        <div>Vol {vol.toLocaleString()}</div>
      </div>
    </div>
  );
}
function Row({ k, v, bold, tone }) {
  const { t } = window.useApp();
  const c = tone === 'green' ? t.green : tone === 'amber' ? t.amber : t.text;
  return (
    <div style={{ display:'flex', justifyContent:'space-between', padding: '4px 0', fontWeight: bold ? 700 : 400 }}>
      <span style={{ color: t.textDim }}>{k}</span>
      <span style={{ color: c }}>{v}</span>
    </div>
  );
}

// ── New mapping ───────────────────────────────────────────────────
function NewMappingModal() {
  const { t, setModal, toast } = window.useApp();
  const [k, setK] = aUseS('');
  const [p, setP] = aUseS('');
  return (
    <Shell width={520}>
      <Header kicker="Add mapping" title="New cross-platform mapping"/>
      <div style={{ padding: 24 }}>
        <Field label="Kalshi ticker" value={k} onChange={setK} placeholder="TRUMP-2028-NOM-Y" mono/>
        <Field label="Polymarket slug" value={p} onChange={setP} placeholder="will-trump-win-2028-gop-nomination" mono/>
        <div style={{ background: t.bgSubtle, borderRadius: 8, padding: 12, fontSize: 11.5, color: t.textDim, lineHeight: 1.55 }}>
          Arbiter will fetch both descriptions and run semantic similarity. If score ≥ 0.85, the mapping is auto-confirmed.
        </div>
      </div>
      <Footer>
        <window.Btn variant="ghost" size="sm" onClick={() => setModal(null)}>Cancel</window.Btn>
        <window.Btn variant="primary" size="sm" disabled={!k || !p} onClick={() => { setModal(null); toast('Mapping queued for scoring', { sub: k.slice(0, 18) }); }}>Score &amp; submit</window.Btn>
      </Footer>
    </Shell>
  );
}
function Field({ label, value, onChange, placeholder, mono, type = 'text' }) {
  const { t } = window.useApp();
  return (
    <div style={{ marginBottom: 16 }}>
      <div style={{ fontSize: 11, color: t.textDim, fontWeight: 500, marginBottom: 6, letterSpacing:'0.02em' }}>{label}</div>
      <input type={type} value={value} onChange={e => onChange(e.target.value)} placeholder={placeholder}
        style={{ width: '100%', padding: '10px 12px', background: t.bgCard, border: `1px solid ${t.borderBright}`, borderRadius: 7, fontSize: 13, color: t.text, outline:'none', fontFamily: mono ? window.FONTS.mono : window.FONTS.sans }}/>
    </div>
  );
}

// ── Edit setting (slider/value editor) ───────────────────────────
function EditSettingModal() {
  const { t, modal, setModal, settings, setSettings, toast } = window.useApp();
  const cfg = modal.payload; // { key, label, kind, min, max, step, unit, options }
  const [v, setV] = aUseS(settings[cfg.key]);
  return (
    <Shell width={460}>
      <Header kicker="Edit strategy parameter" title={cfg.label}/>
      <div style={{ padding: 24 }}>
        <div style={{ fontSize: 11.5, color: t.textDim, marginBottom: 18, lineHeight: 1.5 }}>{cfg.desc}</div>
        {cfg.kind === 'slider' && (
          <>
            <div style={{ display:'flex', alignItems:'center', gap: 12, marginBottom: 12 }}>
              <input type="range" min={cfg.min} max={cfg.max} step={cfg.step} value={v} onChange={e => setV(+e.target.value)} style={{ flex: 1, accentColor: t.accent }}/>
              <div style={{ width: 110, padding: '8px 12px', background: t.bgSubtle, border: `1px solid ${t.border}`, borderRadius: 7, fontFamily: window.FONTS.mono, fontSize: 14, fontWeight: 600, color: t.text, textAlign:'center' }}>{v}{cfg.unit || ''}</div>
            </div>
            <div style={{ display:'flex', justifyContent:'space-between', fontSize: 10.5, color: t.textMuted, fontFamily: window.FONTS.mono }}>
              <span>{cfg.min}{cfg.unit}</span><span>{cfg.max}{cfg.unit}</span>
            </div>
          </>
        )}
        {cfg.kind === 'toggle' && (
          <div style={{ display:'flex', gap: 8 }}>
            {['on','off'].map(opt => (
              <button key={opt} onClick={() => setV(opt)} style={{ flex: 1, padding: '14px', background: v === opt ? t.bgSubtle : t.bgCard, border: `1.5px solid ${v === opt ? t.text : t.border}`, borderRadius: 8, fontSize: 13, color: t.text, fontWeight: 600, cursor:'pointer', textTransform:'uppercase' }}>{opt}</button>
            ))}
          </div>
        )}
        {cfg.kind === 'options' && (
          <div style={{ display:'flex', flexDirection:'column', gap: 6 }}>
            {cfg.options.map(opt => (
              <button key={opt} onClick={() => setV(opt)} style={{ padding: '10px 14px', background: v === opt ? t.bgSubtle : t.bgCard, border: `1px solid ${v === opt ? t.text : t.border}`, borderRadius: 7, fontSize: 12.5, color: t.text, fontWeight: 500, cursor:'pointer', textAlign:'left' }}>{opt}</button>
            ))}
          </div>
        )}
      </div>
      <Footer>
        <window.Btn variant="ghost" size="sm" onClick={() => setModal(null)}>Cancel</window.Btn>
        <window.Btn variant="primary" size="sm" onClick={() => { setSettings(s => ({ ...s, [cfg.key]: v })); setModal(null); toast(`${cfg.label} updated`, { sub: String(v) + (cfg.unit||'') }); }}>Save</window.Btn>
      </Footer>
    </Shell>
  );
}

// ── API key modal ─────────────────────────────────────────────────
function ApiKeyModal() {
  const { t, setModal, toast } = window.useApp();
  const [revealed, setRevealed] = aUseS(false);
  const [copied, setCopied] = aUseS(false);
  const key = 'sk-arb-08FE-2c9d-4ab1-9f02-bd3e7a1c6f4a';
  const masked = '••••••••••••••••••••••••••••••••sk-arb-08FE';
  return (
    <Shell width={520}>
      <Header kicker="API access" title="Operator API key"/>
      <div style={{ padding: 24 }}>
        <div style={{ fontSize: 11.5, color: t.textDim, marginBottom: 14, lineHeight: 1.5 }}>Read-only key for external dashboards. Rotate immediately if exposed.</div>
        <div style={{ display:'flex', alignItems:'center', gap: 6, padding: '10px 12px', background: t.bgSubtle, border:`1px solid ${t.border}`, borderRadius: 8, marginBottom: 14, fontFamily: window.FONTS.mono, fontSize: 12, color: t.text }}>
          <span style={{ flex: 1, overflow:'hidden', textOverflow:'ellipsis', whiteSpace:'nowrap' }}>{revealed ? key : masked}</span>
          <button onClick={() => setRevealed(!revealed)} style={{ padding:'4px 8px', background: t.bgCard, border:`1px solid ${t.border}`, borderRadius: 5, fontSize: 11, color: t.text, cursor:'pointer' }}>{revealed ? 'Hide' : 'Reveal'}</button>
          <button onClick={() => { navigator.clipboard?.writeText(key).catch(()=>{}); setCopied(true); setTimeout(()=>setCopied(false), 1500); }} style={{ padding:'4px 8px', background: t.bgCard, border:`1px solid ${t.border}`, borderRadius: 5, fontSize: 11, color: t.text, cursor:'pointer' }}>{copied ? 'Copied' : 'Copy'}</button>
        </div>
        <div style={{ background: t.amberSoft, border:`1px solid ${t.amber}30`, borderRadius: 8, padding: 12, fontSize: 11.5, color: t.amber, lineHeight: 1.5 }}>Created Mar 14 · last used 8m ago. Rotating revokes immediately.</div>
      </div>
      <Footer>
        <window.Btn variant="ghost" size="sm" onClick={() => setModal(null)}>Close</window.Btn>
        <window.Btn variant="danger" size="sm" onClick={() => { setModal(null); toast('API key rotated', { sub:'old key revoked', tone:'warn' }); }}>Rotate key</window.Btn>
      </Footer>
    </Shell>
  );
}

// ── Team ──────────────────────────────────────────────────────────
function TeamModal() {
  const { t, setModal, toast } = window.useApp();
  const [members, setMembers] = aUseS([
    { name:'Sam Park', email:'sam@arbiter.app', role:'Operator', you: true },
    { name:'Riya Patel', email:'riya@arbiter.app', role:'Operator' },
    { name:'Mark Chen', email:'mark@arbiter.app', role:'Viewer' },
    { name:'Auditor', email:'audit@trustchain.io', role:'Auditor' },
  ]);
  const [inv, setInv] = aUseS('');
  return (
    <Shell width={520}>
      <Header kicker="Team" title="Members &amp; access"/>
      <div style={{ padding: 24 }}>
        <div style={{ display:'flex', gap: 8, marginBottom: 16 }}>
          <input value={inv} onChange={e => setInv(e.target.value)} placeholder="email@company.com" style={{ flex: 1, padding:'9px 12px', background: t.bgCard, border:`1px solid ${t.borderBright}`, borderRadius: 7, fontSize: 12, color: t.text, outline:'none' }}/>
          <window.Btn variant="primary" size="sm" disabled={!inv} onClick={() => { setMembers(m => [...m, {name: inv.split('@')[0], email: inv, role:'Viewer'}]); setInv(''); toast(`Invite sent to ${inv}`); }}>Invite</window.Btn>
        </div>
        <div style={{ border:`1px solid ${t.border}`, borderRadius: 8, overflow:'hidden' }}>
          {members.map((m, i) => (
            <div key={i} style={{ padding:'12px 14px', borderBottom: i < members.length - 1 ? `1px solid ${t.border}` : 'none', display:'flex', alignItems:'center', gap: 10 }}>
              <div style={{ width: 30, height: 30, borderRadius: 8, background: t.accent, color:'#fff', display:'flex', alignItems:'center', justifyContent:'center', fontSize: 11, fontWeight: 700 }}>{m.name.split(' ').map(s=>s[0]).join('').slice(0,2).toUpperCase()}</div>
              <div style={{ flex: 1 }}>
                <div style={{ fontSize: 12.5, color: t.text, fontWeight: 500 }}>{m.name} {m.you && <span style={{ fontSize: 10.5, color: t.textMuted }}>(you)</span>}</div>
                <div style={{ fontSize: 10.5, color: t.textDim, fontFamily: window.FONTS.mono }}>{m.email}</div>
              </div>
              <window.Pill tone={m.role === 'Operator' ? 'green' : m.role === 'Auditor' ? 'blue' : 'default'}>{m.role}</window.Pill>
              {!m.you && <button onClick={() => { setMembers(ms => ms.filter(x => x !== m)); toast(`${m.name} removed`); }} style={{ background:'none', border:'none', color: t.textMuted, fontSize: 14, cursor:'pointer' }}>✕</button>}
            </div>
          ))}
        </div>
      </div>
      <Footer><window.Btn variant="primary" size="sm" onClick={() => setModal(null)}>Done</window.Btn></Footer>
    </Shell>
  );
}

// ── Billing ───────────────────────────────────────────────────────
function BillingModal() {
  const { t, setModal, toast } = window.useApp();
  return (
    <Shell width={520}>
      <Header kicker="Billing" title="Subscription &amp; usage"/>
      <div style={{ padding: 24 }}>
        <div style={{ background: t.bgSubtle, borderRadius: 10, padding: 18, marginBottom: 16 }}>
          <div style={{ fontSize: 11, color: t.textMuted, letterSpacing:'0.06em', textTransform:'uppercase', marginBottom: 4 }}>CURRENT PLAN</div>
          <div style={{ fontSize: 22, fontWeight: 600, color: t.text, marginBottom: 4 }}>Operator · $99 / month</div>
          <div style={{ fontSize: 11.5, color: t.textDim }}>Renews May 14 · paid via card ending 4242</div>
        </div>
        <div style={{ display:'grid', gridTemplateColumns:'1fr 1fr 1fr', gap: 10, marginBottom: 16 }}>
          {[['Scans this cycle','387,210','of unlimited'],['Trades this cycle','1,847','of unlimited'],['Take rate','2.0%','of profit, capped $200']].map(([l,v,s]) => (
            <div key={l} style={{ padding:14, border:`1px solid ${t.border}`, borderRadius: 8 }}>
              <div style={{ fontSize: 10.5, color: t.textMuted, letterSpacing:'0.06em' }}>{l}</div>
              <div style={{ fontSize: 16, fontWeight: 600, color: t.text, fontFamily: window.FONTS.mono, marginTop: 4 }}>{v}</div>
              <div style={{ fontSize: 10.5, color: t.textDim, marginTop: 2 }}>{s}</div>
            </div>
          ))}
        </div>
        <div style={{ display:'flex', justifyContent:'space-between', alignItems:'center', padding: 14, border:`1px solid ${t.border}`, borderRadius: 8 }}>
          <div>
            <div style={{ fontSize: 12.5, color: t.text, fontWeight: 500 }}>•••• 4242</div>
            <div style={{ fontSize: 10.5, color: t.textDim }}>Visa · expires 09/27</div>
          </div>
          <window.Btn variant="ghost" size="sm" onClick={() => toast('Card update form would open here')}>Update card</window.Btn>
        </div>
      </div>
      <Footer>
        <window.Btn variant="ghost" size="sm" onClick={() => toast('Invoice downloaded')}>Download invoice</window.Btn>
        <window.Btn variant="primary" size="sm" onClick={() => setModal(null)}>Done</window.Btn>
      </Footer>
    </Shell>
  );
}

// ── Notif prefs ───────────────────────────────────────────────────
function NotifPrefsModal() {
  const { t, setModal, notifPrefs, setNotifPrefs, toast } = window.useApp();
  const [p, setP] = aUseS(notifPrefs);
  const items = [
    ['killSwitch','Kill switch trips','Always alert immediately'],
    ['recovery','Recovery flows','When auto-recovery engages'],
    ['fail','Trade failures','Any leg fails after submission'],
    ['daily','Daily P&L summary','End-of-day rollup at 17:00 ET'],
    ['mapping','New mapping suggestions','Score ≥ 0.85'],
    ['deposit','Deposit settled','Funds available for trading'],
  ];
  return (
    <Shell width={520}>
      <Header kicker="Notifications" title="Email &amp; alert preferences"/>
      <div style={{ padding: 24 }}>
        {items.map(([k, l, d], i) => (
          <div key={k} style={{ padding: '14px 0', borderBottom: i < items.length - 1 ? `1px solid ${t.border}` : 'none', display:'flex', alignItems:'center', gap: 12 }}>
            <div style={{ flex: 1 }}>
              <div style={{ fontSize: 12.5, color: t.text, fontWeight: 500 }}>{l}</div>
              <div style={{ fontSize: 11, color: t.textDim }}>{d}</div>
            </div>
            <Switch on={p[k]} onChange={v => setP({...p, [k]: v})}/>
          </div>
        ))}
      </div>
      <Footer>
        <window.Btn variant="ghost" size="sm" onClick={() => setModal(null)}>Cancel</window.Btn>
        <window.Btn variant="primary" size="sm" onClick={() => { setNotifPrefs(p); setModal(null); toast('Notification preferences saved'); }}>Save</window.Btn>
      </Footer>
    </Shell>
  );
}
function Switch({ on, onChange }) {
  const { t } = window.useApp();
  return (
    <button onClick={() => onChange(!on)} style={{ width: 36, height: 20, padding: 0, background: on ? t.green : t.border, border:'none', borderRadius: 99, position:'relative', cursor:'pointer', flexShrink: 0 }}>
      <span style={{ position:'absolute', top: 2, left: on ? 18 : 2, width: 16, height: 16, borderRadius:'50%', background:'#fff', transition:'left 0.15s' }}/>
    </button>
  );
}
window.Switch = Switch;

// ── Notification detail ───────────────────────────────────────────
function NotificationModal() {
  const { t, modal, setModal, setPage } = window.useApp();
  const n = modal.payload;
  return (
    <Shell width={480}>
      <Header kicker={`Alert · ${n.ts} ago`} title={n.title}/>
      <div style={{ padding: 24 }}>
        <div style={{ fontSize: 13, color: t.text, marginBottom: 14, lineHeight: 1.55 }}>{n.body}</div>
        {n.detail && <div style={{ background: t.bgSubtle, borderRadius: 8, padding: 14, fontFamily: window.FONTS.mono, fontSize: 11.5, color: t.textDim, lineHeight: 1.6, whiteSpace:'pre-wrap' }}>{n.detail}</div>}
      </div>
      <Footer>
        <window.Btn variant="ghost" size="sm" onClick={() => setModal(null)}>Dismiss</window.Btn>
        {n.go && <window.Btn variant="primary" size="sm" onClick={() => { setModal(null); setPage(n.go); }}>Go to {n.go}</window.Btn>}
      </Footer>
    </Shell>
  );
}

// ── Export ────────────────────────────────────────────────────────
function ExportModal() {
  const { t, modal, setModal, toast } = window.useApp();
  const what = modal.payload || 'data';
  const [fmt, setFmt] = aUseS('csv');
  const [range, setRange] = aUseS('30d');
  return (
    <Shell width={420}>
      <Header kicker="Download" title={`Export ${what}`}/>
      <div style={{ padding: 22 }}>
        <div style={{ fontSize: 11, color: t.textMuted, letterSpacing:'0.06em', textTransform:'uppercase', marginBottom: 8 }}>FORMAT</div>
        <div style={{ display:'flex', gap: 6, marginBottom: 16 }}>
          {['csv','json','xlsx'].map(f => (
            <button key={f} onClick={() => setFmt(f)} style={{ flex: 1, padding:'8px', background: fmt === f ? t.bgSubtle : t.bgCard, border: `1px solid ${fmt === f ? t.text : t.border}`, borderRadius: 6, fontSize: 12, color: t.text, cursor:'pointer', fontFamily: window.FONTS.mono, textTransform:'uppercase' }}>{f}</button>
          ))}
        </div>
        <div style={{ fontSize: 11, color: t.textMuted, letterSpacing:'0.06em', textTransform:'uppercase', marginBottom: 8 }}>RANGE</div>
        <div style={{ display:'flex', gap: 6 }}>
          {['7d','30d','90d','All'].map(r => (
            <button key={r} onClick={() => setRange(r)} style={{ flex: 1, padding:'8px', background: range === r ? t.bgSubtle : t.bgCard, border: `1px solid ${range === r ? t.text : t.border}`, borderRadius: 6, fontSize: 12, color: t.text, cursor:'pointer', fontFamily: window.FONTS.mono }}>{r}</button>
          ))}
        </div>
      </div>
      <Footer>
        <window.Btn variant="ghost" size="sm" onClick={() => setModal(null)}>Cancel</window.Btn>
        <window.Btn variant="primary" size="sm" icon="↓" onClick={() => { setModal(null); toast(`Export ready · ${what}.${fmt}`, { sub: `${range} range` }); }}>Download</window.Btn>
      </Footer>
    </Shell>
  );
}

// ── Filter ────────────────────────────────────────────────────────
function FilterModal() {
  const { t, modal, setModal, filters, setFilters, toast } = window.useApp();
  const what = modal.payload?.kind || 'trades';
  const [f, setF] = aUseS({ ...(filters[what] || {}) });
  return (
    <Shell width={460}>
      <Header kicker="Filter" title={`Filter ${what}`}/>
      <div style={{ padding: 22 }}>
        <div style={{ fontSize: 11, color: t.textMuted, letterSpacing:'0.06em', textTransform:'uppercase', marginBottom: 8 }}>PLATFORM</div>
        <div style={{ display:'flex', gap: 6, marginBottom: 16 }}>
          {['both','kalshi','polymarket'].map(p => (
            <button key={p} onClick={() => setF({...f, platform: p})} style={{ flex: 1, padding:'8px', background: f.platform === p ? t.bgSubtle : t.bgCard, border: `1px solid ${f.platform === p ? t.text : t.border}`, borderRadius: 6, fontSize: 11.5, color: t.text, cursor:'pointer', textTransform:'capitalize' }}>{p}</button>
          ))}
        </div>
        <div style={{ fontSize: 11, color: t.textMuted, letterSpacing:'0.06em', textTransform:'uppercase', marginBottom: 8 }}>MIN P&amp;L</div>
        <div style={{ display:'flex', gap: 6, marginBottom: 16 }}>
          {[['any','Any'],['pos','Profit only'],['neg','Loss only']].map(([k, l]) => (
            <button key={k} onClick={() => setF({...f, pnl: k})} style={{ flex: 1, padding:'8px', background: f.pnl === k ? t.bgSubtle : t.bgCard, border: `1px solid ${f.pnl === k ? t.text : t.border}`, borderRadius: 6, fontSize: 11.5, color: t.text, cursor:'pointer' }}>{l}</button>
          ))}
        </div>
        <div style={{ fontSize: 11, color: t.textMuted, letterSpacing:'0.06em', textTransform:'uppercase', marginBottom: 8 }}>DATE RANGE</div>
        <div style={{ display:'flex', gap: 6 }}>
          {['1h','6h','24h','7d','All'].map(r => (
            <button key={r} onClick={() => setF({...f, range: r})} style={{ flex: 1, padding:'8px', background: f.range === r ? t.bgSubtle : t.bgCard, border: `1px solid ${f.range === r ? t.text : t.border}`, borderRadius: 6, fontSize: 11.5, color: t.text, cursor:'pointer', fontFamily: window.FONTS.mono }}>{r}</button>
          ))}
        </div>
      </div>
      <Footer>
        <window.Btn variant="ghost" size="sm" onClick={() => { setFilters(prev => ({ ...prev, [what]: {} })); setModal(null); toast('Filters cleared'); }}>Reset</window.Btn>
        <window.Btn variant="primary" size="sm" onClick={() => { setFilters(prev => ({ ...prev, [what]: f })); setModal(null); toast('Filters applied'); }}>Apply</window.Btn>
      </Footer>
    </Shell>
  );
}

// ── Profile ───────────────────────────────────────────────────────
function ProfileModal() {
  const { t, setModal, profile, setProfile, toast } = window.useApp();
  const [p, setP] = aUseS({...profile});
  return (
    <Shell width={460}>
      <Header kicker="Operator profile" title="Edit profile"/>
      <div style={{ padding: 24 }}>
        <div style={{ display:'flex', alignItems:'center', gap: 14, marginBottom: 20 }}>
          <div style={{ width: 56, height: 56, borderRadius: 14, background: t.accent, color:'#fff', display:'flex', alignItems:'center', justifyContent:'center', fontSize: 22, fontWeight: 700 }}>{p.initials}</div>
          <button onClick={() => toast('Avatar upload would open here')} style={{ padding:'6px 12px', background: t.bgCard, border:`1px solid ${t.border}`, borderRadius: 7, fontSize: 11.5, color: t.text, cursor:'pointer' }}>Change avatar</button>
        </div>
        <Field label="Display name" value={p.name} onChange={v => setP({...p, name: v})}/>
        <Field label="Email" value={p.email} onChange={v => setP({...p, email: v})}/>
        <Field label="Initials" value={p.initials} onChange={v => setP({...p, initials: v.slice(0,2).toUpperCase()})}/>
      </div>
      <Footer>
        <window.Btn variant="ghost" size="sm" onClick={() => setModal(null)}>Cancel</window.Btn>
        <window.Btn variant="primary" size="sm" onClick={() => { setProfile(p); setModal(null); toast('Profile saved'); }}>Save</window.Btn>
      </Footer>
    </Shell>
  );
}

// ── User dropdown ─────────────────────────────────────────────────
function UserMenu() {
  const { t, userMenu, setUserMenu, setModal, signOut, profile, setPage } = window.useApp();
  if (!userMenu) return null;
  const items = [
    ['◐','Profile', () => setModal({ kind:'profile' })],
    ['⚙','Settings', () => setPage('settings')],
    ['$','Billing', () => setModal({ kind:'billing' })],
    ['◈','Switch workspace', () => setUserMenu(false)],
    ['?','Help &amp; docs', () => window.open('about:blank','_blank')],
  ];
  return (
    <div onClick={() => setUserMenu(false)} style={{ position:'fixed', inset: 0, zIndex: 80 }}>
      <div onClick={e => e.stopPropagation()} style={{ position:'absolute', top: 60, right: 28, width: 240, background: t.bgCard, border: `1px solid ${t.border}`, borderRadius: 10, boxShadow: t.shadowLg, overflow:'hidden' }}>
        <div style={{ padding: '14px 16px', borderBottom: `1px solid ${t.border}`, display:'flex', alignItems:'center', gap: 10 }}>
          <div style={{ width: 36, height: 36, borderRadius: 9, background: t.accent, color:'#fff', display:'flex', alignItems:'center', justifyContent:'center', fontSize: 13, fontWeight: 700 }}>{profile.initials}</div>
          <div style={{ flex: 1, minWidth: 0 }}>
            <div style={{ fontSize: 13, fontWeight: 600, color: t.text }}>{profile.name}</div>
            <div style={{ fontSize: 10.5, color: t.textDim, fontFamily: window.FONTS.mono, overflow:'hidden', textOverflow:'ellipsis' }}>{profile.email}</div>
          </div>
        </div>
        <div style={{ padding: 6 }}>
          {items.map(([ic, l, fn]) => (
            <button key={l} onClick={() => { fn(); setUserMenu(false); }} style={{ width:'100%', padding: '8px 10px', background:'none', border:'none', display:'flex', alignItems:'center', gap: 10, fontSize: 12.5, color: t.text, cursor:'pointer', borderRadius: 6 }} onMouseEnter={e => e.currentTarget.style.background = t.bgSubtle} onMouseLeave={e => e.currentTarget.style.background = 'transparent'}>
              <span style={{ width: 18, fontSize: 12, color: t.textMuted, fontFamily: window.FONTS.mono, textAlign:'center' }} dangerouslySetInnerHTML={{__html: ic}}/>
              <span dangerouslySetInnerHTML={{__html: l}}/>
            </button>
          ))}
        </div>
        <div style={{ padding: 6, borderTop:`1px solid ${t.border}` }}>
          <button onClick={() => { setUserMenu(false); signOut(); }} style={{ width:'100%', padding: '8px 10px', background:'none', border:'none', display:'flex', alignItems:'center', gap: 10, fontSize: 12.5, color: t.red, cursor:'pointer', borderRadius: 6 }} onMouseEnter={e => e.currentTarget.style.background = t.redSoft} onMouseLeave={e => e.currentTarget.style.background = 'transparent'}>
            <span style={{ width: 18, fontFamily: window.FONTS.mono, textAlign:'center' }}>⏏</span>
            Sign out
          </button>
        </div>
      </div>
    </div>
  );
}

window.ToastHost = ToastHost;
window.ConfirmDialog = ConfirmDialog;
window.ExtraModal = ExtraModal;
window.UserMenu = UserMenu;
