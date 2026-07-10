# Resolver: gapped-suffix matching + qualified-name miss suggestions

Date: 2026-07-10
Status: proposed
Area: `core/codecanvas_mcp/mcp/queries.py` — `resolve_function`

## Problem

A real session (`~/Downloads/codecanvas-log.md`) ended with the agent abandoning
CodeCanvas and reading raw source. The trigger:

```
what_does(function: "UserTransferService._do_transfer")
-> { "error": "No function matching 'UserTransferService._do_transfer'." }
```

`_do_transfer` is a nested function defined inside the method `transfer_user`.
Its qualified name is `…UserTransferService.transfer_user._do_transfer`.

Reproduced in-repo against a fixture (`scratchpad/repro.py`):

| Query | Today |
|---|---|
| `_do_transfer` | ✅ resolves |
| `transfer_user._do_transfer` | ✅ resolves |
| `UserTransferService.transfer_user._do_transfer` | ✅ resolves |
| `UserTransferService._do_transfer` | ❌ MISS, suggestions=`['UserTransferService']` |

Two independent defects:

1. **Gapped suffix misses.** `resolve_function` step 3 matches only *contiguous*
   dot-suffixes (`qualified_name == ref or endswith("." + ref)`). The query skips
   the enclosing scope `transfer_user`, so it is a *subsequence*, not a suffix —
   even though the function is fully indexed (nested defs have been indexed since
   commit `cc25085`).
2. **Miss-suggestions mislead.** Step 4 runs `difflib.get_close_matches(ref, names)`
   where `names` are *bare* simple names, against a *dotted* `ref`. It returned
   `['UserTransferService']` (the class) and never surfaced the one string that
   rescues the user (`…transfer_user._do_transfer`). This degrades *every* miss,
   not just this pattern.

Out of scope: the malformed JSON elsewhere in the log is terminal copy-paste
noise, not tool output (a real malformed payload would break the MCP client).

## Design

Edit `resolve_function` only. Steps 1 (exact), 2 (`file:line`), 3 (contiguous
suffix) are unchanged — precision path stays first and untouched.

### Step 3b — gapped dot-boundary subsequence (new fallback)

Runs **only when step 3 finds nothing**, and **only for dotted refs**
(`len(ref.split(".")) >= 2`; a bare name that missed step 3 gains nothing here).

A qname matches `ref` when `ref`'s dotted segments occur **in order** within the
qname's segments **and the final segments coincide** (tail-anchored — the
function's own name must match):

```
def _gapped_suffix_match(qname: str, ref: str) -> bool:
    q = qname.split("."); r = ref.split(".")
    if len(r) < 2 or r[-1] != q[-1]:      # tail-anchored on the own name
        return False
    i = 0                                  # r must be an ordered subsequence of q
    for seg in q:
        if i < len(r) and seg == r[i]:
            i += 1
    return i == len(r)
```

- Exactly 1 candidate → return it.
- >1 candidate → `_rank_and_select(cg, ref, cands)` (auto-selects only a
  dominant candidate, else returns the ranked candidate list — same contract the
  contiguous path already uses; no new silent-pick risk).

Precision rationale: fallback-only + tail-anchored + `>=2` segments keeps this
from loosening the common contiguous path. `UserTransferService._do_transfer`
now resolves directly.

### Step 4 — rank qualified-name suffixes (rewrite)

Replace difflib-over-bare-names. On a genuine miss, suggest **qualified names**,
best-first:

1. `tail = ref.split(".")[-1]`.
2. Exact tail hits: functions whose simple `name == tail`. If any, these are the
   suggestions.
3. Else fuzzy: `difflib.get_close_matches(tail, {simple names})`, mapped back to
   the functions carrying those names.
4. Rank via existing `_rank_key` (non-test, concrete, fan-in); cap 5; emit each
   as its `qualified_name`.

So a miss on `Foo.bar` returns `['…a.b.bar', …]`, not `['Foo']` — the agent
retries with a copy-pasteable qualified name and recovers in one step.

## Testing (TDD)

New `core/tests/test_resolve_function.py` (no suite exists yet; this bootstraps
one). A fixture package with a class method containing a nested function, driven
through `resolve_function`:

- `UserTransferService._do_transfer` → resolves (regression for the log). 
- Bare / contiguous / exact forms still resolve (no regression).
- Ambiguous gapped ref across two classes → returns ranked `candidates`, no wrong
  auto-pick.
- Miss (`totally_absent`) → `suggestions` are qualified names, tail-ranked.
- Gapped match is tail-anchored: `Service.transfer_user` does **not** match via a
  mid-qname `transfer_user` when the tail differs.

## Non-goals

- No change to indexing, the call graph, or any other tool.
- No new matching for `file:line` or exact paths.
- No fuzzy matching on intermediate (non-tail) segments.
