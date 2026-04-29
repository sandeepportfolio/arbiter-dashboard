// Tweaks panel — accent, density, mock scenario
const { useState: tweakUseState } = React;

function TweaksHook() {
  const { t, setName, name } = window.useApp();
  const [accent, setAccent] = tweakUseState(t.accent);
  const [scenario, setScenario] = tweakUseState('busy');

  React.useEffect(() => {
    const onMsg = (e) => {
      if (e.data?.type === '__activate_edit_mode') setOpen(true);
      if (e.data?.type === '__deactivate_edit_mode') setOpen(false);
    };
    window.addEventListener('message', onMsg);
    window.parent.postMessage({ type: '__edit_mode_available' }, '*');
    return () => window.removeEventListener('message', onMsg);
  }, []);

  const [open, setOpen] = tweakUseState(false);
  if (!open) return null;

  const accents = [
    ['#5B5BD6', 'Indigo'], ['#0EA5E9', 'Sky'], ['#16A34A', 'Emerald'], ['#E11D48', 'Rose'], ['#D97706', 'Amber'], ['#7C3AED', 'Violet'],
  ];

  const setAccentLive = (hex) => {
    setAccent(hex);
    window.THEMES.light.accent = hex;
    window.THEMES.dark.accent = hex;
    window.THEMES.light.accentSoft = hex + '18';
    window.THEMES.dark.accentSoft = hex + '28';
    setName(name);
  };

  return (
    <div style={{ position:'fixed', bottom: 20, right: 20, width: 280, background: t.bgCard, border: `1px solid ${t.border}`, borderRadius: 12, boxShadow: t.shadowLg, zIndex: 200, overflow:'hidden' }}>
      <div style={{ padding: '12px 16px', borderBottom: `1px solid ${t.border}`, display:'flex', alignItems:'center', justifyContent:'space-between' }}>
        <div style={{ fontSize: 12, fontWeight: 600, color: t.text }}>Tweaks</div>
        <button onClick={() => { setOpen(false); window.parent.postMessage({ type: '__edit_mode_dismissed' }, '*'); }} style={{ background:'none', border:'none', color: t.textMuted, fontSize: 16, cursor:'pointer' }}>✕</button>
      </div>
      <div style={{ padding: 14 }}>
        <div style={{ fontSize: 10.5, color: t.textMuted, letterSpacing:'0.06em', textTransform:'uppercase', marginBottom: 8 }}>Accent</div>
        <div style={{ display:'flex', gap: 6, marginBottom: 16 }}>
          {accents.map(([hex, n]) => (
            <button key={hex} onClick={() => setAccentLive(hex)} title={n} style={{ width: 28, height: 28, borderRadius: 7, background: hex, border: accent === hex ? `2px solid ${t.text}` : `1px solid ${t.border}`, cursor:'pointer' }}/>
          ))}
        </div>

        <div style={{ fontSize: 10.5, color: t.textMuted, letterSpacing:'0.06em', textTransform:'uppercase', marginBottom: 8 }}>Theme</div>
        <div style={{ display:'flex', gap: 6, marginBottom: 16 }}>
          {['light','dark'].map(m => (
            <button key={m} onClick={() => setName(m)} style={{ flex: 1, padding: '6px 10px', background: name === m ? t.bgSubtle : t.bgCard, border: `1px solid ${name === m ? t.text : t.border}`, borderRadius: 6, fontSize: 11.5, color: t.text, fontWeight: 500, cursor:'pointer', textTransform:'capitalize' }}>{m}</button>
          ))}
        </div>

        <div style={{ fontSize: 10.5, color: t.textMuted, letterSpacing:'0.06em', textTransform:'uppercase', marginBottom: 8 }}>Mock scenario</div>
        <div style={{ display:'flex', gap: 6, flexWrap:'wrap' }}>
          {['busy','quiet','error'].map(s => (
            <button key={s} onClick={() => setScenario(s)} style={{ flex: 1, padding: '6px 10px', background: scenario === s ? t.bgSubtle : t.bgCard, border: `1px solid ${scenario === s ? t.text : t.border}`, borderRadius: 6, fontSize: 11.5, color: t.text, fontWeight: 500, cursor:'pointer', textTransform:'capitalize' }}>{s}</button>
          ))}
        </div>
      </div>
    </div>
  );
}

window.TweaksHook = TweaksHook;
