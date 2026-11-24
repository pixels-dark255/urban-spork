"""A short, stable hash of the numbers a phase produced.

Two runs of the same phase on the same input must print the same
fingerprint. That is the whole point: it turns "did anything change?" from
a careful diff of a results table into a glance at one line.

Floats are formatted before hashing rather than hashed as bytes. The same
value can arrive as 1.1, numpy.float64(1.1) or Decimal("1.1") depending on
which library touched it last, and those have different repr()s but mean
the same thing. Rounding to 9 significant figures also keeps the
fingerprint stable against the last-bit noise that a different BLAS or a
different pandas version can introduce - noise that would otherwise look
like a changed result.
"""
import hashlib
import math


PRECISION = 9


def canonical(value) -> str:
    """One stable spelling for a value, whatever type it arrived as."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if value is None:
        return "null"
    if isinstance(value, (int,)):
        return str(value)
    if isinstance(value, float) or hasattr(value, "__float__"):
        f = float(value)
        if math.isnan(f):
            return "nan"
        if math.isinf(f):
            return "inf" if f > 0 else "-inf"
        if f == 0:
            return "0"          # -0.0 and 0.0 are the same number
        return "%.*g" % (PRECISION, f)
    if isinstance(value, (list, tuple)):
        return "[" + ",".join(canonical(v) for v in value) + "]"
    if isinstance(value, dict):
        # Sorted, so a dict built in a different order still hashes the same.
        return "{" + ",".join("%s=%s" % (k, canonical(value[k]))
                              for k in sorted(value, key=str)) + "}"
    return str(value)


def fingerprint(values: dict) -> str:
    """A 12-character hash of the key numbers in `values`."""
    body = ";".join("%s=%s" % (k, canonical(values[k]))
                    for k in sorted(values, key=str))
    return hashlib.sha256(body.encode("utf-8")).hexdigest()[:12]


def line(phase, values: dict) -> str:
    """The one line a phase prints when it finishes."""
    return "FINGERPRINT phase=%s keys=%d sha=%s" % (
        phase, len(values), fingerprint(values))
