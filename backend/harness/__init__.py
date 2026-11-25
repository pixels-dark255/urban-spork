"""The runner harness: lock, checks, risk gate, logging, alerts.

Every step runs in a fixed order and any of them may stop the run. Stopping
is the normal, safe outcome: a harness that trades through a failed check is
worse than one that does nothing, because the first loses money quietly.
"""
