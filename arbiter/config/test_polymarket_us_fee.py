import math
from arbiter.config.settings import polymarket_us_order_fee


def test_taker_fee_at_fifty_cents():
    # Θ_taker * C * p * (1-p) = 0.05 * 100 * 0.5 * 0.5 = 1.25
    fee = polymarket_us_order_fee(price=0.50, qty=100, intent="taker")
    assert math.isclose(fee, 1.25, abs_tol=0.005)


def test_maker_fee_is_negative_rebate():
    # Θ_maker = -0.0125 → negative = rebate
    fee = polymarket_us_order_fee(price=0.50, qty=100, intent="maker")
    assert fee < 0, "maker must return negative (rebate)"
    assert math.isclose(fee, -0.3125, abs_tol=0.005)


def test_symmetric_in_price():
    # f(0.3) == f(0.7)
    a = polymarket_us_order_fee(price=0.3, qty=100, intent="taker")
    b = polymarket_us_order_fee(price=0.7, qty=100, intent="taker")
    assert math.isclose(a, b, abs_tol=0.005)


def test_rounds_to_cent_bankers():
    fee = polymarket_us_order_fee(price=0.51, qty=5, intent="taker")
    cents = round(fee * 100)
    assert fee * 100 == cents  # exact cent


def test_zero_fee_at_edges():
    assert polymarket_us_order_fee(price=0.0, qty=100, intent="taker") == 0.0
    assert polymarket_us_order_fee(price=1.0, qty=100, intent="taker") == 0.0
