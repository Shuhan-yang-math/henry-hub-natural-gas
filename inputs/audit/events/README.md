# BSEE and Sabine event-controller input

The data file is not tracked in Git. Artifact
`selected_event_reports_aligned` is downloaded from the exact GCS generation
pinned in `manifests/selected_strategy_inputs_2026-08-14.json` into the ignored
`inputs/gcs` cache.

`selected_event_reports_aligned` is the frozen report registry used by the
post-score pure short-veto controller.

| Property | Value |
|---|---|
| Rows | 101 BSEE reports |
| Event-date range | 2017-08-24 through 2024-09-29 |
| Local context | Sabine operational notices posted during the preceding three calendar days |
| SHA-256 | `f1a99a286c1a2a5b7b03990edfec08786aa9a56e0b7f5ad88417450fb984fb1b` |

The controller is eligible when the BSEE offshore-gas shut-in estimate worsens
from the prior tradable report and a recent Sabine operational notice is
present. It can set a conflicting short to zero, but it cannot create a
position or amplify exposure.
