// Shared UI primitives + format helpers used across all pages.
const fmt$ = (v, d = 2) => v == null ? '—' : (v < 0 ? '−$' : '$') + Math.abs(v).toFixed(d);
const fmt$Sign = (v, d = 2) => v == null ? '—' : (v >= 0 ? '+$' : '−$') + Math.abs(v).toFixed(d);
const fmtC = v => v == null ? '—' : v.toFixed(1) + '¢';
const fmtPct = (v, d = 2) => v == null ? '—' : (v >= 0 ? '+' : '') + (v * 100).toFixed(d) + '%';
const ago = ts => {
  const s = Math.floor(Date.now()/1000 - ts);
  if (s < 60) return s + 's ago';
  if (s < 3600) return Math.floor(s/60) + 'm ago';
  if (s < 86400) return Math.floor(s/3600) + 'h ago';
  return Math.floor(s/86400) + 'd ago';
};
const tsTime = ts => {
  const d = new Date(ts * 1000);
  return d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
};
const tsDate = ts => {
  const d = new Date(ts * 1000);
  return d.toLocaleDateString([], { month: 'short', day: 'numeric' });
};

window.fmt$ = fmt$;
window.fmt$Sign = fmt$Sign;
window.fmtC = fmtC;
window.fmtPct = fmtPct;
window.ago = ago;
window.tsTime = tsTime;
window.tsDate = tsDate;

// ── Card ────────────────────────────────────────────────────────────
function Card({ title, action, children, padding = 20, style = {} }) {
  const { t } = window.useApp();
  return (
    <div style={{ background: t.bgCard, border: `1px solid ${t.border}`, borderRadius: 10, ...style }}>
      {(title || action) && (
        <div style={{ padding: '14px 18px', borderBottom: `1px solid ${t.border}`, display:'flex', alignItems:'center', justifyContent:'space-between' }}>
          <div style={{ fontSize: 12, fontWeight: 600, color: t.text, letterSpacing: '-0.005em' }}>{title}</div>
          {action}
        </div>
      )}
      <div style={{ padding }}>{children}</div>
    </div>
  );
}

// ── Stat ────────────────────────────────────────────────────────────
function Stat({ label, value, sub, tone, mono, big }) {
  const { t } = window.useApp();
  const tones = {
    green: t.green, red: t.red, amber: t.amber, accent: t.accent, default: t.text,
  };
  const c = tones[tone] || tones.default;
  return (
    <div>
      <div style={{ fontSize: 11, color: t.textMuted, letterSpacing: '0.04em', textTransform: 'uppercase', marginBottom: 6 }}>{label}</div>
      <div style={{ fontSize: big ? 26 : 20, fontWeight: 600, color: c, letterSpacing: '-0.015em', fontFamily: mono ? window.FONTS.mono : window.FONTS.sans }}>{value}</div>
      {sub && <div style={{ fontSize: 11, color: t.textDim, marginTop: 4 }}>{sub}</div>}
    </div>
  );
}

// ── Pill / Badge ────────────────────────────────────────────────────
function Pill({ children, tone = 'default', size = 'sm' }) {
  const { t } = window.useApp();
  const map = {
    green:   [t.green, t.greenSoft],
    red:     [t.red, t.redSoft],
    amber:   [t.amber, t.amberSoft],
    blue:    [t.blue, t.blueSoft],
    purple:  [t.purple, t.purpleSoft],
    accent:  [t.accent, t.accentSoft],
    default: [t.textDim, t.bgSubtle],
  };
  const [fg, bg] = map[tone] || map.default;
  const sm = size === 'sm';
  return (
    <span style={{ display: 'inline-flex', alignItems: 'center', gap: 4, padding: sm ? '2px 8px' : '4px 11px', background: bg, color: fg, borderRadius: 99, fontSize: sm ? 10.5 : 11.5, fontWeight: 600, letterSpacing: '0.02em', whiteSpace:'nowrap', lineHeight: 1.5, textTransform: 'capitalize', verticalAlign: 'middle' }}>
      {children}
    </span>
  );
}

// ── PlatformChip — Kalshi / Polymarket ──────────────────────────────
function PlatformChip({ name, side }) {
  const { t } = window.useApp();
  const cfg = name === 'kalshi'
    ? { fg: t.green, bg: t.greenSoft, label: 'Kalshi' }
    : { fg: t.purple, bg: t.purpleSoft, label: 'Polymarket' };
  return (
    <span style={{ display:'inline-flex', alignItems:'center', gap: 5, padding:'2px 8px', background: cfg.bg, color: cfg.fg, borderRadius: 5, fontSize: 11, fontWeight: 600 }}>
      <span style={{ width: 5, height: 5, borderRadius: '50%', background: cfg.fg }}/>
      {cfg.label}{side && <span style={{ opacity: 0.7, marginLeft: 2 }}>· {side}</span>}
    </span>
  );
}

// ── Button ──────────────────────────────────────────────────────────
function Btn({ children, variant = 'secondary', size = 'md', onClick, icon, disabled }) {
  const { t } = window.useApp();
  const sm = size === 'sm';
  const variants = {
    primary:   { bg: t.text, fg: t.bgCard, bd: t.text },
    secondary: { bg: t.bgCard, fg: t.text, bd: t.border },
    ghost:     { bg: 'transparent', fg: t.text, bd: 'transparent' },
    danger:    { bg: t.red, fg: '#fff', bd: t.red },
    accent:    { bg: t.accent, fg: '#fff', bd: t.accent },
  };
  const v = variants[variant];
  return (
    <button onClick={onClick} disabled={disabled} style={{
      display:'inline-flex', alignItems:'center', gap: 6,
      padding: sm ? '5px 10px' : '7px 14px',
      background: v.bg, color: v.fg, border: `1px solid ${v.bd}`, borderRadius: 7,
      fontSize: sm ? 11.5 : 12.5, fontWeight: 500, fontFamily: 'inherit',
      cursor: disabled ? 'not-allowed' : 'pointer', opacity: disabled ? 0.5 : 1,
      letterSpacing: '-0.005em', whiteSpace: 'nowrap',
    }}>
      {icon && <span style={{ fontFamily: window.FONTS.mono, fontSize: sm ? 11 : 12 }}>{icon}</span>}
      {children}
    </button>
  );
}

// ── DataTable ───────────────────────────────────────────────────────
function DataTable({ columns, rows, onRowClick }) {
  const { t } = window.useApp();
  // For each column, build a track. Fixed widths (e.g. '110px') stay fixed;
  // '1fr' becomes minmax(<minw>, 1fr) so flex columns never collapse.
  const trackFor = (c) => {
    const w = c.w || '1fr';
    if (w === '1fr' || /fr$/.test(w)) {
      const min = c.min || 220;
      return `minmax(${min}px, ${w})`;
    }
    return w;
  };
  // Sum of fixed-px widths + each flex col's min — gives table its min-width
  // so the row scrolls horizontally instead of crushing on narrow viewports.
  const minTotal = columns.reduce((acc, c) => {
    const w = c.w || '1fr';
    if (/^\d+px$/.test(w)) return acc + parseInt(w, 10);
    if (w === '1fr' || /fr$/.test(w)) return acc + (c.min || 220);
    return acc + 100;
  }, 0) + 36; // padding
  const tracks = columns.map(trackFor).join(' ');
  return (
    <div style={{ overflowX: 'auto', overflowY: 'hidden' }}>
      <div style={{ minWidth: minTotal }}>
        <div style={{ display:'grid', gridTemplateColumns: tracks, padding: '10px 18px', fontSize: 10.5, fontWeight: 600, color: t.textMuted, letterSpacing: '0.06em', textTransform: 'uppercase', borderBottom: `1px solid ${t.border}`, background: t.bgSubtle, gap: 12 }}>
          {columns.map((c, i) => <div key={i} style={{ textAlign: c.align || 'left', minWidth: 0 }}>{c.label}</div>)}
        </div>
        {rows.map((r, i) => (
          <div key={i} onClick={() => onRowClick?.(r, i)} style={{
            display:'grid', gridTemplateColumns: tracks, padding: '12px 18px', fontSize: 12.5, color: t.text, borderBottom: i < rows.length - 1 ? `1px solid ${t.border}` : 'none', cursor: onRowClick ? 'pointer' : 'default', alignItems: 'center', gap: 12,
          }} onMouseEnter={e => { if (onRowClick) e.currentTarget.style.background = t.bgSubtle; }} onMouseLeave={e => { e.currentTarget.style.background = 'transparent'; }}>
            {columns.map((c, j) => <div key={j} style={{ textAlign: c.align || 'left', minWidth: 0, overflow: 'hidden' }}>{c.render ? c.render(r) : r[c.key]}</div>)}
          </div>
        ))}
      </div>
    </div>
  );
}

// ── PageHeader ──────────────────────────────────────────────────────
function PageHeader({ kicker, title, sub, children }) {
  const { t } = window.useApp();
  return (
    <div style={{ display:'flex', alignItems:'flex-end', justifyContent:'space-between', margin: '0 0 22px', gap: 24 }}>
      <div>
        {kicker && <div style={{ fontSize: 10.5, fontWeight: 600, color: t.textMuted, letterSpacing: '0.08em', textTransform:'uppercase', marginBottom: 6 }}>{kicker}</div>}
        <div style={{ fontSize: 24, fontWeight: 600, color: t.text, letterSpacing: '-0.02em', marginBottom: sub ? 6 : 0 }}>{title}</div>
        {sub && <div style={{ fontSize: 13, color: t.textDim, maxWidth: 540, lineHeight: 1.5 }}>{sub}</div>}
      </div>
      {children && <div style={{ display:'flex', gap: 8, alignItems: 'center' }}>{children}</div>}
    </div>
  );
}

window.Card = Card;
window.Stat = Stat;
window.Pill = Pill;
window.PlatformChip = PlatformChip;
window.Btn = Btn;
window.DataTable = DataTable;
window.PageHeader = PageHeader;
