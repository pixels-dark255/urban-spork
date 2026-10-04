"""
Strategy Arena - several intraday strategies paper-trading side by side with
simulated money, so the question "which algorithm actually makes money?" is
answered by outcomes after costs, not by how convincing a strategy sounds.

Modules:
  costs       Indian intraday equity charges + slippage (configurable).
  strategies  The competing strategies, plus a buy-at-open benchmark.
  engine      Bar-by-bar simulation, risk rules, end-of-day settlement,
              benching/reinstating, leaderboard. Pure functions over a plain
              dict state, so live trading and the historical replay share
              exactly one code path.
  store       Persistence (JSON file, or Postgres when DATABASE_URL is set).
  service     Glue between the scheduler/API and the engine.

Simulated money only. Nothing in this package places a real order.
"""
