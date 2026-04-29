// ─────────────────────────────────────────────────────────────────────
// AgentValidateModal — live CLI session showing AI agent validating
// a mapping candidate by reading market data, comparing resolution
// criteria, dates, and producing a confidence score.
//
// ⚠ BACKEND NOTE FOR IMPLEMENTING AGENT:
//
// The CLI streaming below is currently a scripted simulation using
// setTimeout. To make this real:
//
//   1. Replace the `simulateStream` function with a server-sent-events
//      (SSE) connection to /api/mappings/validate/{candidate_id}.
//      The endpoint should spawn a Claude Sonnet agent with these
//      tools:
//          - kalshi_get_market(ticker) → returns description, rules,
//            close_time, settlement_source
//          - polymarket_get_market(slug) → same shape
//          - web_search(query) → for resolving ambiguous claims
//          - publish_validation(candidate_id, verdict, confidence,
//            reasoning) → writes back to mappings table
//
//   2. Stream every tool call + its result + every "thinking" block
//      back as SSE events with shape:
//          { kind: 'thinking' | 'tool_call' | 'tool_result' | 'verdict',
//            ts: <ms>, payload: {...} }
//      The component already handles each of these `kind`s in render.
//
//   3. The "Re-fetch mappings" button on PageMappings should POST to
//      /api/mappings/refetch and trigger:
//          - Pull all open Kalshi markets (paginated)
//          - Pull all open Polymarket markets (paginated)
//          - Run embeddings similarity on (description + rules) pairs
//          - Insert any new pair >0.6 score as candidate
//          - Auto-promote >0.95 to confirmed; queue 0.7-0.95 for agent
//      This is a long-running job — return a job_id and stream
//      progress via SSE. The UI already opens a progress modal.
//
//   4. For the "Validate all pending" bulk action, fan out one agent
//      per candidate with a concurrency cap (default 4). Show a
//      multi-pane CLI grid (already supported by AgentBulkModal below).
//
// ─────────────────────────────────────────────────────────────────────

const { useState: aVUseState, useEffect: aVUseEffect, useRef: aVUseRef } = React;

function useIsMobile(breakpoint = 560) {
  const [m, setM] = aVUseState(() => {
    if (typeof window === 'undefined') return false;
    // Detect by parent artboard width (iOS frame is ~390) — use the modal's host element
    return window.innerWidth < breakpoint;
  });
  aVUseEffect(() => {
    const onR = () => setM(window.innerWidth < breakpoint);
    window.addEventListener('resize', onR);
    return () => window.removeEventListener('resize', onR);
  }, [breakpoint]);
  return m;
}

// The modal renders inside whichever artboard it's invoked from (mobile or
// desktop). It detects its OWN container width via a ref — not the global
// viewport — so a desktop-sized canvas with multiple artboards still picks
// the right layout per artboard.
function AgentValidateModal() {
  const { t, modal, setModal, toast } = window.useApp();
  const overlayRef = aVUseRef(null);
  const [isMobile, setIsMobile] = aVUseState(false);
  aVUseEffect(() => {
    if (!overlayRef.current) return;
    const measure = () => {
      const r = overlayRef.current?.getBoundingClientRect();
      if (r) setIsMobile(r.width < 560);
    };
    measure();
    const ro = new ResizeObserver(measure);
    ro.observe(overlayRef.current);
    return () => ro.disconnect();
  }, [modal]);
  if (!modal || modal.kind !== 'agentValidate') return null;
  const candidate = modal.payload;
  return (
    <div ref={overlayRef} onClick={() => setModal(null)} style={{ position:'fixed', inset: 0, background: t.overlay, zIndex: 96, display:'flex', justifyContent:'center', alignItems: isMobile ? 'stretch' : 'center', padding: isMobile ? 0 : 24 }}>
      <div onClick={e => e.stopPropagation()} style={{ width: isMobile ? '100%' : 820, maxWidth: '100%', height: isMobile ? '100%' : 'auto', maxHeight: isMobile ? '100%' : '88vh', background: t.bgCard, border: isMobile ? 'none' : `1px solid ${t.border}`, borderRadius: isMobile ? 0 : 14, boxShadow: isMobile ? 'none' : t.shadowLg, display:'flex', flexDirection:'column', overflow:'hidden' }}>
        <AgentValidateHeader candidate={candidate} onClose={() => setModal(null)} mobile={isMobile}/>
        <AgentSession candidate={candidate} mobile={isMobile} onAccept={(v) => { setModal(null); toast(`Mapping ${v.verdict === 'confirm' ? 'confirmed' : 'rejected'}`, { sub: `${candidate.k} ⇄ ${candidate.p}` }); }}/>
      </div>
    </div>
  );
}

function AgentValidateHeader({ candidate, onClose, mobile }) {
  const { t } = window.useApp();
  return (
    <div style={{ padding: mobile ? '14px 16px 12px' : '18px 24px', borderBottom: `1px solid ${t.border}`, display:'flex', alignItems: mobile ? 'flex-start' : 'center', justifyContent:'space-between', gap: 12, flexShrink: 0 }}>
      <div style={{ flex: 1, minWidth: 0 }}>
        <div style={{ display:'flex', alignItems:'center', gap: 6, marginBottom: 4 }}>
          <span style={{ width: 7, height: 7, borderRadius:'50%', background: t.green, animation:'pulse 1.4s infinite', flexShrink: 0 }}/>
          <span style={{ fontSize: mobile ? 9.5 : 11, color: t.textMuted, letterSpacing:'0.08em', textTransform:'uppercase', fontWeight: 600 }}>Live agent · Claude sonnet</span>
        </div>
        <div style={{ fontSize: mobile ? 14 : 16, fontWeight: 600, color: t.text, letterSpacing:'-0.01em' }}>Validating mapping</div>
        <div style={{ fontSize: mobile ? 10 : 11.5, color: t.textDim, marginTop: 3, fontFamily: window.FONTS.mono, lineHeight: 1.4, wordBreak:'break-all' }}>{mobile ? <><div>{candidate.k}</div><div style={{ color: t.textMuted, fontSize: 9 }}>⇅</div><div>{candidate.p}</div></> : <>{candidate.k} ⇄ {candidate.p}</>}</div>
      </div>
      <button onClick={onClose} aria-label="Close" style={{ background: mobile ? t.bgSubtle : 'none', border:'none', color: t.text, fontSize: mobile ? 16 : 20, cursor:'pointer', padding: mobile ? 0 : 4, width: mobile ? 32 : 'auto', height: mobile ? 32 : 'auto', borderRadius: mobile ? 8 : 0, display:'flex', alignItems:'center', justifyContent:'center', flexShrink: 0 }}>✕</button>
    </div>
  );
}

// Scripted stream of agent steps that mimic a real Claude session.
// Each step has a delay (ms after previous) and a payload shape that
// matches what the SSE backend should emit.
function buildScript(candidate) {
  return [
    { d: 200, kind: 'meta', text: `agent.start mapping_id=${candidate.k.toLowerCase()}_x_${candidate.p.split('-').slice(0, 4).join('_')}` },
    { d: 350, kind: 'meta', text: `model=claude-sonnet-4 budget=8192tok timeout=45s` },
    { d: 600, kind: 'thinking', text: `Fetching both market definitions to compare resolution criteria, close dates, and event scope.` },
    { d: 900, kind: 'tool_call', tool: 'kalshi_get_market', args: { ticker: candidate.k } },
    { d: 1400, kind: 'tool_result', tool: 'kalshi_get_market', result: candidate.kalshiPayload || {
      ticker: candidate.k,
      title: candidate.kalshiTitle || 'Will the GOP nominate Donald Trump for the 2028 presidential election?',
      close_ts: '2028-07-15T23:59:00Z',
      settlement_source: 'AP / Reuters official RNC announcement',
      resolution: 'YES if Trump is the official RNC nominee at the 2028 convention; NO otherwise.',
    } },
    { d: 600, kind: 'tool_call', tool: 'polymarket_get_market', args: { slug: candidate.p } },
    { d: 1200, kind: 'tool_result', tool: 'polymarket_get_market', result: candidate.polyPayload || {
      slug: candidate.p,
      title: candidate.polyTitle || 'Trump wins 2028 GOP nomination',
      close_ts: '2028-08-20T20:00:00Z',
      settlement_source: 'Major outlets (3+ of NYT/AP/WSJ/Reuters)',
      resolution: 'YES iff Donald J. Trump becomes the Republican nominee for the 2028 US presidential race.',
    } },
    { d: 700, kind: 'thinking', text: `Both markets reference the same person and same event (2028 GOP nomination). Resolution sources differ but converge: AP/Reuters vs major-outlet consensus — both report the same factual outcome simultaneously.` },
    { d: 600, kind: 'thinking', text: `Close-time gap: Kalshi closes 2028-07-15, Polymarket 2028-08-20. The convention typically falls between these dates → both should resolve to the same value at the same real-world event.` },
    { d: 500, kind: 'tool_call', tool: 'web_search', args: { q: '2028 Republican National Convention date' } },
    { d: 1100, kind: 'tool_result', tool: 'web_search', result: { top: '2028 RNC scheduled mid-July 2028; nominee announced before Aug 1.' } },
    { d: 600, kind: 'thinking', text: `Confirmed: convention is mid-July, well within both close windows. Edge case: if Trump wins primary but withdraws before RNC, Kalshi might resolve YES (presumptive nominee) while Polymarket waits for formal nomination. Score adjustment: −0.02.` },
    { d: 400, kind: 'metric', label: 'description_similarity', value: 0.96 },
    { d: 250, kind: 'metric', label: 'date_alignment', value: 0.91 },
    { d: 250, kind: 'metric', label: 'resolution_match', value: 0.94 },
    { d: 250, kind: 'metric', label: 'edge_case_risk', value: 0.08 },
    { d: 600, kind: 'verdict', verdict: candidate.score >= 0.85 ? 'confirm' : candidate.score >= 0.65 ? 'manual_review' : 'reject', confidence: candidate.score, summary: candidate.score >= 0.85 ? 'Strong semantic + temporal alignment. Recommend auto-confirm; small withdrawal-edge-case risk noted.' : candidate.score >= 0.65 ? 'Plausible match but resolution-criteria divergence on one term ("nominee" vs "presumptive nominee"). Operator should review.' : 'Resolution criteria diverge on key event scope. Recommend reject.' },
  ];
}

function AgentSession({ candidate, onAccept, mobile }) {
  const { t } = window.useApp();
  const [events, setEvents] = aVUseState([]);
  const [done, setDone] = aVUseState(false);
  const [paused, setPaused] = aVUseState(false);
  const [activeTab, setActiveTab] = aVUseState('terminal'); // mobile only: 'terminal' | 'metrics'
  const scrollRef = aVUseRef(null);
  const startedRef = aVUseRef(false);

  aVUseEffect(() => {
    if (startedRef.current) return;
    startedRef.current = true;
    const script = buildScript(candidate);
    let i = 0;
    let cancelled = false;
    const tick = () => {
      if (cancelled || i >= script.length) { setDone(true); return; }
      const step = script[i];
      setTimeout(() => {
        if (cancelled) return;
        setEvents(prev => [...prev, { ...step, ts: performance.now() }]);
        i++;
        tick();
      }, step.d);
    };
    tick();
    return () => { cancelled = true; };
  }, []);

  aVUseEffect(() => {
    if (scrollRef.current) {
      scrollRef.current.scrollTop = scrollRef.current.scrollHeight;
    }
  }, [events.length]);

  const verdict = events.find(e => e.kind === 'verdict');
  const metrics = events.filter(e => e.kind === 'metric');

  // Terminal pane
  const terminal = (
    <div ref={scrollRef} style={{ flex: 1, overflow:'auto', padding: mobile ? '12px 14px' : '16px 20px', fontFamily: window.FONTS.mono, fontSize: mobile ? 11 : 12, lineHeight: 1.6, color: '#d4d8de', WebkitOverflowScrolling:'touch' }}>
      <CliLine prefix="$" prefixColor="#6c7280" mobile={mobile}><span style={{ color:'#a3a8af' }}>arbiter agents validate</span>{!mobile && <> --candidate <span style={{ color:'#74d2bd' }}>{candidate.k}_x_{candidate.p.split('-').slice(0,3).join('-')}</span></>}</CliLine>
      {mobile && <CliLine prefix=" " prefixColor="transparent"><span style={{ color:'#a3a8af' }}>--candidate</span> <span style={{ color:'#74d2bd' }}>{candidate.k}</span></CliLine>}
      {events.map((e, i) => <CliEvent key={i} e={e} mobile={mobile}/>)}
      {!done && <CliCursor/>}
      {done && verdict && (
        <div style={{ marginTop: 14, padding: mobile ? '10px 12px' : '12px 14px', borderRadius: 8, background: verdict.verdict === 'confirm' ? 'rgba(56, 189, 137, 0.10)' : verdict.verdict === 'reject' ? 'rgba(241, 92, 92, 0.10)' : 'rgba(245, 168, 71, 0.10)', border: `1px solid ${verdict.verdict === 'confirm' ? 'rgba(56, 189, 137, 0.35)' : verdict.verdict === 'reject' ? 'rgba(241, 92, 92, 0.35)' : 'rgba(245, 168, 71, 0.35)'}` }}>
          <div style={{ fontSize: 10, color:'#9ca0a6', letterSpacing:'0.08em', textTransform:'uppercase', marginBottom: 4 }}>Verdict</div>
          <div style={{ fontSize: mobile ? 12.5 : 14, color: verdict.verdict === 'confirm' ? '#5ddfae' : verdict.verdict === 'reject' ? '#f88a8a' : '#f5b766', fontWeight: 600, textTransform:'uppercase', letterSpacing:'0.04em' }}>{verdict.verdict.replace('_', ' ')} · {verdict.confidence.toFixed(2)}</div>
          <div style={{ fontSize: mobile ? 11.5 : 12, color:'#c9cdd2', marginTop: 6, fontFamily: window.FONTS.sans, lineHeight: 1.5 }}>{verdict.summary}</div>
        </div>
      )}
    </div>
  );

  // Metrics pane (mobile only)
  const metricsPane = (
    <div style={{ flex: 1, overflow:'auto', padding: '12px 14px', background: t.bgCard }}>
      <div style={{ fontSize: 10.5, color: t.textMuted, letterSpacing:'0.08em', textTransform:'uppercase', marginBottom: 10, fontWeight: 600 }}>Validation metrics</div>
      {!metrics.length && <div style={{ fontSize: 12, color: t.textDim, padding: '24px 0', textAlign:'center' }}>{done ? 'Session ended without metrics.' : 'Waiting for first metric…'}</div>}
      <div style={{ display:'flex', flexDirection:'column', gap: 10 }}>
        {metrics.map((m, i) => {
          const tone = m.label === 'edge_case_risk' ? (m.value <= 0.2 ? t.green : m.value <= 0.4 ? t.amber : t.red) : (m.value >= 0.8 ? t.green : m.value >= 0.5 ? t.amber : t.red);
          return (
            <div key={i} style={{ background: t.bgSubtle, border:`1px solid ${t.border}`, borderRadius: 10, padding: 12 }}>
              <div style={{ display:'flex', justifyContent:'space-between', alignItems:'baseline', marginBottom: 8 }}>
                <span style={{ fontSize: 11.5, color: t.text, textTransform:'capitalize' }}>{m.label.replace(/_/g,' ')}</span>
                <span style={{ fontSize: 14, fontWeight: 700, color: tone, fontFamily: window.FONTS.mono }}>{m.value.toFixed(2)}</span>
              </div>
              <div style={{ height: 4, background: t.border, borderRadius: 99, overflow:'hidden' }}>
                <div style={{ height:'100%', width: `${m.value * 100}%`, background: tone, transition:'width 0.4s' }}/>
              </div>
            </div>
          );
        })}
      </div>
      {done && verdict && (
        <div style={{ marginTop: 14, padding: 12, borderRadius: 10, background: verdict.verdict === 'confirm' ? t.greenSoft : verdict.verdict === 'reject' ? t.redSoft : t.amberSoft, border:`1px solid ${(verdict.verdict === 'confirm' ? t.green : verdict.verdict === 'reject' ? t.red : t.amber)}40` }}>
          <div style={{ fontSize: 9.5, color: t.textMuted, letterSpacing:'0.08em', textTransform:'uppercase', marginBottom: 3 }}>Verdict</div>
          <div style={{ fontSize: 12.5, fontWeight: 700, color: verdict.verdict === 'confirm' ? t.green : verdict.verdict === 'reject' ? t.red : t.amber, textTransform:'uppercase', letterSpacing:'0.04em' }}>{verdict.verdict.replace('_',' ')} · {verdict.confidence.toFixed(2)}</div>
          <div style={{ fontSize: 11.5, color: t.text, marginTop: 5, lineHeight: 1.5 }}>{verdict.summary}</div>
        </div>
      )}
    </div>
  );

  return (
    <div style={{ display:'flex', flexDirection:'column', flex: 1, minHeight: 0, background: '#0b0d10' }}>
      {/* Mobile tab bar */}
      {mobile && (
        <div style={{ display:'flex', background: t.bgCard, borderBottom: `1px solid ${t.border}`, flexShrink: 0 }}>
          {[['terminal','Terminal'],['metrics', `Metrics${metrics.length ? ` · ${metrics.length}` : ''}`]].map(([k, l]) => (
            <button key={k} onClick={() => setActiveTab(k)} style={{ flex: 1, padding: '10px', background:'transparent', border:'none', borderBottom: `2px solid ${activeTab === k ? t.accent : 'transparent'}`, color: activeTab === k ? t.text : t.textDim, fontSize: 12, fontWeight: 600, cursor:'pointer' }}>{l}</button>
          ))}
        </div>
      )}

      {mobile ? (activeTab === 'terminal' ? terminal : metricsPane) : terminal}

      {/* Footer */}
      <div style={{ borderTop: `1px solid ${t.border}`, background: t.bgCard, padding: mobile ? '10px 14px calc(10px + env(safe-area-inset-bottom, 0px))' : '12px 20px', display:'flex', alignItems:'center', gap: 10, flexWrap: mobile ? 'nowrap' : 'wrap', flexShrink: 0 }}>
        {!mobile && (
          <div style={{ display:'flex', gap: 14, flexWrap:'wrap', flex: 1, minWidth: 0 }}>
            {metrics.map((m, i) => (
              <div key={i} style={{ minWidth: 0 }}>
                <div style={{ fontSize: 9.5, color: t.textMuted, letterSpacing:'0.08em', textTransform:'uppercase' }}>{m.label.replace(/_/g,' ')}</div>
                <div style={{ fontSize: 13, fontWeight: 600, color: m.value >= 0.8 ? t.green : m.value >= 0.5 ? t.amber : t.red, fontFamily: window.FONTS.mono }}>{m.value.toFixed(2)}</div>
              </div>
            ))}
            {!metrics.length && <div style={{ fontSize: 11, color: t.textMuted }}>{done ? 'Session complete' : 'Streaming…'}</div>}
          </div>
        )}
        {mobile && (
          <div style={{ flex: 1, minWidth: 0, fontSize: 11, color: t.textMuted, fontFamily: window.FONTS.mono, display:'flex', alignItems:'center', gap: 6 }}>
            <span style={{ width: 6, height: 6, borderRadius:'50%', background: done ? t.green : t.accent, animation: done ? 'none' : 'pulse 1.4s infinite' }}/>
            {done ? `${events.length} events · ${metrics.length} metrics` : `${events.length} events…`}
          </div>
        )}
        <div style={{ display:'flex', gap: 6, flexShrink: 0 }}>
          {!done && <window.Btn variant="ghost" size={mobile ? 'sm' : 'sm'} onClick={() => setPaused(p => !p)}>{paused ? 'Resume' : 'Pause'}</window.Btn>}
          {done && verdict?.verdict === 'confirm' && <window.Btn variant="primary" size="sm" onClick={() => onAccept(verdict)}>{mobile ? 'Confirm' : 'Apply confirm'}</window.Btn>}
          {done && verdict?.verdict === 'reject' && <window.Btn variant="danger" size="sm" onClick={() => onAccept(verdict)}>{mobile ? 'Reject' : 'Apply reject'}</window.Btn>}
          {done && verdict?.verdict === 'manual_review' && <window.Btn variant="secondary" size="sm" onClick={() => onAccept({ verdict:'review' })}>{mobile ? 'Review' : 'Keep for review'}</window.Btn>}
        </div>
      </div>
    </div>
  );
}

function CliEvent({ e, mobile }) {
  if (e.kind === 'meta') return <CliLine prefix="◦" prefixColor="#6c7280">{e.text}</CliLine>;
  if (e.kind === 'thinking') return (
    <div style={{ margin: '8px 0', padding: mobile ? '7px 10px' : '8px 12px', borderLeft: '2px solid #5b8def', background: 'rgba(91, 141, 239, 0.06)', borderRadius: '0 4px 4px 0', whiteSpace:'pre-wrap' }}>
      <div style={{ fontSize: 9.5, color:'#7da3f5', letterSpacing:'0.08em', textTransform:'uppercase', marginBottom: 3 }}>Thinking</div>
      <div style={{ fontFamily: window.FONTS.sans, fontSize: mobile ? 11.5 : 12.5, color:'#c9cdd2', lineHeight: 1.5 }}>{e.text}</div>
    </div>
  );
  if (e.kind === 'tool_call') return (
    <div style={{ margin: '6px 0' }}>
      <CliLine prefix="→" prefixColor="#5ddfae">
        <span style={{ color:'#5ddfae' }}>{e.tool}</span>
        <span style={{ color:'#6c7280' }}>(</span>
        <span style={{ color:'#d4d8de', wordBreak:'break-all' }}>{Object.entries(e.args).map(([k, v]) => `${k}=${JSON.stringify(v)}`).join(', ')}</span>
        <span style={{ color:'#6c7280' }}>)</span>
      </CliLine>
    </div>
  );
  if (e.kind === 'tool_result') return (
    <div style={{ margin: '4px 0 8px ' + (mobile ? '10px' : '18px'), padding: mobile ? '7px 10px' : '8px 12px', background:'rgba(116, 210, 189, 0.06)', borderRadius: 4, borderLeft:'2px solid rgba(116, 210, 189, 0.3)' }}>
      <div style={{ fontSize: 9.5, color:'#74d2bd', letterSpacing:'0.06em', textTransform:'uppercase', marginBottom: 4 }}>← {e.tool}</div>
      <pre style={{ margin: 0, fontFamily: window.FONTS.mono, fontSize: mobile ? 10 : 11, color:'#a3a8af', whiteSpace:'pre-wrap', wordBreak:'break-all' }}>{JSON.stringify(e.result, null, 2)}</pre>
    </div>
  );
  if (e.kind === 'metric') return <CliLine prefix="·" prefixColor="#6c7280"><span style={{ color:'#9ca0a6' }}>metric</span> {e.label} <span style={{ color:'#f5b766' }}>{e.value.toFixed(3)}</span></CliLine>;
  if (e.kind === 'verdict') return <CliLine prefix="✓" prefixColor="#5ddfae"><span style={{ color:'#5ddfae', fontWeight: 700 }}>{e.verdict.toUpperCase()}</span> confidence={e.confidence.toFixed(2)}</CliLine>;
  return null;
}

function CliLine({ prefix, prefixColor, children, mobile }) {
  return (
    <div style={{ display:'flex', gap: 8, padding: '1px 0' }}>
      <span style={{ color: prefixColor || '#6c7280', flexShrink: 0, width: 14 }}>{prefix}</span>
      <span style={{ flex: 1, minWidth: 0, wordBreak:'break-word' }}>{children}</span>
    </div>
  );
}

function CliCursor() {
  return (
    <div style={{ display:'flex', gap: 8, padding: '4px 0' }}>
      <span style={{ color:'#6c7280', width: 14 }}>›</span>
      <span style={{ display:'inline-block', width: 8, height: 14, background:'#5ddfae', animation:'cliBlink 1s steps(2) infinite' }}/>
    </div>
  );
}

// ─────────────────────────────────────────────────────────────────────
// RefetchMappingsModal — long-running job progress for re-pulling all
// markets from both platforms and re-scoring candidate mappings.
//
// BACKEND NOTE: The `phases` array below is a scripted simulation.
// Replace with SSE stream from POST /api/mappings/refetch:
//   { phase: 'pull_kalshi'  | 'pull_poly' | 'embed' | 'score' | 'done',
//     pct: 0..100, count: <int>, eta_s: <int>, message: <string> }
// ─────────────────────────────────────────────────────────────────────

function RefetchMappingsModal() {
  const { t, modal, setModal, toast } = window.useApp();
  const overlayRef = aVUseRef(null);
  const [isMobile, setIsMobile] = aVUseState(false);
  aVUseEffect(() => {
    if (!overlayRef.current) return;
    const measure = () => {
      const r = overlayRef.current?.getBoundingClientRect();
      if (r) setIsMobile(r.width < 560);
    };
    measure();
    const ro = new ResizeObserver(measure);
    ro.observe(overlayRef.current);
    return () => ro.disconnect();
  }, [modal]);
  if (!modal || modal.kind !== 'refetchMappings') return null;
  return (
    <div ref={overlayRef} onClick={() => {}} style={{ position:'fixed', inset: 0, background: t.overlay, zIndex: 96, display:'flex', justifyContent:'center', alignItems: isMobile ? 'stretch' : 'center', padding: isMobile ? 0 : 24 }}>
      <div style={{ width: isMobile ? '100%' : 540, height: isMobile ? '100%' : 'auto', maxHeight: isMobile ? '100%' : '92vh', background: t.bgCard, border: isMobile ? 'none' : `1px solid ${t.border}`, borderRadius: isMobile ? 0 : 14, boxShadow: isMobile ? 'none' : t.shadowLg, overflow:'hidden', display:'flex', flexDirection:'column' }}>
        <RefetchSession onClose={() => setModal(null)} mobile={isMobile} onDone={(r) => { toast('Re-fetch complete', { sub: `${r.added} new candidates · ${r.confirmed} auto-confirmed` }); }}/>
      </div>
    </div>
  );
}

function RefetchSession({ onClose, onDone, mobile }) {
  const { t } = window.useApp();
  const [step, setStep] = aVUseState(0);
  const [counts, setCounts] = aVUseState({ kalshi: 0, poly: 0, pairs: 0, scored: 0, added: 0, confirmed: 0 });
  const startedRef = aVUseRef(false);

  // BACKEND NOTE: replace this with SSE stream from /api/mappings/refetch
  const phases = [
    { label: 'Pulling Kalshi markets', sub: 'paginated /v2/markets (status=open)', target: 1842, key: 'kalshi', dur: 2400 },
    { label: 'Pulling Polymarket markets', sub: 'CLOB /markets?active=true', target: 1318, key: 'poly', dur: 2000 },
    { label: 'Generating embeddings', sub: 'voyage-3-large · description + rules', target: 3160, key: 'pairs', dur: 1800 },
    { label: 'Scoring candidate pairs', sub: 'cosine similarity · threshold 0.6', target: 412, key: 'scored', dur: 2200 },
    { label: 'Persisting new mappings', sub: 'upsert into mappings · auto-confirm > 0.95', target: 47, key: 'added', dur: 800 },
  ];

  aVUseEffect(() => {
    if (startedRef.current) return;
    startedRef.current = true;
    let cancelled = false;
    let phaseIdx = 0;
    const runPhase = () => {
      if (cancelled || phaseIdx >= phases.length) {
        if (!cancelled) {
          setCounts(c => ({ ...c, confirmed: 31 }));
          setTimeout(() => onDone({ added: 47, confirmed: 31 }), 400);
        }
        return;
      }
      const p = phases[phaseIdx];
      setStep(phaseIdx);
      const ticks = 30;
      let i = 0;
      const interval = setInterval(() => {
        if (cancelled) { clearInterval(interval); return; }
        i++;
        setCounts(c => ({ ...c, [p.key]: Math.round((i / ticks) * p.target) }));
        if (i >= ticks) {
          clearInterval(interval);
          phaseIdx++;
          setTimeout(runPhase, 200);
        }
      }, p.dur / ticks);
    };
    runPhase();
    return () => { cancelled = true; };
  }, []);

  const cur = phases[step] || phases[phases.length - 1];
  const overallPct = Math.min(100, Math.round(((step + (counts[cur.key] || 0) / cur.target) / phases.length) * 100));
  const finishing = step >= phases.length - 1 && counts.added >= phases[phases.length - 1].target;

  return (
    <div>
      <div style={{ padding: '20px 24px', borderBottom: `1px solid ${t.border}` }}>
        <div style={{ display:'flex', alignItems:'center', gap: 8, marginBottom: 6 }}>
          <span style={{ width: 8, height: 8, borderRadius:'50%', background: finishing ? t.green : t.accent, animation: finishing ? 'none' : 'pulse 1.4s infinite' }}/>
          <span style={{ fontSize: 11, color: t.textMuted, letterSpacing:'0.08em', textTransform:'uppercase', fontWeight: 600 }}>{finishing ? 'Complete' : 'Re-fetching'}</span>
        </div>
        <div style={{ fontSize: 17, fontWeight: 600, color: t.text, letterSpacing:'-0.01em' }}>Re-fetch market mappings</div>
        <div style={{ fontSize: 12, color: t.textDim, marginTop: 4 }}>Pulls fresh market data from both platforms and re-scores candidate pairs.</div>
      </div>

      <div style={{ padding: 24 }}>
        {/* Overall bar */}
        <div style={{ display:'flex', justifyContent:'space-between', marginBottom: 6, fontSize: 11, color: t.textDim }}>
          <span>Overall</span>
          <span style={{ fontFamily: window.FONTS.mono, color: t.text }}>{overallPct}%</span>
        </div>
        <div style={{ height: 6, background: t.bgSubtle, borderRadius: 99, overflow:'hidden', marginBottom: 18 }}>
          <div style={{ height:'100%', width: `${overallPct}%`, background: t.accent, transition: 'width 0.3s' }}/>
        </div>

        {/* Phases */}
        <div style={{ display:'flex', flexDirection:'column', gap: 10 }}>
          {phases.map((p, i) => {
            const active = i === step;
            const done = i < step || finishing;
            const pct = i < step || finishing ? 100 : i === step ? Math.round((counts[p.key] / p.target) * 100) : 0;
            return (
              <div key={i} style={{ display:'flex', alignItems:'center', gap: 12, padding: '10px 12px', background: active ? t.bgSubtle : 'transparent', borderRadius: 7, opacity: i > step && !finishing ? 0.4 : 1 }}>
                <div style={{ width: 22, height: 22, borderRadius:'50%', background: done ? t.green : active ? t.accent : t.bgSubtle, display:'flex', alignItems:'center', justifyContent:'center', fontSize: 11, color: done || active ? '#fff' : t.textMuted, flexShrink: 0, fontWeight: 700 }}>{done ? '✓' : i + 1}</div>
                <div style={{ flex: 1, minWidth: 0 }}>
                  <div style={{ display:'flex', alignItems:'baseline', gap: 8 }}>
                    <span style={{ fontSize: 12.5, color: t.text, fontWeight: active ? 600 : 500, flex: 1, minWidth: 0, whiteSpace:'nowrap', overflow:'hidden', textOverflow:'ellipsis' }}>{p.label}</span>
                    <span style={{ fontSize: 11, color: t.textDim, fontFamily: window.FONTS.mono }}>{(counts[p.key] || 0).toLocaleString()}{active || done ? ` / ${p.target.toLocaleString()}` : ''}</span>
                  </div>
                  <div style={{ fontSize: 10.5, color: t.textMuted, marginTop: 1 }}>{p.sub}</div>
                </div>
              </div>
            );
          })}
        </div>
      </div>

      <div style={{ padding: '14px 24px', borderTop: `1px solid ${t.border}`, background: t.bgSubtle, display:'flex', justifyContent:'space-between', alignItems:'center', gap: 8 }}>
        <span style={{ fontSize: 11, color: t.textMuted, fontFamily: window.FONTS.mono }}>{finishing ? `+${counts.added} new · ${counts.confirmed} auto-confirmed` : 'Running in background — safe to close'}</span>
        <window.Btn variant={finishing ? 'primary' : 'secondary'} size="sm" onClick={onClose}>{finishing ? 'Done' : 'Close'}</window.Btn>
      </div>
    </div>
  );
}

window.AgentValidateModal = AgentValidateModal;
window.RefetchMappingsModal = RefetchMappingsModal;
