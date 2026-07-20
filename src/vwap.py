"""
Pure-python cumulative VWAP. No numpy/pandas.

VWAP_t = cumsum(typical_price_i * volume_i for i in [start..t]) / cumsum(volume_i)

Falls back to a simple running average of price if cumulative volume is zero
(can happen on illiquid strikes in the first minute or two).
"""


def compute_cumulative_vwap(bars):
    """
    bars: list of dicts, each with keys 'price' (float) and 'volume' (float/int),
          already sorted in time order, starting from the VWAP anchor point
          (e.g. 09:15 market open).

    Returns: list of vwap values, same length/order as `bars`.
    """
    vwap_values = []
    cum_pv = 0.0
    cum_vol = 0.0
    running_price_sum = 0.0

    for i, bar in enumerate(bars):
        price = bar["price"]
        vol = bar.get("volume", 0) or 0

        cum_pv += price * vol
        cum_vol += vol
        running_price_sum += price

        if cum_vol > 0:
            vwap_values.append(cum_pv / cum_vol)
        else:
            # no volume yet anywhere in the window - fall back to simple mean
            vwap_values.append(running_price_sum / (i + 1))

    return vwap_values
