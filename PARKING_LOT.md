# Parking lot

Ideas agreed on in principle but deferred. Not commitments; revisit
before building any of them.

## Styled messages beyond `shell`

`shell`/`pes` has styled messages (icons, color, highlighting; the
`say()`/`hl_*()` helpers in `src/pew.py`, on when stderr is a terminal
and `$NO_COLOR` isn't set). Rolling the same treatment out to the rest:

- **Turn it on for every subcommand.** Set `FANCY` in `main()` for all
  actions, not just `shell`. The ~75 `die()`/`warn()`/`info()` calls
  pick it up for free; the work is adding `hl_host()`/`hl_num()` to the
  messages that name hosts or counts.
- **`run`/`copy` failure summary.** `warning: 2/12 host(s) failed: a,b`
  becomes a red `✘` line with the failed hosts bold, matching the
  `--pretty` footer's final status line.
- **`--continue-on-fail` hint.** `stopping (pass --continue-on-fail/-X
  ...)`: the flag in cyan.
- **Settings and paths in cyan** (`pretty_style=`, `~/.pewrc` paths)
  in pewrc warnings.
- **Host lists in columns** for long multi-line errors (no-match
  suggestions, too-many-matches), instead of one per line.
- **Verbose by default everywhere?** `shell` always shows its messages;
  open question whether `run`/`copy`/`ping` should too, now that the dim
  `›` info lines are less noisy. Would need a way to turn it back off
  (`verbose=no` in pewrc, or a `--quiet-info`).
- **Emoji icons.** Follow `pretty_style=emoji` for message icons too
  (❌ ⚠️ ✅ 🔌 🛑). Skipped for now: double-width glyphs complicate the
  status line's width math.
- **A `messages=plain` pewrc setting**, if styled output ever needs
  turning off on a terminal without setting `$NO_COLOR` globally.
