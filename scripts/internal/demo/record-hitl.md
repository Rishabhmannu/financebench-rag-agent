# Recording the HITL demo (`docs/demos/hitl.gif`)

The shipped HITL demo is a **two-pane** terminal recording: finance (left) is
blocked at the approval gate while admin (right) reviews and approves, and the
released answer streams back into the finance pane. VHS records a single
terminal, so the two panes come from **tmux**, and the session is captured live
with **asciinema** (you drive the async cross-pane timing by hand, then trim).
This is why hitl is recorded differently from `rbac.tape` / `memory.tape`.

Requires: `brew install asciinema agg tmux`. Stack up + healthy
(`curl -sf http://localhost:8000/v1/health`).

## 1. Clean slate (not recorded)

```bash
financebench login -u admin            # admin123
financebench approvals review          # approve/reject EVERY item until the inbox is EMPTY
rm -f ~/.financebench/profiles/*.json  # fresh logins on camera
```
Maximize the terminal window first (asciinema captures its current size). If the
inbox is not cleared, stale pending requests show up on camera.

## 2. Record

```bash
asciinema rec --overwrite ~/hitl.cast
tmux -f /dev/null new -s demo
#   Ctrl-b %      split LEFT | RIGHT       Ctrl-b ←/→   move panes
# LEFT  (finance): export FB_PROFILE=finance ; financebench login -u finance ; financebench chat
#   -> "What is the total due on the Global Consulting Partners invoice?"  -> Pending box (waits)
# RIGHT (admin):   export FB_PROFILE=admin ; financebench login -u admin ; financebench chat
#   -> /approvals -> Enter (select) -> Enter (Approve) -> type a note -> Enter
# LEFT:  the released answer auto-appears (the poll-wait resolves)
# exit both panes (Ctrl-D in each REPL, then `exit`), then Ctrl-D to stop asciinema
```

The invoice query reads a real document figure (~$197,653), so the gate trips
on a clean amount — avoid round-number prompts like "$500,000", which a draft
can balloon into a nonsensical figure.

## 3. Trim + render

asciinema 3.x writes asciicast **v3**; `agg` reads **v2**. Trim the trailing
tmux-exit cruft (keep the header + events through the released-answer frame,
drop everything after — a short Python pass over the JSONL does this), then:

```bash
asciinema convert -f asciicast-v2 --overwrite ~/hitl_trim.cast ~/hitl_v2.cast
agg --speed 2 --theme dracula --font-size 16 ~/hitl_v2.cast docs/demos/hitl.gif
```

`agg` cannot read v3 — always convert to v2 first. Tune `--speed` on the v2 file
without re-recording.
