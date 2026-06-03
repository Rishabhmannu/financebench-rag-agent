# Multi-party HITL demo: finance submits a high-value query, sees Pending;
# admin (separate FB_PROFILE) reviews + approves.
#
# NOTE: the SHIPPED two-pane hitl.gif (finance + admin side by side) is recorded
# live with asciinema + tmux — see record-hitl.md in this folder. This VHS tape
# is a single-terminal fallback (one terminal, FB_PROFILE switch).
#
# Run: vhs scripts/internal/demo/hitl.tape  (~110s)
# Output: docs/demos/hitl.gif

Output "docs/demos/hitl.gif"

Set Shell "zsh"
Set FontSize 14
Set Width 1280
Set Height 760
Set Theme "Dracula"
Set TypingSpeed 45ms
Set PlaybackSpeed 1.0

Hide
Type "setopt interactive_comments"
Enter
Sleep 300ms
Type "rm -f ~/.financebench/profiles/finance.json ~/.financebench/profiles/admin.json"
Enter
Sleep 500ms
Type "clear"
Enter
Sleep 300ms
Show

Type "# Terminal A — finance role. Per-terminal identity via FB_PROFILE."
Enter
Sleep 1500ms

Type "export FB_PROFILE=finance"
Enter
Sleep 500ms

Type "financebench login -u finance"
Enter
Sleep 500ms
Type "finance123"
Enter
Sleep 4s

Type "financebench chat 'Approve a $500,000 expense reimbursement for TechSolutions consulting work'"
Enter
Sleep 55s

Ctrl+C
Sleep 2s

Type "# HITL triggered. $500K is above finance's $100K threshold."
Enter
Sleep 2s
Type "# Draft answer is NOT shown to the requester — they can't self-approve."
Enter
Sleep 3s

Type "# Switch terminals — admin reviews."
Enter
Sleep 1500ms

Type "export FB_PROFILE=admin"
Enter
Sleep 500ms

Type "financebench login -u admin"
Enter
Sleep 500ms
Type "admin123"
Enter
Sleep 4s

Type "financebench approvals review"
Enter
Sleep 5s

Enter
Sleep 5s

Enter
Sleep 3s

Type "Approved — within Q4 budget allocation"
Sleep 1s
Enter
Sleep 5s

Escape
Sleep 2s

Type "# Approved. Decision metadata (who, when, why) persisted to Postgres."
Enter
Sleep 4s
