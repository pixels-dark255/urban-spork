"""Shared signal code.

AGENTS.md §1.2: the backtest and the live runner import the same rules from
one place. This package holds the parts that are shared across phases; the
Arena's own strategy rules live in arena/strategies.py and are wrapped, not
replaced, by signals/rules.py.
"""
