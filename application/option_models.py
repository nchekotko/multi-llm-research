"""Scenario pricing models, not exchange settlement or execution prices."""

import math

YEAR = 365 * 86400


def cdf(x):
    return (1 + math.erf(x / math.sqrt(2))) / 2


def price(spot, strike, years, sigma, call, rate, carry, american=False, steps=32):
    """Generalized BSM / CRR. carry=0 for futures; discount=0 for margined scenario."""
    if min(spot, strike, years, sigma) <= 0:
        raise ValueError("invalid_pricing_inputs")
    sign = 1 if call else -1
    if not american:
        forward = spot * math.exp(carry * years)
        std = sigma * math.sqrt(years)
        d1 = math.log(forward / strike) / std + std / 2
        return math.exp(-rate * years) * sign * (forward * cdf(sign * d1) - strike * cdf(sign * (d1 - std)))
    dt = years / steps
    up = math.exp(sigma * math.sqrt(dt))
    down = 1 / up
    probability = (math.exp(carry * dt) - down) / (up - down)
    if not 0 <= probability <= 1:
        raise ValueError("binomial_probability_out_of_bounds")
    values = [max(sign * (spot * up**j * down ** (steps - j) - strike), 0) for j in range(steps + 1)]
    discount = math.exp(-rate * dt)
    for n in range(steps - 1, -1, -1):
        values = [
            max(
                discount * ((1 - probability) * values[j] + probability * values[j + 1]),
                sign * (spot * up**j * down ** (n - j) - strike),
                0,
            )
            for j in range(n + 1)
        ]
    return values[0]


def implied(target, *args):
    # args: spot, strike, years, call, rate, carry, american
    spot, strike, years, call, rate, carry, american = args

    def value(vol):
        return price(spot, strike, years, vol, call, rate, carry, american)

    # CRR needs a sufficiently large sigma for a valid probability.
    low = max(0.005, abs(carry) * math.sqrt(years / 32) * 1.01)
    high = 5.0
    if low >= high or not value(low) < target < value(high):
        raise ValueError("iv_out_of_bounds")
    for _ in range(26):
        mid = (low + high) / 2
        if value(mid) < target:
            low = mid
        else:
            high = mid
    return (low + high) / 2


def delta(spot, strike, years, sigma, call, rate, carry, american=False):
    step = spot * 1e-4
    return (
        price(spot + step, strike, years, sigma, call, rate, carry, american)
        - price(spot - step, strike, years, sigma, call, rate, carry, american)
    ) / (2 * step)
