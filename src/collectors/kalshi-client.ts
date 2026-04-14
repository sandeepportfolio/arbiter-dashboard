/**
 * Kalshi API client — stub implementation.
 * Requires API key + RSA private key for authentication.
 * When credentials are not available, returns empty data gracefully.
 */

import type { PricePoint } from "../types.js";

export interface KalshiConfig {
  apiKey?: string;
  privateKeyPath?: string;
  dryRun: boolean;
}

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

    // TODO: Implement real Kalshi API calls with RSA auth
    // For now, return empty — will be implemented when credentials are configured
    console.log("[Kalshi] API client initialized (credentials found, but full API integration pending)");
    return [];
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
