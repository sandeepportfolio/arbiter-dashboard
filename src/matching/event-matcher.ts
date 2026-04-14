/**
 * Cross-platform event matcher — STUB
 * Matches equivalent events across different prediction market platforms.
 *
 * Full implementation will come from ARB-130 (Market Scanner).
 * This stub provides the interface so the CLI pipeline can wire up.
 */

import type { PricePoint, MatchedEvent, MatchedContract, Platform } from "../types.js";

export class EventMatcher {
  /**
   * Match price points from different platforms that represent the same event.
   *
   * Stub: groups by platform only. Real implementation will use
   * fuzzy title matching, event metadata, and manual mapping tables.
   */
  match(
    ...platformPrices: PricePoint[][]
  ): MatchedEvent[] {
    // Flatten all price points
    const allPrices = platformPrices.flat();

    // Group by platform for now (stub — no cross-platform matching yet)
    const byPlatform = new Map<Platform, PricePoint[]>();
    for (const pp of allPrices) {
      const list = byPlatform.get(pp.platform) ?? [];
      list.push(pp);
      byPlatform.set(pp.platform, list);
    }

    // Create stub matched events — one per price point, no actual cross-matching
    const events: MatchedEvent[] = [];
    for (const pp of allPrices) {
      const prices = new Map<Platform, PricePoint>();
      prices.set(pp.platform, pp);

      const contract: MatchedContract = {
        contractKey: pp.contractId,
        title: pp.contractTitle,
        prices,
      };

      events.push({
        eventKey: pp.eventId,
        title: pp.eventTitle,
        contracts: [contract],
      });
    }

    return events;
  }

  /** Returns how many events had cross-platform matches */
  get crossPlatformMatchCount(): number {
    return 0; // Stub — always 0 until real matching is implemented
  }
}
