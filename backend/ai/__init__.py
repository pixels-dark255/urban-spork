"""The AI layer: small, sealed, and off.

It may touch the CORE and nothing else. Intraday is far too fast for a
model call to be part of the loop, and a sleeve that needed one would be
a strategy whose rule nobody can write down.

Everything here is built to be auditable rather than clever. One document
type in, one call from a closed list out, saved immutably before it can
affect anything, and a replay that is honest about being a replay.
"""
