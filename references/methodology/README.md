# Evidence-gated de-obfuscation

Reverse engineering has no `tests pass`. The failure mode is a confident, wrong
name. This is a small workflow that keeps an LLM-assisted naming pass honest by
requiring evidence and an independent check before anything is accepted.

## The layers

Do the cheap, exact layers first. Only spend effort on the last one.

```
Layer 0  Demangle Swift symbols                         free, exact, no LLM
Layer 1  Obj-C classes and selectors                    ground truth, no LLM
Layer 2  Swift metadata: types, protocols, conformances  parser + cross-check
Layer 3  Anonymous sub_* : closures, thunks, inlined     LLM, then verify
```

Layers 0 and 1 already make most of a binary readable for free. If a function
already has a real demangled symbol, use it as is. The LLM is only for the
genuinely anonymous parts.

## When is a unit done

A unit is one class, or one small cluster of related functions, never a single
random function in isolation. It is accepted only when all of these hold:

1. Any function with a demangled symbol keeps a name consistent with it.
2. The prototype is recovered and not contradicted by callers or callees.
3. There are at least two independent pieces of evidence for the role, drawn
   from: nearby strings, Obj-C selectors, xrefs, Swift metadata.
4. The reconstruction type-checks by inspection against the real SDK signatures
   it touches.
5. An independent check re-derived the same role from scratch and agreed.

## Anti-hallucination rules

- No name without at least two pieces of evidence. Unsure means mark it analysed
  with low confidence and move on. A truthful "analysed" beats an invented name.
- Never invent struct fields, enum cases, or argument names that the disassembly
  or metadata does not support. Mark uncertainty explicitly.

## The metric

Track `accept_rate = verified / (verified + rejected)`.

- Below 0.5: the pass is inventing more than it finds. Tighten evidence, do not
  widen scope.
- 0.7 or above: healthy.

The prompts in `prompts/` implement this as three steps: inventory, one-unit
loop, and an independent verification gate.
