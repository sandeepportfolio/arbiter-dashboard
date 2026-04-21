import { describe, it, expect, vi } from "vitest";
import {
  KALSHI_TS_CLIENT_BLOCKER,
  KalshiClient,
  KalshiCollector,
} from "../collectors/kalshi-client.js";

describe("KalshiClient", () => {
  it("reports unavailable when credentials are missing", () => {
    const client = new KalshiClient({ dryRun: true });
    expect(client.isAvailable()).toBe(false);
  });

  it("returns an empty market list when credentials are missing", async () => {
    const client = new KalshiClient({ dryRun: true });
    await expect(client.fetchMarkets()).resolves.toEqual([]);
  });

  it("fails fast with a clear blocker when credentials are present", async () => {
    const client = new KalshiClient({
      apiKey: "key",
      privateKeyPath: "/tmp/kalshi.pem",
      dryRun: true,
    });

    expect(client.isAvailable()).toBe(true);
    await expect(client.fetchMarkets()).rejects.toThrow(KALSHI_TS_CLIENT_BLOCKER);
  });
});

describe("KalshiCollector", () => {
  it("returns no prices and logs a skip when credentials are missing", async () => {
    const logSpy = vi.spyOn(console, "log").mockImplementation(() => {});
    const collector = new KalshiCollector(new KalshiClient({ dryRun: true }));

    await expect(collector.collect()).resolves.toEqual([]);
    expect(logSpy).toHaveBeenCalledWith(
      "[Kalshi] Skipping — no API credentials configured (set KALSHI_API_KEY and KALSHI_PRIVATE_KEY_PATH)"
    );

    logSpy.mockRestore();
  });

  it("maps fetched markets into PricePoint records", async () => {
    const collector = new KalshiCollector({
      isAvailable: () => true,
      fetchMarkets: async () => [
        {
          ticker: "BTC-2026",
          title: "Will BTC be above 100k on 2026-12-31?",
          yesPrice: 0.61,
          noPrice: 0.39,
          volume: 12345,
        },
      ],
    } as unknown as KalshiClient);

    const prices = await collector.collect();

    expect(prices).toHaveLength(1);
    expect(prices[0]).toMatchObject({
      platform: "kalshi",
      eventId: "kalshi-BTC-2026",
      contractId: "kalshi-BTC-2026",
      eventTitle: "Will BTC be above 100k on 2026-12-31?",
      contractTitle: "Will BTC be above 100k on 2026-12-31?",
      yesPrice: 0.61,
      noPrice: 0.39,
      volume: 12345,
    });
    expect(prices[0].lastUpdated).toBeInstanceOf(Date);
  });
});
