# Rule changelog

Append only. Every line is something that stopped trading, or was rotated,
and why. Nothing here is ever edited: a record that can be rewritten is a
story.

A kill line is a multiple of the worst drawdown a strategy was tested
through: 2x for a sleeve or intraday strategy, 1.5x for the core. Past it,
the thing running is no longer the thing that was tested.

Two separate events, deliberately:

- **switched off** happens the moment the line is crossed. That is safety.
- **out** means written out and eligible for replacement, and needs enough
  live evidence as well. One bad week switches a strategy off; it does not
  retire it.

Core changes are human-only. The core crossing 1.5x raises an alert and
blocks new sleeves, and changes nothing else by itself.

---

- **2026-11-25** Nothing has been switched off or rotated. `bench.yaml` has
  no survivors, so there is nothing live to kill: every strategy is
  observe-only with no capital (see AGENTS.md §5).
