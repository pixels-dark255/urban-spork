# results/: machine-readable results

`locked_test_log.csv`   every locked-window run (AGENTS.md §1.6)
`phase_fingerprints.csv` one fingerprint per phase (AGENTS.md §1.7)
`asset_labels.json`     Phase 1's labels, so funnel gate 1 is reproducible
`funnel.csv`            the last funnel run, one row per idea
`phase9_fill_rule.md`   the same funnel on both fill rules, before and after
`slippage_sweep.md`     every idea at 0x, 0.5x, 1x and 2x of the fill assumption
`patient_family.md`     the trade-less variants through the funnel, and why they failed

Append-only. A row that is edited or removed destroys the audit trail that
makes a locked test mean anything.
