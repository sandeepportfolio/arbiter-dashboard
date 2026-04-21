/**
 * Kalshi collector adapter for the TypeScript demo pipeline.
 *
 * Current repo truth:
 * - Live Kalshi auth and trading checks exist in the Python stack under arbiter/.
 * - This TypeScript client does not sign or issue real Kalshi requests yet.
 * - If credentials are present, fail fast so operators do not mistake an empty
 *   response for a healthy live connection.
 */

import type { PricePoint } from "../types.js";

export interface KalshiConfig {
  apiKey?: string;
  privateKeyPath?: string;
  dryRun: boolean;
}

export const KALSHI_TS_CLIENT_BLOCKER =
  "Kalshi TypeScript collector is not implemented. The live path uses the Python stack; add signed REST calls here before relying on src/cli.ts for Kalshi market data.";

export class KalshiClient {
  private readonly config: KalshiConfig;
  private readonly available: boolean;

  constructor(config: KalshiConfig) {
    this.config = config;
    this.available = !!(config.apiKey && config.privateKeyPath);
  }

  isAvailable(): boolean {
    return this.available;
  }

  async fetchMarkets(): Promise<KalshiMarket[]> {
    if (!this.available) {
      return [];
    }

    throw new Error(KALSHI_TS_CLIENT_BLOCKER);
  }
}

interface KalshiMarket {
  ticker: string;
  title: string;
  yesPrice: number;
  noPrice: number;
  volume: number;
}

export class KalshiCollector {
  constructor(private client: KalshiClient) {}

  async collect(): Promise<PricePoint[]> {
    if (!this.client.isAvailable()) {
      console.log("[Kalshi] Skipping — no API credentials configured (set KALSHI_API_KEY and KALSHI_PRIVATE_KEY_PATH)");
      return [];
    }

    const markets = await this.client.fetchMarkets();
    return markets.map((m) => ({
      platform: "kalshi" as const,
      eventId: `kalshi-${m.ticker}`,
      eventTitle: m.title,
      contractId: `kalshi-${m.ticker}`,
      contractTitle: m.title,
      yesPrice: m.yesPrice,
      noPrice: m.noPrice,
      volume: m.volume,
      lastUpdated: new Date(),
    }));
  }
}
