# RBAC role-switch demo: analyst hits the wall on confidential docs;
# clevel re-login unlocks the same query.
#
# Run: vhs scripts/internal/demo/rbac.tape  (~90s)
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
Type "clear"
Enter
Sleep 300ms
Show

Type "# Login as analyst — sees public 10-Ks only."
Enter
Sleep 1500ms

Type "financebench login -u analyst"
Enter
Sleep 500ms
Type "analyst123"
Enter
Sleep 4s

Type "financebench chat 'What was Apple revenue in fiscal year 2023?'"
Enter
Sleep 50s

Type "# Now try a confidential invoice. Analyst is not cleared."
Enter
Sleep 2s

Type "financebench chat 'Show me the TechSolutions consulting invoice details'"
Enter
Sleep 35s

Type "# Re-login as clevel — broader clearance."
Enter
Sleep 2s

Type "financebench login -u clevel"
Enter
Sleep 500ms
Type "clevel123"
Enter
Sleep 4s

Type "financebench chat 'Show me the TechSolutions consulting invoice details'"
Enter
Sleep 55s

Type "# Same query. Different role. RBAC enforced at the retrieval layer."
Enter
Sleep 4s
