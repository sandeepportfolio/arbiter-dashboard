/**
 * PredictIt API client — fetches live market data.
 * No authentication required.
 */

import type { PricePoint } from "../types.js";

interface PredictItMarket {
  id: number;
  name: string;
  shortName: string;
  url: string;
  contracts: PredictItContract[];
  status: string;
}

interface PredictItContract {
  id: number;
  name: string;
  shortName: string;
  bestBuyYesCost: number | null;
  bestBuyNoCost: number | null;
  bestSellYesCost: number | null;
  bestSellNoCost: number | null;
  lastTradePrice: number | null;
  lastClosePrice: number | null;
  status: string;
}

interface PredictItApiResponse {
  markets: PredictItMarket[];
}

export class PredictItClient {
  private readonly baseUrl = "https://www.predictit.org/api/marketdata";

  async fetchAllMarkets(): Promise<PredictItMarket[]> {
    const response = await fetch(`${this.baseUrl}/all/`);
    if (!response.ok) {
      throw new Error(`PredictIt API error: ${response.status} ${response.statusText}`);
    }
    const data = (await response.json()) as PredictItApiResponse;
    return data.markets;
  }
}

export class PredictItCollector {
  constructor(private client: PredictItClient) {}

  async collect(): Promise<PricePoint[]> {
    const markets = await this.client.fetchAllMarkets();
    const pricePoints: PricePoint[] = [];

    for (const market of markets) {
      if (market.status !== "Open") continue;

      for (const contract of market.contracts) {
        if (contract.status !== "Open") continue;

        const yesPrice = contract.bestBuyYesCost ?? contract.lastTradePrice ?? 0;
        const noPrice = contract.bestBuyNoCost ?? (1 - yesPrice);

        if (yesPrice <= 0) continue;

        pricePoints.push({
          platform: "predictit",
          eventId: `predictit-${market.id}`,
          eventTitle: market.name,
          contractId: `predictit-contract-${contract.id}`,
          contractTitle: contract.name,
          yesPrice,
          noPrice,
          volume: 0, // PredictIt doesn't expose volume in this API
          lastUpdated: new Date(),
        });
      }
    }

    return pricePoints;
  }
}
