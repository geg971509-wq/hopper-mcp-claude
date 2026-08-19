# 00 - Bootstrap: build the inventory (run once per target)

The target binary is open in a disassembler with an MCP bridge (for example
ida-pro-mcp). Run this once per target. Do not name functions here. This pass
only inventories and classifies.

## Steps

1. Confirm the right database is open (get_metadata) and matches the target.
2. Enumerate functions. For each, get the demangled name.
3. Classify into a layer:
   - readable: has a real demangled Swift symbol or an Obj-C selector.
     Mark verified, the loop will not touch these.
   - metadata-disputed: symbol present but ambiguous. Mark analysed, low conf.
   - anonymous: bare sub_*, no symbol. Mark untouched. This is the loop's work.
4. Group anonymous functions into units (not one-function units). Group by Swift
   type or module when metadata gives it, otherwise by call-graph cluster. Cap a
   unit at roughly 3 to 8 functions so one iteration can finish it.
5. Set priority for the modules you care about.
6. Write the inventory ledger and a one-line progress note. Rename nothing yet.
