# Superseded 015 configurations

These files are kept unchanged as evidence for the verification they belonged to. **They must not be used for any launch.** The launcher refuses them.

- **`proposed-comparison-strict-db619a8.json`:** the proposed 015 configuration of 2026-10-16. It set `strict_ratio_learning: true` for guarded-clm `db619a8` (0.2.0.dev0).
  - **Why it is superseded:** guarded-clm `3f18473` (0.2.0.dev1) removed the option, so the launcher rejects any spec containing it, with a migration error.
  - **Its verification:** clm-lib-test-results, `experiments/015-public-clm-comparison/strict-ratio-verification.md`. That record is valid only for the revision and settings it recorded.
  - **Successor:** `../proposed-comparison-0.2.0.dev1.json`.
