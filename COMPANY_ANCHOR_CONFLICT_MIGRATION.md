# Applying the anchor-conflict fix to existing Approved rows

The code change only affects rows resolved **after** it ships. Rows already in
Airtable carry a persisted `Outbound Hold` decision, so recovering them needs a
separate, deliberate backfill. This documents that path. **Nothing here is
implemented in this PR.**

## What must be recomputed

`Outbound Hold` is a signed field, so a repair cannot patch it alone. The full
set that must be rewritten together:

| field | why |
| --- | --- |
| `Outbound Company` | resolver output (unchanged for every recoverable row — see below) |
| `Outbound Company Confidence` | `low` → `medium` |
| `Outbound Company Identity` | resolver output |
| `Outbound Company Evidence` | now records `bridging_brand` and the new reason |
| `Outbound Hold` | `true` → `false` |
| `Validation Version` | must be `config.VALIDATION_VERSION` |
| `Validated At` | repair timestamp |
| `Validation Fingerprint` | **must be regenerated** |

## Signing

`validation_integrity.validation_fingerprint(fields)`, computed over
`validation_integrity.SIGNED_FIELDS` **after** the new display values are in
place. `prepare_outbound_backfill.py::_resolve_record` already implements exactly
this shape and is the code to reuse — do not hand-roll a second signer.

Patching a signed display field without re-signing leaves the row failing
`validation_fingerprint_mismatch`, which is strictly worse than the hold it was
meant to clear.

## Is the repair deterministic and idempotent?

**Yes, but only for the narrowly scoped set.** For all 14 rows the simulation
recovers, the display *name text is byte-identical* before and after
(`changes_outbound_name = false` on every one). The repair changes a confidence
label and a boolean, so re-running it is a no-op the second time.

## The scoping rule the backfill MUST apply

A blanket "recompute every held row and clear what clears" is **unsafe** and was
proven so during the backlog diagnosis: recomputing all 211 rows would rewrite
outbound-visible text on 5 of them, two to a *different company*
(`New York State Division of Criminal Justice Services` →
`NYS Division of Homeland Security & Emergency Services`;
`Diamond Jo Casino & Hotel` → `Blue Chip Casino Hotel Spa`).

So the backfill must select rows on both conditions:

1. the recomputed evidence carries a non-empty `bridging_brand`, **and**
2. the recomputed `Outbound Company` is byte-identical to the persisted one.

Any row failing either condition stays held and goes to manual review. That
scoping is what makes the repair provably text-preserving rather than a
re-resolution.

## Suggested sequence

1. Ship this PR. New rows resolve correctly from then on.
2. Run a dry-run backfill over `Status=Approved` applying the scoping rule; diff
   every proposed patch.
3. Review the proposed rows (12 distinct companies at the time of writing).
4. Apply with `prepare_outbound_backfill`'s existing re-signing, in one batch.
5. Re-run the Approved eligibility check to confirm the expected count moved.

## Risks

* The 4 recovered rows still blocked by `apollo_email_not_verified` will not
  become enrollable until Apollo verification is re-run — clearing their hold
  changes nothing on its own.
* Confidence `medium` is send-safe. If policy later tightens outbound to
  `high`-only, these rows drop out again.
* The bridge proves the two anchors share a brand, not that they are the same
  legal entity. It is deliberately not promoted to `high`.

## Re-validation against current main, 2026-09-04

The evidence above was gathered on 2026-08-28 against a 211-row Approved
population. The branch it was gathered on fell far behind `main`, so the change
was re-applied as a single commit on top of `main@4b41c4b` and the simulation
re-run read-only against **current** production data. The Approved population has
since grown to 992 rows.

Method: list `Status=Approved`, resolve each row's company display twice with the
cache disabled in both directions — once with `_bridging_brand` live, once with it
stubbed to `""` to reproduce the old behaviour — and diff.

| | 2026-08-28 (211 rows) | 2026-09-04 (992 rows) |
| --- | --- | --- |
| real conflict pairs | 136 | **135** |
| holds cleared | 14 | **14** |
| distinct companies cleared | 12 | **12** |
| known-unsafe pairs cleared | 0 | **0** |
| unknown pairs still held | 120 | **121** |
| rows newly held (regression) | 0 | **0** |
| outbound-visible text changes | 0 | **0** |
| rows whose already-agreeing anchors changed | — | **0** |
| cleared rows whose recomputed name is byte-identical to the persisted one | — | **14 / 14** |
| cleared rows with a verified email (i.e. actually enrollable) | 10 | **10** |

Both known-unsafe pairs are still held, with the same evidence as before:

```
Massachusetts Department of Developmental Services  linkedin:ddsmass       mass.gov
Blue Chip Casino Hotel Spa                          linkedin:diamondjoworth boydgaming.com
```

The 14 cleared rows and their bridging brands:

```
opus  martin  weissman  newfund  clark x3  alterman
keith  blueground  lalo  gelt  carpe  volohealth
```

Every one moves `low -> medium` confidence and `Outbound Hold: true -> false`,
and none changes a character of `Outbound Company`.

The simulation writes `reports/anchor_conflict_revalidation.json`, which is
gitignored: it carries real company text and Airtable record ids.

## Status of the backfill

**Not applied.** The code change ships on its own. The Airtable migration remains
a separate, explicitly authorized operation and still requires, in order: a
rollback snapshot, the exact record-id list, the `bridging_brand` + byte-identical
text scoping rule above, re-signing of the whole signed field set together, a
small canary with a read-back, and only then the remaining rows.
