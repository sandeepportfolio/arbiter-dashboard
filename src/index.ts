/** Barrel export for the arbiter pipeline. */

// Types
export type {
  Platform,
  PricePoint,
  MatchedEvent,
  MatchedContract,
  ArbitrageOpportunity,
  TradeResult,
  ArbitrageTradeResult,
  RiskCheckResult,
  PipelineConfig,
} from "./types.js";

// Execution
export { ArbitrageExecutor } from "./execution/arbitrage-executor.js";
export { TradeLogger } from "./execution/trade-logger.js";
export type { ArbitrageLogEntry } from "./execution/trade-logger.js";
export { RiskGate } from "./execution/risk-gate.js";

// Matching
export { EventMatcher } from "./matching/event-matcher.js";
export { ArbitrageDetector } from "./matching/arbitrage-detector.js";

// Collectors
export { PredictItClient, PredictItCollector } from "./collectors/predictit-client.js";
export { KalshiClient, KalshiCollector } from "./collectors/kalshi-client.js";
