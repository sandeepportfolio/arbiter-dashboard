/** Core types for the arbitrage pipeline */

export type Platform = "kalshi" | "predictit" | "polymarket";

export interface PricePoint {
  platform: Platform;
  eventId: string;
  eventTitle: string;
  contractId: string;
  contractTitle: string;
  yesPrice: number; // 0-1
  noPrice: number; // 0-1
  volume: number;
  lastUpdated: Date;
}

export interface MatchedEvent {
  eventKey: string; // normalized event identifier
  title: string;
  contracts: MatchedContract[];
}

export interface MatchedContract {
  contractKey: string;
  title: string;
  prices: Map<Platform, PricePoint>;
}

export interface ArbitrageOpportunity {
  id: string;
  matchedEvent: MatchedEvent;
  contractKey: string;
  buyPlatform: Platform;
  sellPlatform: Platform;
  buyPrice: number;
  sellPrice: number;
  spread: number; // sellPrice - buyPrice
  expectedProfit: number; // after fees
  confidence: number; // 0-1
  detectedAt: Date;
}

export interface TradeResult {
  opportunityId: string;
  buyPlatform: Platform;
  sellPlatform: Platform;
  buyPrice: number;
  sellPrice: number;
  quantity: number;
  grossProfit: number;
  fees: number;
  netProfit: number;
  dryRun: boolean;
  executedAt: Date;
  status: "executed" | "skipped" | "failed";
  reason?: string;
}

export interface RiskCheckResult {
  approved: boolean;
  reason: string;
  adjustedQuantity?: number;
}

export interface PipelineConfig {
  kalshiApiKey?: string;
  kalshiPrivateKeyPath?: string;
  dryRun: boolean;
  maxPositionSize: number;
  minSpread: number; // minimum spread to consider (e.g., 0.02 = 2%)
  maxExposure: number; // max total exposure in dollars
}
