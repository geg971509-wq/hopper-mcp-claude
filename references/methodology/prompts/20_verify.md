# 20 - Verification gate (independent, one unit)

You are an independent checker. A previous pass named the functions of one unit
and wrote a reconstruction. Your job is to refute, not to rubber-stamp. Default
to refute when in doubt.

## Procedure

1. Re-derive blind. Without reading the assigned names first, decompile each
   address yourself and state what you think it does and what its prototype is,
   from the disassembly, strings, and xrefs alone.
2. Compare with the assigned names and the reconstruction.
3. Any of these fails the unit:
   - an assigned name contradicts the function's demangled symbol
   - the prototype is inconsistent with callers or callees
   - the reconstruction references SDK APIs whose signatures do not check out
   - fewer than two genuinely independent pieces of evidence
   - any invented detail not supported by disassembly or metadata
4. Return a verdict per function (confirm or refute) and an overall agree flag.
   The unit passes only if every function is confirmed and all checks pass. Be
   specific about the contradiction or the missing evidence.
