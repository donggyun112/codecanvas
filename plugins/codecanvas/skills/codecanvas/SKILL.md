---
name: codecanvas
description: Use INSTEAD OF grep/ripgrep/reading whole files when a question about Python code has a call-graph or control-flow answer. Triggers - "who calls X", "what breaks if I change X", "what does X do", "what does X call", "when does this return/raise", "is this branch reachable", "can X reach Y", "where are the entrypoints/routes", "impact of this diff/PR", "why is this None/empty here", "trace this bug", "does this state have field F", plus any refactor, rename, signature change, dead-code check, or PR review on a Python repo. Also use before asserting any reachability or blast-radius claim as fact.
---

# CodeCanvas

Answers come from a real call graph + CFG built from the repo, not text search. Grep finds strings; these tools find edges. On a Python repo, reach here first.

## Pick the tool

| Question | Tool |
|---|---|
| Zero context, "how does X work / why does it do Y" | `logic_flow` — start here, one call covers resolution + branches + outcomes + callees + effects |
| Who calls this? Safe to change the signature? | `who_calls` |
| What does this reach downstream? Side effects? | `call_tree` |
| What does this do, in one glance? | `what_does` |
| How does its logic branch? | `function_flow` |
| Exact guard on each return/raise | `reaching_conditions` |
| Blast radius of a diff/PR | `analyze_impact` |
| Where does the app start? HTTP routes? | `list_entrypoints` |
| Find a symbol by name/pattern | `find_symbols` |
| "X can reach Y" / "this is unreachable" — before saying it | `verify_claim` |
| Bug depends on state shape | `validate_state_schema`, then `simulate_state_transition` |
| Is the project indexed? | `project_status` |

## Rules

- Pass `project_path` (repo root) once per session; later calls inherit it.
- `logic_flow` first for open-ended questions. Don't then re-run its component tools unless the result was truncated or ambiguous.
- Narrow with `filter` / `depth` / `kind` instead of paging through a capped result.
- Never state a reachability or impact claim from reading alone — run `verify_claim` or `who_calls` and cite the path.
- Python only. Other languages: fall back to grep.
