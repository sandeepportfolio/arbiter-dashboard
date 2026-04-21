import asyncio
import inspect


# Moved to root conftest — pytest 8+ deprecates pytest_plugins in non-top-level conftests.
# Sandbox + live fixture modules are loaded as plugins here; individual test modules
# that don't consume these fixtures are unaffected (plugin loading is lazy).
pytest_plugins = [
    "arbiter.sandbox.fixtures.sandbox_db",
    "arbiter.sandbox.fixtures.kalshi_demo",
    "arbiter.sandbox.fixtures.polymarket_test",
    "arbiter.live.fixtures.production_db",
    "arbiter.live.fixtures.kalshi_production",
    "arbiter.live.fixtures.polymarket_production",
]


def pytest_addoption(parser):
    parser.addoption(
        "--live",
        action="store_true",
        default=False,
        help="Run live-marked scenarios against configured endpoints.",
    )


def pytest_configure(config):
    config.addinivalue_line("markers", "asyncio: run the test inside an asyncio event loop")
    config.addinivalue_line("markers", "live: live-fire scenario (opt-in via --live)")
    config.addinivalue_line("markers", "legacy_polymarket: legacy non-US CLOB tests")


def pytest_pyfunc_call(pyfuncitem):
    """Custom async test dispatch.

    G-3 fix (Plan 04-09, 2026-04-20): resolve async-generator fixtures before
    invoking the test. pytest-asyncio STRICT mode does not unwrap
    ``async def`` + ``yield`` fixtures for us when this custom hook is active,
    so sandbox fixtures like ``balance_snapshot`` arrive as raw
    ``async_generator`` objects. We drive each one through ``__anext__`` for
    setup and ``__anext__`` again for teardown, matching pytest's built-in
    async-fixture lifecycle.

    Backward-compatible: sync fixtures pass through unchanged; regular
    ``async def`` fixtures (no yield) that pytest already resolved arrive as
    their yielded value (not a coroutine/generator) and also pass through;
    only raw async-generator objects take the new setup/teardown path.
    """
    test_func = pyfuncitem.obj
    if not inspect.iscoroutinefunction(test_func):
        return None

    argnames = pyfuncitem._fixtureinfo.argnames

    async def _runner():
        resolved = {}
        active_generators = []  # list[(name, async_generator)] for teardown order
        try:
            for name in argnames:
                value = pyfuncitem.funcargs[name]
                if inspect.isasyncgen(value):
                    resolved[name] = await value.__anext__()
                    active_generators.append((name, value))
                elif inspect.iscoroutine(value):
                    resolved[name] = await value
                else:
                    resolved[name] = value
            await test_func(**resolved)
        finally:
            # Drive teardown in reverse order (LIFO -- mirrors pytest fixture teardown).
            for _, gen in reversed(active_generators):
                try:
                    await gen.__anext__()
                except StopAsyncIteration:
                    pass
                except Exception:
                    # Swallow teardown exceptions so the test's primary result
                    # stands; pytest's built-in async-fixture runner behaves
                    # the same way.
                    pass

    asyncio.run(_runner())
    return True
