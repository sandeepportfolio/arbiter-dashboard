// SVG chart primitives — area chart, sparkline, bar, donut.
// Pure React components, no external libs. All accept a `theme` prop with
// stroke/fill/grid colors so they can be reskinned per aesthetic.

const { useMemo, useState, useRef, useEffect } = React;

function buildPath(points, width, height, pad = 4) {
  if (!points.length) return { area: '', line: '', dots: [] };
  const xs = points.map(p => p.x);
  const ys = points.map(p => p.y);
  const xMin = Math.min(...xs), xMax = Math.max(...xs);
  const yMin = Math.min(...ys), yMax = Math.max(...ys);
  const xRange = xMax - xMin || 1;
  const yRange = yMax - yMin || 1;
  const W = width - pad * 2;
  const H = height - pad * 2;
  const mapped = points.map(p => ({
    x: pad + ((p.x - xMin) / xRange) * W,
    y: pad + H - ((p.y - yMin) / yRange) * H,
  }));
  const line = mapped.map((p, i) => (i === 0 ? `M${p.x},${p.y}` : `L${p.x},${p.y}`)).join(' ');
  const area = `${line} L${mapped[mapped.length - 1].x},${pad + H} L${mapped[0].x},${pad + H} Z`;
  return { area, line, dots: mapped, yMin, yMax };
}

// ── AreaChart ─────────────────────────────────────────────────────────
function AreaChart({ data, width = 600, height = 200, stroke = '#22C55E', fill = 'rgba(34,197,94,0.15)', grid = '#1E2230', showAxis = true, showGrid = true, currency = true, accentDot = true }) {
  const [hover, setHover] = useState(null);
  const ref = useRef(null);
  const points = data.map((d, i) => ({ x: i, y: d.v, t: d.t }));
  const { area, line, dots, yMin, yMax } = buildPath(points, width, height, 8);

  const onMove = e => {
    if (!ref.current) return;
    const rect = ref.current.getBoundingClientRect();
    const x = ((e.clientX - rect.left) / rect.width) * width;
    let nearest = 0, dmin = Infinity;
    for (let i = 0; i < dots.length; i++) {
      const d = Math.abs(dots[i].x - x);
      if (d < dmin) { dmin = d; nearest = i; }
    }
    setHover(nearest);
  };

  const fmt = v => currency ? '$' + v.toFixed(2) : v.toFixed(2);
  const ticks = showGrid ? [0.25, 0.5, 0.75].map(p => 8 + (height - 16) * p) : [];

  return (
    <svg ref={ref} viewBox={`0 0 ${width} ${height}`} preserveAspectRatio="none" style={{ width: '100%', height: '100%', display: 'block' }} onMouseMove={onMove} onMouseLeave={() => setHover(null)}>
      {ticks.map((y, i) => (
        <line key={i} x1="0" x2={width} y1={y} y2={y} stroke={grid} strokeDasharray="2 4" />
      ))}
      <defs>
        <linearGradient id="area-grad" x1="0" x2="0" y1="0" y2="1">
          <stop offset="0%" stopColor={stroke} stopOpacity="0.32" />
          <stop offset="100%" stopColor={stroke} stopOpacity="0" />
        </linearGradient>
      </defs>
      <path d={area} fill="url(#area-grad)" />
      <path d={line} fill="none" stroke={stroke} strokeWidth="1.5" strokeLinejoin="round" strokeLinecap="round" />
      {accentDot && dots.length > 0 && (
        <circle cx={dots[dots.length - 1].x} cy={dots[dots.length - 1].y} r="3" fill={stroke} />
      )}
      {hover != null && dots[hover] && (
        <g>
          <line x1={dots[hover].x} x2={dots[hover].x} y1="0" y2={height} stroke={stroke} strokeOpacity="0.4" strokeDasharray="2 2" />
          <circle cx={dots[hover].x} cy={dots[hover].y} r="3.5" fill={stroke} stroke="#000" strokeWidth="1" />
        </g>
      )}
      {showAxis && (
        <>
          <text x="4" y="14" fontSize="9" fill="#666" fontFamily="ui-monospace,monospace">{fmt(yMax)}</text>
          <text x="4" y={height - 4} fontSize="9" fill="#666" fontFamily="ui-monospace,monospace">{fmt(yMin)}</text>
        </>
      )}
    </svg>
  );
}

// ── Sparkline ────────────────────────────────────────────────────────
function Sparkline({ data, width = 120, height = 32, stroke = '#22C55E', fill = 'rgba(34,197,94,0.12)' }) {
  const points = data.map((v, i) => ({ x: i, y: typeof v === 'object' ? v.v : v }));
  const { area, line } = buildPath(points, width, height, 2);
  return (
    <svg viewBox={`0 0 ${width} ${height}`} preserveAspectRatio="none" style={{ width: '100%', height: '100%', display: 'block' }}>
      <path d={area} fill={fill} />
      <path d={line} fill="none" stroke={stroke} strokeWidth="1.5" strokeLinejoin="round" />
    </svg>
  );
}

// ── BarChart ─────────────────────────────────────────────────────────
function BarChart({ data, width = 360, height = 160, color = '#818CF8', labelColor = '#8B90A0', valueLabel = true, accent }) {
  const max = Math.max(...data.map(d => d.count));
  const padX = 12;
  const padY = 10;
  // Reserve space for the longest range label (chars * ~6.2px at fontSize 10 mono) and value label
  const labelW = Math.max(...data.map(d => String(d.range).length)) * 6.4 + 8;
  const valueW = valueLabel ? (String(max).length * 6.4 + 8) : 0;
  const trackX = padX + labelW;
  const trackW = width - trackX - padX - valueW;
  const rowH = (height - padY * 2) / data.length;
  const barH = Math.max(8, rowH - 6);
  return (
    <svg viewBox={`0 0 ${width} ${height}`} preserveAspectRatio="none" style={{ width: '100%', height: '100%', display: 'block' }}>
      {data.map((d, i) => {
        const w = (d.count / max) * trackW;
        const y = padY + i * rowH + (rowH - barH) / 2;
        const cy = y + barH / 2 + 3.5;
        const isAccent = accent && d.count >= max * 0.55;
        return (
          <g key={i}>
            <text x={trackX - 6} y={cy} textAnchor="end" fontSize="10" fill={labelColor} fontFamily="ui-monospace,monospace">{d.range}</text>
            <rect x={trackX} y={y} width={Math.max(2, w)} height={barH} fill={isAccent ? (accent || color) : color} rx="2" />
            {valueLabel && <text x={trackX + Math.max(2, w) + 6} y={cy} fontSize="10" fill={labelColor} fontFamily="ui-monospace,monospace">{d.count}</text>}
          </g>
        );
      })}
    </svg>
  );
}

// ── Donut ────────────────────────────────────────────────────────────
function Donut({ slices, size = 140, thickness = 18, label, sublabel, labelColor = '#E8EAF0', dimColor = '#8B90A0' }) {
  const total = slices.reduce((a, s) => a + s.value, 0);
  const r = (size - thickness) / 2;
  const c = size / 2;
  const circ = 2 * Math.PI * r;
  let acc = 0;
  return (
    <svg viewBox={`0 0 ${size} ${size}`} style={{ width: '100%', height: '100%' }}>
      <circle cx={c} cy={c} r={r} fill="none" stroke="rgba(255,255,255,0.05)" strokeWidth={thickness} />
      {slices.map((s, i) => {
        const len = (s.value / total) * circ;
        const dash = `${len} ${circ - len}`;
        const offset = -acc;
        acc += len;
        return (
          <circle key={i} cx={c} cy={c} r={r} fill="none" stroke={s.color} strokeWidth={thickness} strokeDasharray={dash} strokeDashoffset={offset} transform={`rotate(-90 ${c} ${c})`} strokeLinecap="butt" />
        );
      })}
      {label && <text x={c} y={c - 2} textAnchor="middle" fontSize="20" fontWeight="700" fill={labelColor} fontFamily="ui-monospace,monospace">{label}</text>}
      {sublabel && <text x={c} y={c + 14} textAnchor="middle" fontSize="9" fill={dimColor} letterSpacing="1">{sublabel}</text>}
    </svg>
  );
}

// ── EdgeStrip — horizontal heatmap of recent edges ───────────────────
function EdgeStrip({ history, width = 600, height = 48, scale = ['#1A1D28', '#312E81', '#3B82F6', '#22C55E'] }) {
  const max = Math.max(...history.map(h => h.best_edge_cents));
  const cell = width / history.length;
  const colorFor = v => {
    const t = Math.min(1, v / 5);
    if (t < 0.25) return scale[0];
    if (t < 0.5) return scale[1];
    if (t < 0.75) return scale[2];
    return scale[3];
  };
  return (
    <svg viewBox={`0 0 ${width} ${height}`} style={{ width: '100%', height: '100%', display: 'block' }}>
      {history.map((h, i) => (
        <rect key={i} x={i * cell} y={0} width={cell - 0.5} height={height} fill={colorFor(h.best_edge_cents)} />
      ))}
    </svg>
  );
}

window.AreaChart = AreaChart;
window.Sparkline = Sparkline;
window.BarChart = BarChart;
window.Donut = Donut;
window.EdgeStrip = EdgeStrip;
