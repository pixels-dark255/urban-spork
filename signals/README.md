# signals/: shared signal code

**The code lives in `backend/signals/`.** The backend is the Python import
root (everything runs with `backend` on the path), so a package here could
not be imported without path games that break differently in CI and on
Render.

  `backend/signals/regime.py`  trend and volatility per bar
  `backend/signals/rules.py`   loads `rules.yaml`, the 7-part schema

The one signal module is `backend/arena/strategies.py` (AGENTS.md §1.2);
backtest and live runner both import it. Anything added extends that module
or builds on it. **Do not re-implement a rule here** - two copies of a rule
is the failure this layout exists to prevent.
