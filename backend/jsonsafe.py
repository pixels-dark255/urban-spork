"""
The last line of defence against NaN/inf escaping into JSON.

Two places reject non-finite floats outright, and both of them fail *after*
the real work is done, which is the worst possible time:

  - Starlette's JSONResponse serialises with ``allow_nan=False``, so a single
    NaN anywhere in a response body raises and the caller gets a 500 with no
    useful message.
  - Postgres JSONB refuses NaN, so a prediction or portfolio containing one
    fails to save and the write is silently lost.

Individual NaN sources are fixed at the source (see indicators.rsi,
indicators.summarize_timeframe and predictor.predict_price). This module is
the net under those fixes: cheap, total, and applied at the two boundaries
where a leak actually costs something. New code that computes a fresh
statistic does not have to remember the rule.

``clean`` replaces non-finite floats with None rather than 0.0 on purpose -
"no reading" is the truth, and a zero would be read as a real measurement.
"""
from __future__ import annotations

import math


def clean(value):
    """Recursively replace NaN/inf floats with None.

    Containers are rebuilt only as far as needed; dict keys are left alone
    (JSON keys are strings, and a NaN key is not a thing that happens here).
    """
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, dict):
        return {k: clean(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [clean(v) for v in value]
    # numpy scalars answer to float() but are not float instances.
    if hasattr(value, "item") and not isinstance(value, (str, bytes)):
        try:
            unwrapped = value.item()
        except (ValueError, AttributeError):
            return value
        return clean(unwrapped) if isinstance(unwrapped, float) else unwrapped
    return value
