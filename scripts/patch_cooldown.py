#!/usr/bin/env python3
"""Patch auto_executor.py to add failed-execution cooldown."""
from pathlib import Path

p = Path(__file__).resolve().parent.parent / "arbiter" / "execution" / "auto_executor.py"
content = p.read_text()

# 1. Add cooldown map field
old1 = "        self._seen_dedup_keys: dict[str, float] = {}"
new1 = (
    "        self._seen_dedup_keys: dict[str, float] = {}\n"
    "        self._failed_cooldown: dict[str, float] = {}  # canonical_id -> cooldown_until"
)

# 2. Add cooldown check before dedup
old2 = "        dedup_key = self._dedup_key(opp, now)\n        if dedup_key in self._seen_dedup_keys:"
new2 = (
    '        # Cooldown after failed fill-or-kill (avoid spamming thin orderbooks)\n'
    '        cooldown_until = self._failed_cooldown.get(opp.canonical_id, 0.0)\n'
    '        if now < cooldown_until:\n'
    '            log.info(\n'
    '                "auto_executor.skip.failed_cooldown",\n'
    '                canonical_id=opp.canonical_id,\n'
    '                remaining=round(cooldown_until - now, 1),\n'
    '            )\n'
    '            return\n'
    '\n'
    '        dedup_key = self._dedup_key(opp, now)\n'
    '        if dedup_key in self._seen_dedup_keys:'
)

# 3. Set cooldown after failed execution
old3 = "        if result is not None:\n            self.stats.executed += 1"
new3 = (
    '        if result is None or getattr(result, "status", "") == "failed":\n'
    '            # Back off for 5 minutes after a failed attempt on this market\n'
    '            self._failed_cooldown[opp.canonical_id] = time.time() + 300.0\n'
    '        if result is not None:\n'
    '            self.stats.executed += 1'
)

ok = True
for old, new, label in [(old1, new1, "cooldown map"), (old2, new2, "cooldown check"), (old3, new3, "cooldown set")]:
    if old in content:
        content = content.replace(old, new, 1)
        print("PATCHED:", label)
    else:
        print("NOT FOUND:", label)
        ok = False

if ok:
    p.write_text(content)
    print("All patches applied successfully")
else:
    print("SOME PATCHES FAILED - file not modified")
