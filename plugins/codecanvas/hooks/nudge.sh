#!/usr/bin/env bash
# PreToolUse: when a text search/read targets Python, remind the model that
# codecanvas answers call-graph questions from edges, not strings.
# ponytail: fires on every .py read/grep; add per-session throttle if it gets noisy.
input=$(cat)
printf '%s' "$input" | grep -Eq '\.py(\\|"|'"'"'| |$)' || exit 0
cat <<'EOF'
{"hookSpecificOutput":{"hookEventName":"PreToolUse","additionalContext":"codecanvas: this touches Python. If the question is 'who calls X', 'what breaks if I change X', 'how does X branch', 'can X reach Y', or 'impact of this diff', grep/read give strings — codecanvas gives graph edges. Load schemas with ToolSearch \"select:mcp__plugin_codecanvas_codecanvas__logic_flow,mcp__plugin_codecanvas_codecanvas__who_calls,mcp__plugin_codecanvas_codecanvas__analyze_impact\" and pass project_path (repo root) once. Plain file reads/edits: continue as-is."}}
EOF
