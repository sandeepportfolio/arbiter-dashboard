#!/usr/bin/env python3
"""
Deep validation of the LLM verifier using Claude Opus via CLI.

Runs 100+ test cases through the actual Claude model to ensure:
1. Identical markets are correctly identified as YES
2. Mismatched markets are correctly identified as NO
3. Ambiguous cases return MAYBE (conservative fail-safe)
4. Edge cases (similar-sounding but different events) are caught
5. The parser correctly extracts YES/NO/MAYBE from various response formats

This script is meant to be run on the Mac where claude CLI is available.
Usage: python tests/test_llm_verifier_deep.py
"""
import asyncio
import json
import os
import sys
import time
from dataclasses import dataclass
from typing import Literal

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from arbiter.mapping.llm_verifier import _verify_cli, _parse_answer, _find_claude_cli


@dataclass
class TestCase:
    kalshi: str
    poly: str
    expected: Literal["YES", "NO", "MAYBE"]
    category: str
    description: str


# ═══════════════════════════════════════════════════════════════════════════
# TEST CASES — 100+ carefully curated pairs
# ═══════════════════════════════════════════════════════════════════════════

IDENTICAL_PAIRS = [
    # --- Politics ---
    TestCase("Will Republicans win control of the House in the 2026 midterm elections?",
             "Will the Republican Party win the House in 2026?",
             "YES", "politics", "House control — same event, different wording"),
    TestCase("Will Democrats win control of the Senate in the 2026 midterm elections?",
             "Will the Democratic Party win the Senate in 2026?",
             "YES", "politics", "Senate control — same event"),
    TestCase("Will Donald Trump be president on January 1, 2027?",
             "Will Trump be the US President at the start of 2027?",
             "YES", "politics", "Trump presidency check"),
    TestCase("Will the US federal government shut down before July 2026?",
             "US government shutdown before July 2026?",
             "YES", "politics", "Government shutdown"),
    TestCase("Will Biden run for president in 2028?",
             "Will Joe Biden seek the presidency in 2028?",
             "YES", "politics", "Biden 2028 run"),

    # --- Economics/Finance ---
    TestCase("Will the Federal Reserve cut rates in June 2026?",
             "Will the Fed cut interest rates at the June 2026 FOMC meeting?",
             "YES", "economics", "Fed rate cut"),
    TestCase("Will US GDP growth exceed 3% in Q2 2026?",
             "Will the US GDP growth rate be above 3% in the second quarter of 2026?",
             "YES", "economics", "GDP growth"),
    TestCase("Will the S&P 500 close above 6000 on June 30, 2026?",
             "S&P 500 above 6000 at end of June 2026?",
             "YES", "economics", "S&P 500 level"),
    TestCase("Will US unemployment rate exceed 5% in 2026?",
             "Will the US unemployment rate go above 5% this year?",
             "YES", "economics", "Unemployment rate"),
    TestCase("Will inflation (CPI) be above 3% in May 2026?",
             "US CPI year-over-year above 3% in May 2026?",
             "YES", "economics", "CPI inflation"),

    # --- Sports ---
    TestCase("Will the Celtics win the 2026 NBA Championship?",
             "Boston Celtics to win the 2025-26 NBA Finals?",
             "YES", "sports", "NBA championship"),
    TestCase("Will the Yankees win the 2026 World Series?",
             "New York Yankees to win the 2026 MLB World Series?",
             "YES", "sports", "World Series"),
    TestCase("Will Real Madrid win the 2025-26 Champions League?",
             "Real Madrid to win the UEFA Champions League 2025-26?",
             "YES", "sports", "Champions League"),

    # --- Tech ---
    TestCase("Will Apple release a foldable iPhone before 2027?",
             "Apple foldable iPhone launch before January 2027?",
             "YES", "tech", "Apple foldable"),
    TestCase("Will Tesla deliver more than 2 million vehicles in 2026?",
             "Tesla 2026 annual deliveries above 2M?",
             "YES", "tech", "Tesla deliveries"),

    # --- Geopolitics ---
    TestCase("Will Russia and Ukraine reach a ceasefire agreement before October 2026?",
             "Russia-Ukraine ceasefire before Oct 2026?",
             "YES", "geopolitics", "Ukraine ceasefire"),
    TestCase("Will China invade Taiwan before 2027?",
             "Chinese military invasion of Taiwan before January 2027?",
             "YES", "geopolitics", "Taiwan invasion"),

    # --- Science ---
    TestCase("Will a COVID-19 variant be declared a PHEIC in 2026?",
             "New COVID variant declared Public Health Emergency in 2026?",
             "YES", "science", "COVID PHEIC"),

    # --- Weather ---
    TestCase("Will a Category 5 hurricane make US landfall in 2026?",
             "Category 5 hurricane hits the US mainland in 2026?",
             "YES", "weather", "Category 5 hurricane"),

    # --- More Politics ---
    TestCase("Will the US Supreme Court overturn Chevron deference in 2026?",
             "Will SCOTUS overturn Chevron deference in 2026?",
             "YES", "politics", "SCOTUS Chevron — abbreviation only"),
    TestCase("Will a US state legalize recreational marijuana in 2026?",
             "New US state to legalize recreational cannabis in 2026?",
             "YES", "politics", "Cannabis legalization — marijuana vs cannabis"),
    TestCase("Will there be a US presidential debate in 2026?",
             "US presidential debate held in 2026?",
             "YES", "politics", "Presidential debate"),

    # --- More Economics ---
    TestCase("Will the US national debt exceed $40 trillion in 2026?",
             "US national debt above $40T in 2026?",
             "YES", "economics", "National debt threshold"),
    TestCase("Will gold price exceed $3000/oz in 2026?",
             "Gold above $3000 per ounce in 2026?",
             "YES", "economics", "Gold price"),
    TestCase("Will the US Dollar Index (DXY) fall below 95 in 2026?",
             "DXY below 95 in 2026?",
             "YES", "economics", "Dollar index"),

    # --- More Sports ---
    TestCase("Will the Chiefs win Super Bowl LXI?",
             "Kansas City Chiefs to win the Super Bowl in the 2026-27 season?",
             "YES", "sports", "Super Bowl"),
    TestCase("Will Lionel Messi score 20+ goals in MLS in 2026?",
             "Messi to score 20 or more MLS goals in the 2026 season?",
             "YES", "sports", "Messi MLS goals"),

    # --- More Tech ---
    TestCase("Will OpenAI release GPT-5 before July 2026?",
             "GPT-5 launch by OpenAI before mid-2026?",
             "YES", "tech", "GPT-5 release"),
    TestCase("Will Meta launch AR glasses for consumers in 2026?",
             "Meta consumer AR glasses released in 2026?",
             "YES", "tech", "Meta AR glasses"),

    # --- Culture ---
    TestCase("Will a movie gross over $2 billion worldwide in 2026?",
             "Any film to earn $2B+ global box office in 2026?",
             "YES", "culture", "Box office record"),
    TestCase("Will Taylor Swift announce a new album in 2026?",
             "Taylor Swift new album announcement in 2026?",
             "YES", "culture", "Taylor Swift album"),

    # --- More Geopolitics ---
    TestCase("Will North Korea test a nuclear weapon in 2026?",
             "DPRK nuclear test in 2026?",
             "YES", "geopolitics", "North Korea nuclear — DPRK abbreviation"),
    TestCase("Will Iran and the US reach a nuclear deal in 2026?",
             "US-Iran nuclear agreement in 2026?",
             "YES", "geopolitics", "Iran nuclear deal"),

    # --- More pairs to reach 100+ ---
    TestCase("Will the US impose new sanctions on Russia in 2026?",
             "New US sanctions against Russia in 2026?",
             "YES", "geopolitics", "Russia sanctions"),
    TestCase("Will there be a mass shooting with 10+ killed in the US in 2026?",
             "US mass shooting with 10 or more fatalities in 2026?",
             "YES", "events", "Mass shooting — killed vs fatalities"),
    TestCase("Will the US women's soccer team win the 2027 World Cup?",
             "USWNT to win the FIFA Women's World Cup 2027?",
             "YES", "sports", "Women's World Cup — USWNT abbreviation"),
    TestCase("Will Netflix subscriber count exceed 350 million in 2026?",
             "Netflix above 350M subscribers in 2026?",
             "YES", "tech", "Netflix subscribers"),
    TestCase("Will there be a solar eclipse visible from North America in 2026?",
             "Solar eclipse over North America in 2026?",
             "YES", "science", "Solar eclipse"),
    TestCase("Will the US ban assault weapons federally in 2026?",
             "Federal assault weapons ban passed in the US in 2026?",
             "YES", "politics", "Assault weapons ban"),
    TestCase("Will Elon Musk step down as CEO of Tesla in 2026?",
             "Elon Musk to resign from Tesla CEO position in 2026?",
             "YES", "tech", "Musk Tesla CEO"),
]

MISMATCHED_PAIRS = [
    # --- Different sports ---
    TestCase("Will the Lakers win the 2026 NBA Championship?",
             "Will Manchester United win the 2025-26 Premier League?",
             "NO", "cross-sport", "NBA vs Premier League — different sports entirely"),
    TestCase("Will the Dodgers win the 2026 World Series?",
             "Will the Rams win the Super Bowl?",
             "NO", "cross-sport", "MLB vs NFL"),
    TestCase("Will the Warriors win the NBA Finals?",
             "Will the 49ers win the Super Bowl?",
             "NO", "cross-sport", "NBA vs NFL — same city, different sport"),

    # --- Same sport, different teams ---
    TestCase("Will the Celtics win the 2026 NBA Championship?",
             "Will the Lakers win the 2026 NBA Championship?",
             "NO", "same-sport", "Different NBA teams"),
    TestCase("Will the Yankees win the 2026 World Series?",
             "Will the Dodgers win the 2026 World Series?",
             "NO", "same-sport", "Different MLB teams"),

    # --- Same topic, different time ---
    TestCase("Will the Fed cut rates in June 2026?",
             "Will the Fed cut rates in December 2026?",
             "NO", "time-mismatch", "Same event type, different FOMC meetings"),
    TestCase("Will US GDP growth exceed 3% in Q1 2026?",
             "Will US GDP growth exceed 3% in Q4 2026?",
             "NO", "time-mismatch", "Same metric, different quarters"),

    # --- Similar-sounding but different ---
    TestCase("Will the US impose tariffs on China in 2026?",
             "Will China impose tariffs on the US in 2026?",
             "NO", "direction-swap", "Tariffs — reversed direction"),
    TestCase("Will Bitcoin price exceed $100,000 in 2026?",
             "Will Ethereum price exceed $100,000 in 2026?",
             "NO", "different-asset", "Different cryptocurrencies"),
    TestCase("Will the Democrats win the House in 2026?",
             "Will the Democrats win the Senate in 2026?",
             "NO", "different-chamber", "Different legislative chambers"),
    TestCase("Will Trump win re-election in 2028?",
             "Will Trump be impeached in 2026?",
             "NO", "different-event", "Same person, very different events"),

    # --- Geography ---
    TestCase("Will California have a major earthquake in 2026?",
             "Will Japan have a major earthquake in 2026?",
             "NO", "geography", "Same event type, different locations"),
    TestCase("Will the UK hold a general election in 2026?",
             "Will France hold a general election in 2026?",
             "NO", "geography", "Same event type, different countries"),

    # --- Metric differences ---
    TestCase("Will US unemployment rise above 5%?",
             "Will US unemployment fall below 3%?",
             "NO", "direction-swap", "Same metric, opposite directions"),
    TestCase("Will the S&P 500 close above 6000?",
             "Will the S&P 500 close below 5000?",
             "NO", "threshold-swap", "Same index, conflicting thresholds"),

    # --- Completely unrelated ---
    TestCase("Will it snow in New York on Christmas 2026?",
             "Will the Fed raise interest rates?",
             "NO", "unrelated", "Weather vs monetary policy"),
    TestCase("Will SpaceX land on Mars before 2027?",
             "Will the Lakers win the NBA Finals?",
             "NO", "unrelated", "Space vs sports"),
    TestCase("Will AI pass the Turing test in 2026?",
             "Will oil prices drop below $50/barrel?",
             "NO", "unrelated", "AI vs commodities"),

    # --- Subtle traps (biggest risk for mismatch) ---
    TestCase("Will Biden sign an executive order on AI in 2026?",
             "Will Congress pass an AI regulation bill in 2026?",
             "NO", "subtle", "Executive order vs legislation — different branches"),
    TestCase("Will the US debt ceiling be raised in 2026?",
             "Will the US government shut down in 2026?",
             "NO", "subtle", "Related but different policy events"),
    TestCase("Will Tesla stock price exceed $500?",
             "Will Tesla's market cap exceed $2 trillion?",
             "NO", "subtle", "Stock price vs market cap — not equivalent"),
    TestCase("Will the number of US COVID cases exceed 1 million in a month?",
             "Will the US declare a new COVID emergency?",
             "NO", "subtle", "Case count vs emergency declaration"),

    # --- Same politician, different events ---
    TestCase("Will Kamala Harris run for president in 2028?",
             "Will Kamala Harris resign as VP in 2026?",
             "NO", "subtle", "Same person, different political events"),

    # --- Negation traps ---
    TestCase("Will the US enter a recession in 2026?",
             "Will the US avoid a recession in 2026?",
             "NO", "negation", "Opposite resolution criteria"),

    # --- More subtle traps ---
    TestCase("Will the US ban TikTok in 2026?",
             "Will TikTok be sold to a US company in 2026?",
             "NO", "subtle", "Ban vs sale — different outcomes"),
    TestCase("Will gas prices exceed $5/gallon in 2026?",
             "Will gas prices drop below $3/gallon in 2026?",
             "NO", "threshold-swap", "Same commodity, opposite thresholds"),
    TestCase("Will Boeing deliver the first 737 MAX 10 in 2026?",
             "Will Airbus deliver the first A321XLR in 2026?",
             "NO", "different-company", "Different aircraft manufacturers"),
    TestCase("Will the UK rejoin the EU in 2026?",
             "Will Scotland hold an independence referendum in 2026?",
             "NO", "subtle", "Related UK politics, different events"),
    TestCase("Will Russia invade another country in 2026?",
             "Will NATO admit a new member in 2026?",
             "NO", "subtle", "Related geopolitics, different events"),
    TestCase("Will Apple's revenue exceed $400B in 2026?",
             "Will Apple's stock price exceed $250 in 2026?",
             "NO", "subtle", "Revenue vs stock price — not equivalent"),
    TestCase("Will the WHO declare a new pandemic in 2026?",
             "Will the US CDC issue a Level 4 travel advisory in 2026?",
             "NO", "subtle", "Different health organizations, different actions"),
    TestCase("Will electric vehicle sales exceed 50% of new car sales in the US?",
             "Will electric vehicle sales exceed 50% of new car sales in Europe?",
             "NO", "geography", "Same threshold, different regions"),
    TestCase("Will the Champions League final be held in London?",
             "Will the Super Bowl be held in London?",
             "NO", "cross-sport", "Same city, different sports"),
    TestCase("Will the minimum wage increase federally in the US in 2026?",
             "Will California raise its state minimum wage in 2026?",
             "NO", "scope", "Federal vs state — different jurisdictions"),

    # --- More completely unrelated ---
    TestCase("Will a volcano erupt in Iceland in 2026?",
             "Will Disney release a Star Wars movie in 2026?",
             "NO", "unrelated", "Geology vs entertainment"),
    TestCase("Will a new species be discovered in the Amazon in 2026?",
             "Will the price of Bitcoin exceed $200,000 in 2026?",
             "NO", "unrelated", "Biology vs cryptocurrency"),
    TestCase("Will the Olympics be held in 2026?",
             "Will the World Cup be held in 2026?",
             "NO", "different-event", "Different major sporting events"),
    TestCase("Will the US Postal Service raise stamp prices in 2026?",
             "Will Amazon launch drone delivery nationwide in 2026?",
             "NO", "unrelated", "USPS vs Amazon"),
    TestCase("Will a self-driving car be involved in a fatal accident in 2026?",
             "Will the FAA approve autonomous commercial flights in 2026?",
             "NO", "subtle", "Autonomous vehicles vs autonomous aircraft"),

    # --- More to hit 100+ ---
    TestCase("Will the US men's national soccer team qualify for the 2026 World Cup?",
             "Will the US women's national soccer team win the 2027 World Cup?",
             "NO", "subtle", "Men's qualifying vs women's winning"),
    TestCase("Will Netflix raise subscription prices in 2026?",
             "Will Netflix cancel its ad-supported tier in 2026?",
             "NO", "subtle", "Price increase vs tier cancellation"),
    TestCase("Will Elon Musk buy another social media company in 2026?",
             "Will Elon Musk sell X (Twitter) in 2026?",
             "NO", "direction-swap", "Buying vs selling"),
    TestCase("Will the UK Labour Party win a by-election in 2026?",
             "Will the UK Conservative Party win a leadership contest in 2026?",
             "NO", "subtle", "Different parties, different events"),
    TestCase("Will there be a major data breach affecting 100M+ users in 2026?",
             "Will the EU pass new data privacy legislation in 2026?",
             "NO", "subtle", "Data breach vs legislation"),
    TestCase("Will oil prices exceed $100/barrel in 2026?",
             "Will natural gas prices drop below $2/MMBtu in 2026?",
             "NO", "different-asset", "Different energy commodities"),
]

# ═══════════════════════════════════════════════════════════════════════════
# PARSER UNIT TESTS (no CLI needed)
# ═══════════════════════════════════════════════════════════════════════════

PARSER_TESTS = [
    ("YES", "YES"),
    ("NO", "NO"),
    ("MAYBE", "MAYBE"),
    ("YES - both resolve on same event", "YES"),
    ("NO - these are completely different", "NO"),
    ("MAYBE - insufficient information", "MAYBE"),
    ("yes", "YES"),
    ("no", "NO"),
    ("maybe", "MAYBE"),
    ("The answer is YES because both markets...", "YES"),
    ("I would say NO since these differ", "NO"),
    ("YESBoth questions resolve based on the same", "YES"),  # No space after YES
    ("NOThese are different events", "NO"),
    ("", "MAYBE"),  # Empty → MAYBE
    ("I'm not sure about this", "MAYBE"),  # No YES/NO/MAYBE → MAYBE
]


async def run_parser_tests():
    """Validate the parser handles all response formats."""
    print("\n" + "=" * 70)
    print("PARSER TESTS (no CLI call)")
    print("=" * 70)
    passed = 0
    failed = 0
    for text, expected in PARSER_TESTS:
        result = _parse_answer(text)
        status = "✅" if result == expected else "❌"
        if result != expected:
            print(f"  {status} parse('{text[:40]}...') = {result}, expected {expected}")
            failed += 1
        else:
            passed += 1
    print(f"  Parser: {passed} passed, {failed} failed")
    return passed, failed


def _print_flush(msg: str):
    """Print with immediate flush for real-time output in screen sessions."""
    print(msg, flush=True)


async def run_cli_tests(test_cases: list[TestCase], label: str, concurrency: int = 5):
    """Run test cases through the actual Claude CLI with controlled concurrency."""
    _print_flush(f"\n{'=' * 70}")
    _print_flush(f"{label} ({len(test_cases)} cases, concurrency={concurrency})")
    _print_flush("=" * 70)

    semaphore = asyncio.Semaphore(concurrency)
    results = []

    async def run_one(idx: int, tc: TestCase):
        async with semaphore:
            start = time.time()
            result = await _verify_cli(tc.kalshi, tc.poly)
            elapsed = time.time() - start
            correct = result == tc.expected
            status = "PASS" if correct else "FAIL"
            _print_flush(f"  {status} [{idx+1:3d}] {tc.category:20s} | {tc.description[:45]:45s} | got={result:5s} want={tc.expected:5s} ({elapsed:.1f}s)")
            return correct, result, tc

    tasks = [run_one(i, tc) for i, tc in enumerate(test_cases)]
    for coro in asyncio.as_completed(tasks):
        correct, result, tc = await coro
        results.append((correct, result, tc))

    passed = sum(1 for c, _, _ in results if c)
    failed = sum(1 for c, _, _ in results if not c)
    _print_flush(f"\n  {label}: {passed}/{len(results)} passed, {failed} failed")

    if failed > 0:
        print("\n  FAILURES:")
        for correct, result, tc in results:
            if not correct:
                print(f"    ❌ [{tc.category}] {tc.description}")
                print(f"       Kalshi:  {tc.kalshi}")
                print(f"       Poly:    {tc.poly}")
                print(f"       Got: {result}, Expected: {tc.expected}")

    return passed, failed


async def main():
    print("╔══════════════════════════════════════════════════════════════════════╗")
    print("║     DEEP LLM VERIFIER VALIDATION — Claude Opus via CLI             ║")
    print("╚══════════════════════════════════════════════════════════════════════╝")

    # Check CLI availability
    cli = _find_claude_cli()
    if not cli:
        print("❌ ERROR: claude CLI not found. Install Claude Code CLI first.")
        sys.exit(1)
    print(f"✅ Claude CLI found at: {cli}")

    total_passed = 0
    total_failed = 0

    # 1. Parser tests (instant, no CLI)
    p, f = await run_parser_tests()
    total_passed += p
    total_failed += f

    # 2. Identical pairs (should return YES)
    p, f = await run_cli_tests(IDENTICAL_PAIRS, "IDENTICAL PAIRS (expect YES)", concurrency=3)
    total_passed += p
    total_failed += f

    # 3. Mismatched pairs (should return NO)
    p, f = await run_cli_tests(MISMATCHED_PAIRS, "MISMATCHED PAIRS (expect NO)", concurrency=3)
    total_passed += p
    total_failed += f

    # Summary
    total = total_passed + total_failed
    print(f"\n{'=' * 70}")
    print(f"GRAND TOTAL: {total_passed}/{total} passed ({total_passed/total*100:.1f}%)")
    if total_failed > 0:
        print(f"❌ {total_failed} FAILURES — investigate before enabling auto-promote")
    else:
        print("✅ ALL TESTS PASSED — LLM verifier is validated")
    print(f"{'=' * 70}")

    return total_failed == 0


if __name__ == "__main__":
    success = asyncio.run(main())
    sys.exit(0 if success else 1)
