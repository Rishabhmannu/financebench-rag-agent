# Conversation memory demo: follow-up queries resolve against thread history.
# Query resolver rewrites "And Microsoft?" using the prior Apple Q.
#
# Run: vhs scripts/demo/memory.tape  (~140s)
# Output: docs/demos/memory.gif

Output "docs/demos/memory.gif"

Set Shell "zsh"
Set FontSize 14
Set Width 1280
Set Height 760
Set Theme "Dracula"
Set TypingSpeed 45ms
Set PlaybackSpeed 1.0

Hide
Type "source /opt/anaconda3/etc/profile.d/conda.sh && conda activate agentic-ai"
Enter
Sleep 1s
Type "setopt interactive_comments"
Enter
Sleep 300ms
Type "cd /Users/rishabh/Documents/Documents/Agentic-AI-Course/CampusX-LangGraph-Course"
Enter
Sleep 500ms
Type "unset FB_PROFILE && rm -f ~/.financebench/profiles/default.json"
Enter
Sleep 500ms
Type "clear"
Enter
Sleep 300ms
Show

Type "# Conversation memory — follow-ups resolve against thread history."
Enter
Sleep 1500ms

Type "financebench login -u analyst"
Enter
Sleep 500ms
Type "analyst123"
Enter
Sleep 4s

Type "financebench chat"
Enter
Sleep 7s

Type "What was Apple revenue in fiscal year 2023?"
Enter
Sleep 50s

Type "And Microsoft?"
Enter
Sleep 50s

Type "# 'And Microsoft?' has no entities on its own."
Enter
Sleep 2s
Type "# Resolver rewrote it as 'What was Microsoft revenue in FY 2023?' using prior turns."
Enter
Sleep 4s

Type "/thread show"
Enter
Sleep 4s

Type "/quit"
Enter
Sleep 2s
