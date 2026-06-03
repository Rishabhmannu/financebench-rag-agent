# RBAC role-switch demo: analyst is blocked on a confidential invoice;
# clevel re-login unlocks the same query. RBAC enforced at the retrieval layer.
# Run from repo root with the stack UP:  vhs scripts/internal/demo/rbac.tape
# Output: docs/demos/rbac.gif

Output "docs/demos/rbac.gif"

Set Shell "zsh"
Set FontSize 14
Set Width 1280
Set Height 720
Set Theme "Dracula"
Set TypingSpeed 45ms
Set PlaybackSpeed 1.0

Hide
Type "setopt interactive_comments"
Enter
Sleep 300ms
Type "unset FB_PROFILE && rm -rf ~/.financebench/profiles/default.json"
Enter
Sleep 500ms
# WARM-UP (hidden): preload the reranker + LLM clients so the on-camera queries
# run warm and the Sleep timings below stay in sync. Cold start is ~60-70s and
# would desync the recording (the original no-warm-up version did exactly that).
Type "financebench login -u analyst"
Enter
Sleep 1s
Type "analyst123"
Enter
Sleep 4s
Type "financebench chat 'What was Apple revenue in fiscal year 2023?'"
Enter
Sleep 90s
Type "unset FB_PROFILE && rm -rf ~/.financebench/profiles/default.json"
Enter
Sleep 500ms
Type "clear"
Enter
Sleep 300ms
Show

Type "# Login as analyst - sees public 10-Ks only."
Enter
Sleep 1500ms
Type "financebench login -u analyst"
Enter
Sleep 1s
Type "analyst123"
Enter
Sleep 4s

Type "financebench chat 'What was Apple revenue in fiscal year 2023?'"
Enter
Sleep 30s

Type "# Now a confidential invoice. Analyst is not cleared."
Enter
Sleep 2s
Type "financebench chat 'Show me the TechSolutions consulting invoice details'"
Enter
Sleep 30s

Type "# Re-login as clevel - broader clearance."
Enter
Sleep 2s
Type "financebench login -u clevel"
Enter
Sleep 1s
Type "clevel123"
Enter
Sleep 4s

Type "financebench chat 'Show me the TechSolutions consulting invoice details'"
Enter
Sleep 45s

Type "# Same query, different role. RBAC enforced at the retrieval layer."
Enter
Sleep 4s
