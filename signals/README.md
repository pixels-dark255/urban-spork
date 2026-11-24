# signals/ — shared signal code

The one signal module is `backend/arena/strategies.py` (AGENTS.md §1.2);
backtest and the live runner both import it. Anything added here extends
that module or builds on it. **Do not re-implement a rule here** - two
copies of a rule is the failure this layout exists to prevent.
