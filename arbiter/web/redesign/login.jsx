// Login / Connect screen
function LoginScreen({ onSignIn }) {
  const { t, name, toggle } = window.useApp();
  const M = window.MOCK;

  return (
    <div style={{ width: '100%', height: '100%', background: t.bg, color: t.text, fontFamily: window.FONTS.sans, display:'flex', overflow: 'hidden' }}>
      {/* Left side - form */}
      <div style={{ flex: '0 0 480px', padding: '48px 56px', display:'flex', flexDirection:'column', borderRight: `1px solid ${t.border}`, background: t.bgCard }}>
        <div style={{ display:'flex', alignItems:'center', gap: 10, marginBottom: 64 }}>
          <div style={{ width: 30, height: 30, borderRadius: 8, background: t.text, color: t.bgCard, display:'flex', alignItems:'center', justifyContent:'center', fontSize: 14, fontWeight: 700 }}>A</div>
          <div style={{ fontSize: 15, fontWeight: 600, letterSpacing:'-0.01em' }}>Arbiter</div>
          <button onClick={toggle} style={{ marginLeft:'auto', width: 30, height: 30, background: t.bgSubtle, border: `1px solid ${t.border}`, borderRadius: 7, color: t.text, cursor:'pointer' }}>{name === 'light' ? '☾' : '☀'}</button>
        </div>

        <div style={{ flex: 1, display:'flex', flexDirection:'column', justifyContent:'center', maxWidth: 360 }}>
          <div style={{ fontSize: 11, color: t.textMuted, letterSpacing:'0.08em', textTransform:'uppercase', marginBottom: 10 }}>Sign in to your desk</div>
          <div style={{ fontSize: 28, fontWeight: 600, color: t.text, letterSpacing:'-0.02em', lineHeight: 1.2, marginBottom: 12 }}>Resume operations</div>
          <div style={{ fontSize: 13, color: t.textDim, marginBottom: 32, lineHeight: 1.55 }}>Connect your Kalshi and Polymarket accounts to begin scanning. Arbiter never custodies funds — execution happens via signed API requests on your existing accounts.</div>

          <div style={{ marginBottom: 14 }}>
            <div style={{ fontSize: 11, color: t.textDim, fontWeight: 500, marginBottom: 6 }}>Email</div>
            <input defaultValue="sam@arbiter.app" style={{ width: '100%', padding: '11px 14px', background: t.bgCard, border: `1px solid ${t.borderBright}`, borderRadius: 8, fontSize: 13, color: t.text, outline:'none' }}/>
          </div>
          <div style={{ marginBottom: 18 }}>
            <div style={{ display:'flex', justifyContent:'space-between', marginBottom: 6 }}>
              <div style={{ fontSize: 11, color: t.textDim, fontWeight: 500 }}>Password</div>
              <a style={{ fontSize: 11, color: t.accent, cursor:'pointer' }}>Forgot?</a>
            </div>
            <input type="password" defaultValue="••••••••••••" style={{ width: '100%', padding: '11px 14px', background: t.bgCard, border: `1px solid ${t.borderBright}`, borderRadius: 8, fontSize: 13, color: t.text, outline:'none', fontFamily: window.FONTS.mono }}/>
          </div>

          <button onClick={onSignIn} style={{ padding: '12px 16px', background: t.text, color: t.bgCard, border:'none', borderRadius: 8, fontSize: 13, fontWeight: 600, cursor:'pointer', marginBottom: 14 }}>Sign in →</button>

          <div style={{ display:'flex', alignItems:'center', gap: 10, marginBottom: 14 }}>
            <div style={{ flex: 1, height: 1, background: t.border }}/>
            <span style={{ fontSize: 11, color: t.textMuted }}>or continue with</span>
            <div style={{ flex: 1, height: 1, background: t.border }}/>
          </div>

          <div style={{ display:'grid', gridTemplateColumns:'1fr 1fr', gap: 8 }}>
            <button style={{ padding: '10px', background: t.bgCard, border: `1px solid ${t.border}`, borderRadius: 8, fontSize: 12, color: t.text, cursor:'pointer', fontWeight: 500 }}>SSO / Okta</button>
            <button style={{ padding: '10px', background: t.bgCard, border: `1px solid ${t.border}`, borderRadius: 8, fontSize: 12, color: t.text, cursor:'pointer', fontWeight: 500 }}>Hardware key</button>
          </div>
        </div>

        <div style={{ fontSize: 10.5, color: t.textMuted, lineHeight: 1.5, paddingTop: 24, borderTop: `1px solid ${t.border}` }}>
          By signing in you agree to the operator code of conduct: no manual override of risk gates, no off-platform settlement, no shared credentials.
        </div>
      </div>

      {/* Right side - status */}
      <div style={{ flex: 1, padding: '48px 56px', display:'flex', flexDirection:'column', justifyContent:'center' }}>
        <div style={{ fontSize: 11, color: t.textMuted, letterSpacing:'0.08em', textTransform:'uppercase', marginBottom: 14 }}>System status — public</div>
        <div style={{ fontSize: 22, fontWeight: 600, color: t.text, letterSpacing:'-0.018em', marginBottom: 32 }}>All systems nominal</div>

        <div style={{ display:'grid', gridTemplateColumns:'1fr 1fr', gap: 12, marginBottom: 24 }}>
          <StatusTile ok label="Kalshi gateway" detail="API healthy · 142ms"/>
          <StatusTile ok label="Polymarket CLOB" detail="API healthy · 188ms"/>
          <StatusTile ok label="Scanner cluster" detail="3/3 workers live"/>
          <StatusTile ok label="Reconciliation" detail="0 outstanding flags"/>
        </div>

        <div style={{ background: t.bgCard, border: `1px solid ${t.border}`, borderRadius: 12, padding: 22, marginBottom: 16 }}>
          <div style={{ display:'flex', alignItems:'center', justifyContent:'space-between', marginBottom: 14 }}>
            <div style={{ fontSize: 12, color: t.textDim, fontWeight: 500 }}>Last 24 hours</div>
            <div style={{ display:'flex', gap: 12, fontFamily: window.FONTS.mono, fontSize: 11.5 }}>
              <span style={{ color: t.textDim }}>Scans <span style={{ color: t.text, fontWeight: 600 }}>18,420</span></span>
              <span style={{ color: t.textDim }}>Trades <span style={{ color: t.text, fontWeight: 600 }}>87</span></span>
              <span style={{ color: t.textDim }}>Best edge <span style={{ color: t.green, fontWeight: 600 }}>4.8¢</span></span>
            </div>
          </div>
          <div style={{ height: 100 }}>
            <window.AreaChart data={M.health.scanner.history.map((h, i) => ({ t: i, v: h.best_edge_cents }))} stroke={t.accent} grid={t.border} currency={false} showAxis={false}/>
          </div>
          <div style={{ fontSize: 10, color: t.textMuted, marginTop: 6, textAlign:'center' }}>Best edge — last 5 minutes</div>
        </div>

        <div style={{ display:'flex', alignItems:'center', gap: 10, padding: 14, background: t.bgCard, border: `1px solid ${t.border}`, borderRadius: 10 }}>
          <div style={{ width: 28, height: 28, borderRadius: 7, background: t.redSoft, color: t.red, display:'flex', alignItems:'center', justifyContent:'center', fontSize: 14, fontWeight: 700 }}>!</div>
          <div style={{ flex: 1 }}>
            <div style={{ fontSize: 12, fontWeight: 600, color: t.text }}>Need to halt trading?</div>
            <div style={{ fontSize: 11, color: t.textDim }}>Operators can engage the kill switch from the audit page after sign-in.</div>
          </div>
          <button style={{ padding: '6px 12px', background: 'transparent', border: `1px solid ${t.red}`, color: t.red, borderRadius: 7, fontSize: 11.5, fontWeight: 600, cursor:'pointer' }}>Emergency contact</button>
        </div>
      </div>
    </div>
  );
}

function StatusTile({ ok, label, detail }) {
  const { t } = window.useApp();
  return (
    <div style={{ background: t.bgCard, border: `1px solid ${t.border}`, borderRadius: 10, padding: 16 }}>
      <div style={{ display:'flex', alignItems:'center', gap: 6, marginBottom: 6 }}>
        <span style={{ width: 7, height: 7, borderRadius:'50%', background: ok ? t.green : t.red, animation: 'pulse 2s infinite' }}/>
        <span style={{ fontSize: 11, color: t.textDim, fontWeight: 500 }}>{label}</span>
      </div>
      <div style={{ fontSize: 13, fontWeight: 600, color: ok ? t.green : t.red, marginBottom: 2 }}>{ok ? 'Operational' : 'Degraded'}</div>
      <div style={{ fontSize: 10.5, color: t.textMuted, fontFamily: window.FONTS.mono }}>{detail}</div>
    </div>
  );
}

window.LoginScreen = LoginScreen;
