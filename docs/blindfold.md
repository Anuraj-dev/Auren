# Blindfold mode

**Blindfold mode exists only for the blindfold test**: how far Auren gets from a short spoken request and
whatever is on the screen, with nobody near the user noticing. It isn't a mode for everyday use.

Turn it on with the **Blindfold** quick switch on the panel's Home page, `auren-ctl blindfold on|off|toggle`,
or `blindfold.enabled = true` in `config.toml` (`[blindfold]` table; default off). It is saved, and it
applies from the next request without a restart.

**Every action runs without confirmation, including sending, deleting and buying.** Blindfold forces the
approval level to `never` ([safety.md](safety.md#approval-levels)) while it is on, the floor included. The
saved `safety.level` doesn't change, and it applies again once Blindfold is off. Turn it on only when you
accept that whatever the brain is talked into, by the screen or by a misheard word, just runs.

## What it turns off

- **No voice:** no spoken replies, voice samples, greetings or "fresh start". The reply goes only to the
  history (`history.jsonl`).
- **No mascot:** the floating mascot and its bubble hide, and come back when Blindfold is off.
- **No notices:** no OSD, no desktop notifications, no sounds.
- **No questions:** nothing asks for a yes, and the brain is told never to ask in words.

## What it forces

- **Answering the question on screen is the default job.** Super+A pressed twice without a word (a quick
  double tap, or silence in between), or a microphone that fails before any words, sends
  "Answer the question on my screen." (`BLINDFOLD_DEFAULT`). Vague words ("do it", "this one") mean the same.
  Only words that clearly ask for something else change the job.
- **The screen is the context.** Each message carries a `<blindfold>` note (`BLINDFOLD` in
  `src/auren/brain/prompt.py`) that overrides the spoken-reply rules: start with `look_at_screen`, work in
  the focused window, never ask, look again after acting, and end with exactly `ok` or `failed: <why>`.
- **Only the user's window**, enforced in `src/auren/tools.py` (not just asked of the brain):
  - Allowed: `look_at_screen`, and `click_at`, `drag_at`, `scroll_at`, `type_text` and `press_keys` in the
    focused window; reading state (`system_state`, `system_lookup`, `system_info`); herdr agents and jobs,
    which run in the background (their tab is never shown).
  - Refused: everything else, including `launch_app`, `open_url`, the browser tools, window and workspace
    switching, `run_command`, Excalidraw, notifications and reminders.
  - A click, drag or scroll that lands on another window, the bar or the desktop is refused. So are keys
    that would go to another window, and keys that switch, open or close windows or tabs or use the
    clipboard: anything with Super, Alt+Tab, Ctrl+T/N/W, Ctrl+Tab, Ctrl+PgUp/PgDn, copy, cut and paste
    (including Shift+Insert and Ctrl+Shift+C/V/X), media and Print keys, Alt+F4.
  - A refused tool returns an error telling the brain to stay in the window or reply failed. It never asks.
- **Typing, never paste:** `type_text` types key by key and never touches the clipboard. On a US layout each
  character is its real key (with Shift where needed) from Auren's virtual keyboard, so apps see exactly what a
  keyboard would send and nothing needs checking. Other characters, or another layout, go through `wtype`, and
  then the brain is told to look again. Several lines are typed with Shift+Enter between them; in a terminal
  that fails instead (it could run a line). A code editor still auto-closes brackets and auto-indents as it
  would for a person.

## Green and red

The bar icon shows the result:

- **dim** while a Blindfold request runs (listening or working);
- **green** when the brain replied `ok`;
- **red** when it failed, until the next request.

Hover shows why it failed. `state.json` carries `blindfold`, `blindfoldResult` (`"ok"`, `"failed"` or
`null`) and `blindfoldReason`, which is also the `detail` ([ipc.md](ipc.md)).

Red means any of: the brain replied `failed: …` or anything other than `ok`; a brain error (not logged in,
a limit, no brain available); an unexpected error; no reply within 5 minutes (`BLINDFOLD_TIMEOUT`; the brain
and its tools are stopped); or the request being cancelled. No words is not a failure: it asks for the
default job. A refused tool that left the job unfinished shows up as the brain's
`failed: …`.

## Limits

- Shift+Enter in a single-line field may submit the form.
- A click on an empty spot (the desktop or the bar) is refused, like another window.
- "Focused window" is Hyprland's active window; a popup that opens as its own window counts as another one.
- Tested in `tests/test_blindfold.py` with a fake brain and fake `wtype`; the real microphone, real typing and
  the real bar are checked by hand.
