"""Proving the backtester, before any result from it is believed.

Nothing here is a strategy and nothing here is a second backtester for
production use. These are instruments: a synthetic market whose answer is
known by construction, a guard that re-derives a signal from truncated
data, and a proof that the live path and the replay path produce the same
signals.
"""
