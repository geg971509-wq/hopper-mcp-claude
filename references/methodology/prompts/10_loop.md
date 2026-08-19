# 10 - Loop iteration (fresh context each round, one unit only)

You carry no history. Your only sources are the ledger, the methodology notes,
and the live database. Process exactly one unit, then stop.

## Procedure

1. Pick the first untouched or analysed unit by priority whose target matches the
   open database. If none match, say so and stop.
2. Demangle before you guess. If an address already has a readable symbol, use it.
3. Decompile, and cross-check with a second decompiler if available.
4. Gather at least two independent pieces of evidence for the role: nearby
   strings, Obj-C selectors, callers and callees, Swift metadata. Write the raw
   dump and the evidence down.
5. If you have fewer than two pieces of evidence, or anything contradicts a
   demangled symbol, mark the unit analysed with low confidence, note what is
   blocking, and stop. Do not name.
6. If confident, persist names and prototypes into the database, write the
   reconstruction with a header listing addresses and evidence, mark uncertain
   parts explicitly.
7. Run the independent verification gate (prompt 20) for this unit. If it agrees,
   mark verified. If not, revert the renames and mark analysed.
8. Recompute the metric, update the ledger, append one line to the progress log.

## Rules

- One unit per iteration. Never re-open a verified unit.
- Only rename what this unit covers.
- A truthful analysed or rejected is a success. It keeps the metric honest.
