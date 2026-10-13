# Verification performance TODO

- [x] Detect stalled verifier calls in Rethlas and retry within the existing attempt
  budget. Distinguish startup failure, long silent reasoning and hung tools; lack of
  visible events alone is not proof of a stall. Record the reason and preserve failed
  attempts. Keep total timeouts, quota stop (no retry), manual cancellation and frozen
  inputs intact. Test process cleanup and quota/cancellation races.
- [ ] Evaluate root-only partial reviews against decomposed partial reviews on the
  same frozen progress snapshots, keeping final full-solution review decomposed.
  Compare audited unique findings, route rejection disagreements, false alarms,
  latency and downstream wasted generation. Audit differences independently;
  neither mode's findings are ground truth. Do not change the default before evidence.
- [ ] Deferred: cross-iteration incremental review. Do not implement yet. Reusing
  prior conclusions requires dependency/context invalidation, original-question
  preservation and separate partial/full caches; a previous PASS is not a reusable
  correctness certificate.
