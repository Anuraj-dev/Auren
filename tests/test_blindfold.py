"""Blindfold mode (docs/blindfold.md): silent, no questions, only the user's focused window, keys typed one
by one, and a green or red result for the bar. Nothing here touches the real desktop."""

import asyncio
import json
import re
import threading

import pytest

from auren import daemon, notice, settings, tools
from auren.brain import BrainError, Interrupted, prompt
from auren.hands import clipboard, herdr, screen
from auren.tools import Runtime

SESSION = daemon.SESSION
FRONT = "0xFRONT"


# --- the setting, the level and the state -------------------------------------------------------------

def test_the_setting_is_off_by_default_and_saved_as_blindfold_enabled():
    assert settings.load()["blindfold"] is False
    settings.set_value("blindfold.enabled", "true")
    assert settings.load()["blindfold"] is True
    assert "[blindfold]" in settings.CONFIG_FILE.read_text()


def test_blindfold_forces_never_without_changing_the_users_level(tmp_path):
    rt = Runtime(tmp_path / "t.sock")
    rt.set_level("task")
    rt.set_blindfold(True)
    assert rt.level == "never" and rt.chosen == "task"
    rt.set_level("ask")                        # changed while it's on: saved, used once Blindfold is off
    assert rt.level == "never"
    rt.set_blindfold(False)
    assert rt.level == "ask"


class _Voice:
    name, names = "F2", ["F2"]

    def __init__(self):
        self.spoken, self.stopped = [], 0

    def speak(self, text):
        self.spoken.append(text)
        return True

    def stop(self):
        self.stopped += 1

    def tune(self, settings):
        pass

    def set_voice(self, name):
        pass


class _Brain:
    """Answers with `reply`, or raises `error`; with `hang`, waits until interrupted."""
    mode, using_backup, backup_until = "auto", False, None

    def __init__(self, reply="ok", error=None, hang=False):
        self.reply, self.error, self.hang = reply, error, hang
        self.asked, self.interrupted = [], threading.Event()
        self.primary = type("P", (), {"effort": "low"})()
        self.backup = type("B", (), {"model": "m", "effort": "low"})()

    def ask(self, text):
        self.asked.append(text)
        if self.hang:
            self.interrupted.wait(5)
            raise Interrupted("stopped")
        if self.error:
            raise self.error
        yield ("tool", "look_at_screen", {})
        yield ("say", self.reply)

    def interrupt(self):
        self.interrupted.set()

    def forget(self):
        pass

    def set_codex(self, model, effort):
        pass


class _Daemon(daemon.Daemon):
    """The real daemon's logic and state file, with a fake brain and voice."""

    def __init__(self, tmp_path, brain=None):
        self.settings = {**settings.defaults(), "voice": True}
        self.tools = Runtime(tmp_path / "tools.sock")
        self.brain = brain or _Brain()
        self.voice, self.speak_enabled = _Voice(), True
        self.profile, self.listening, self.vocab = None, None, []
        self.state, self.detail = "idle", ""
        self.level = daemon.Level()
        self.history, self.states = [], []

    def set_state(self, state, detail="", **extra):
        super().set_state(state, detail, **extra)
        self.states.append(json.loads(daemon.STATE_FILE.read_text()))

    def _history(self, record):
        self.history.append(record)


@pytest.fixture
def run(tmp_path, monkeypatch):
    monkeypatch.setattr(daemon, "RUNTIME_DIR", tmp_path)
    monkeypatch.setattr(daemon, "STATE_FILE", tmp_path / "state.json")

    def go(scenario, brain=None):
        async def main():
            d = _Daemon(tmp_path, brain)
            d.busy, d.loop = asyncio.Lock(), asyncio.get_running_loop()
            await scenario(d)
            return d
        return asyncio.run(main())
    return go


def _state(d) -> dict:
    return json.loads(daemon.STATE_FILE.read_text())


def test_the_command_saves_it_keeps_the_level_and_shows_it_in_the_state(run):
    async def scenario(d):
        d._save({"safety": "task"})
        d.tools.set_level("task")
        assert await d.command("blindfold maybe") == "usage: blindfold on|off|toggle"
        assert await d.command("blindfold on") == "blindfold on"
        assert d.tools.level == "never" and _state(d)["safety"] == "task" and _state(d)["blindfold"] is True
        assert _state(d)["blindfoldResult"] is None
        assert json.loads(await d.command("status"))["blindfold"] is True
        assert await d.command("blindfold toggle") == "blindfold off"
    d = run(scenario)
    saved = settings.load()
    assert saved["blindfold"] is False and saved["safety"] == "task" and d.tools.level == "task"
    assert notice.muted is False and d.voice.stopped == 1           # turning it on stopped any speech


def test_reload_picks_it_up_from_config_toml(run):
    async def scenario(d):
        settings.set_value("blindfold.enabled", "true")
        assert d._reload() == "Auren picked it up"
        assert d.tools.blindfold and _state(d)["blindfold"] is True
    run(scenario)


# --- a request: silent, and green or red ----------------------------------------------------------------

@pytest.mark.parametrize(("reply", "ok", "reason"), [
    ("ok", True, ""), ("OK.", True, ""), ('"ok"', True, ""),
    ("failed: no question on screen", False, "no question on screen"), ("Failed - window gone", False, "window gone"),
    ("failed", False, "failed"), ("Done, I typed it.", False, "Done, I typed it."), ("", False, "no reply"),
])
def test_the_reply_is_read_as_ok_or_failed(reply, ok, reason):
    assert daemon.blindfold_result(reply) == (ok, reason)


def test_a_request_is_silent_and_ends_green(run, _no_notices):
    async def scenario(d):
        await d.command("blindfold on")
        await d.respond("answer the question on screen", source="typed")
    d = run(scenario)
    assert d.voice.spoken == [] and _no_notices == []                # nothing said, no OSD, no notification
    assert d.brain.asked == ["answer the question on screen"] and d.tools.level == "never"
    assert d.history[-1]["reply"] == "ok"                           # the reply goes only to the history
    assert _state(d)["state"] == "idle" and _state(d)["blindfoldResult"] == "ok"
    assert any(s["state"] == "thinking" and s["blindfoldResult"] is None for s in d.states)   # reset while it runs


def test_a_failed_reply_ends_red_with_the_reason(run, _no_notices):
    async def scenario(d):
        await d.command("blindfold on")
        await d.respond("fill in the form", source="typed")
    d = run(scenario, _Brain(reply="failed: no form on screen"))
    s = _state(d)
    assert s["blindfoldResult"] == "failed" and s["detail"] == s["blindfoldReason"] == "no form on screen"
    assert d.voice.spoken == [] and _no_notices == []


def test_the_next_request_clears_the_last_result(run):
    async def scenario(d):
        await d.command("blindfold on")
        await d.respond("one", source="typed")
        d.brain.reply = "failed: nope"
        await d.respond("two", source="typed")
    d = run(scenario)
    assert [s["blindfoldResult"] for s in d.states if s["state"] in ("thinking", "idle")][-3:] == ["ok", None, "failed"]


def test_a_brain_error_ends_red(run, _no_notices):
    async def scenario(d):
        await d.command("blindfold on")
        await d.respond("answer it", source="typed")
    d = run(scenario, _Brain(error=BrainError("claude isn't logged in")))
    assert _state(d)["blindfoldResult"] == "failed" and "claude isn't logged in" in _state(d)["blindfoldReason"]
    assert d.voice.spoken == [] and _no_notices == []


def test_an_unexpected_exception_ends_red_not_stuck_thinking(run):
    async def scenario(d):
        await d.command("blindfold on")
        await d.respond("answer it", source="typed")
    d = run(scenario, _Brain(error=RuntimeError("boom")))
    assert _state(d)["state"] == "idle" and _state(d)["blindfoldResult"] == "failed"
    assert "boom" in _state(d)["blindfoldReason"]


def test_a_request_that_takes_too_long_is_stopped_and_ends_red(run, monkeypatch):
    monkeypatch.setattr(daemon, "BLINDFOLD_TIMEOUT", 0.2)
    brain = _Brain(hang=True)

    async def scenario(d):
        await d.command("blindfold on")
        await d.respond("answer it", source="typed")
    d = run(scenario, brain)
    assert brain.interrupted.is_set() and d.tools.cancelled(SESSION)
    assert _state(d)["blindfoldResult"] == "failed" and "timed out" in _state(d)["blindfoldReason"]
    assert d.history[-1]["reply"] == "(timed out)"


class _Heard:
    """A finished listen that heard `text`."""
    held_seconds, error = 2.0, None

    def __init__(self, text="", error=None, held=2.0):
        self.text, self.error, self.held_seconds = text, error, held

    async def finish(self):
        return self.text

    async def cancel(self):
        pass


@pytest.mark.parametrize("session", [
    _Heard(""), _Heard("", error="mic busy"), _Heard("", held=0.1),
])
def test_the_talk_key_without_words_answers_the_question_on_screen(run, _no_notices, session):
    async def scenario(d):
        await d.command("blindfold on")
        d.listening = session
        assert await d.release() == "heard: " + prompt.BLINDFOLD_DEFAULT
        await asyncio.sleep(0)                            # the request runs as its own task
        while d.busy.locked() or d.state != "idle":
            await asyncio.sleep(0.01)
    d = run(scenario)
    assert prompt.BLINDFOLD_DEFAULT in d.brain.asked[-1]
    assert _state(d)["blindfoldResult"] == "ok" and _no_notices == []


def test_without_blindfold_no_words_still_does_nothing(run, _no_notices):
    async def scenario(d):
        d.listening = _Heard("", held=0.1)
        assert await d.release() == "too short"
        d.listening = _Heard("")
        assert await d.release() == "heard nothing"
    d = run(scenario)
    assert d.brain.asked == []


def test_cancel_ends_red_only_when_something_was_running(run):
    async def scenario(d):
        await d.command("blindfold on")
        await d.cancel()                                  # nothing running: the bar stays as it was
        assert _state(d)["blindfoldResult"] is None
        d.set_state("thinking", "answer it")
        await d.cancel()
    d = run(scenario)
    assert _state(d)["blindfoldResult"] == "failed" and _state(d)["blindfoldReason"] == "cancelled"


def test_samples_and_greetings_are_not_said(run, _no_notices):
    async def scenario(d):
        await d.command("blindfold on")
        await d.say("Hi, this is how I sound now.")
        await d.forget()                                  # "Okay, fresh start..." normally
    d = run(scenario)
    assert d.voice.spoken == [] and _no_notices == []


def test_off_it_speaks_and_shows_as_before(run, _no_notices):
    async def scenario(d):
        await d.respond("what time is it", source="typed")
    d = run(scenario)
    assert d.voice.spoken == ["ok"] and _state(d)["blindfoldResult"] is None and _state(d)["blindfold"] is False
    assert any(n[0] == "omarchy-osd" for n in _no_notices)       # "Thinking…"


# --- the brain is told -------------------------------------------------------------------------------

def test_the_blindfold_note_is_sent_only_when_it_is_on():
    on = prompt.user_message("answer it", "time 10:00", level="never", blindfold=True)
    off = prompt.user_message("answer it", "time 10:00", level="never")
    assert "<blindfold>" in on and 'exactly "ok"' in on and "Never paste" in on
    assert "<blindfold>" not in off
    assert on.index("<approvals>") < on.index("<blindfold>") < on.index("Transcript:")
    assert "<blindfold> note" in prompt.CORE                     # it overrides the spoken-reply rules


# --- tools: only the focused window ---------------------------------------------------------------

@pytest.fixture
def rt(tmp_path):
    r = Runtime(tmp_path / "tools.sock")
    r.new_turn(SESSION)
    return r


def _call(rt, name, **args) -> str:
    out = rt.call(SESSION, name, args)
    return out[0]


BLOCKED = ["launch_app", "open_url", "open_path", "focus_window", "switch_workspace", "move_window", "close_window",
           "browser_open", "browser_click", "show_agent", "run_command", "excalidraw_draw", "excalidraw_read",
           "notify", "set_reminder", "screenshot", "volume", "media", "lock_screen"]


@pytest.mark.parametrize("name", BLOCKED)
def test_tools_that_leave_the_window_are_refused_in_blindfold_and_allowed_outside(rt, monkeypatch, name):
    ran = []
    monkeypatch.setitem(tools.TOOLS, name, tools.Tool(lambda **a: ran.append(a) or "ran", False))
    rt.set_blindfold(True)
    out = _call(rt, name, x=1)
    assert out.startswith("error:") and "Blindfold" in out and "Don't ask the user" in out and ran == []
    assert "needs_confirmation" not in out
    rt.set_blindfold(False)
    assert _call(rt, name, x=1) == "ran"


def test_every_tool_is_either_allowed_or_refused_on_purpose():
    assert tools.BLINDFOLD_TOOLS <= set(tools.TOOLS)
    assert not tools.BLINDFOLD_TOOLS & set(BLOCKED)


@pytest.fixture
def desk(monkeypatch):
    """A desktop where FRONT is focused and the left half of the screen is another window."""
    done = []
    monkeypatch.setattr(screen, "active_window", lambda: FRONT)
    monkeypatch.setattr(screen, "key_target", lambda: FRONT)
    monkeypatch.setattr(screen, "window_at", lambda x, y: "0xOTHER" if x < 100 else FRONT)
    monkeypatch.setattr(screen, "window", lambda a: {"class": "chromium", "title": "Quiz"})
    monkeypatch.setattr(screen, "click", lambda *a: done.append(("click", *a)) or "clicked")
    monkeypatch.setattr(screen, "drag", lambda *a: done.append(("drag", *a)) or "dragged")
    monkeypatch.setattr(screen, "scroll", lambda *a: done.append(("scroll", *a)) or "scrolled")
    monkeypatch.setattr(screen, "press_keys", lambda k: done.append(("keys", k)) or f"pressed {k}")
    return done


def test_the_mouse_stays_in_the_focused_window(rt, desk):
    rt.set_blindfold(True)
    for name, args in (("click_at", {"x": 50, "y": 5, "target": "a tab"}), ("scroll_at", {"x": 50, "y": 5}),
                       ("drag_at", {"x1": 500, "y1": 5, "x2": 50, "y2": 5, "target": "a slider"})):
        assert "only in the focused window" in _call(rt, name, **args), name
    for hold in ("super", "ctrl+super", "win"):
        assert "isn't allowed" in _call(rt, "drag_at", x1=500, y1=5, x2=600, y2=5, target="a window", hold=hold)
    assert desk == []
    assert _call(rt, "click_at", x=500, y=5, target="answer B") == "clicked"
    assert _call(rt, "scroll_at", x=500, y=5) == "scrolled"
    rt.set_blindfold(False)
    assert _call(rt, "click_at", x=50, y=5, target="a tab") == "clicked"


def test_a_risky_click_in_the_window_just_runs(rt, desk):
    rt.set_blindfold(True)
    assert _call(rt, "click_at", x=500, y=5, target="Submit button") == "clicked"


@pytest.mark.parametrize("keys", ["super+1", "Super+Return", "alt+tab", "ctrl+alt+tab", "alt+shift+tab", "ctrl+t",
                                  "Control+N", "ctrl+shift+t", "ctrl+tab", "ctrl+pgdn", "ctrl+c", "ctrl+v",
                                  "shift+insert", "ctrl+shift+v", "XF86AudioMute", "Print", "alt+print",
                                  "ctrl+alt+delete", "ctrl+w", "alt+f4", "super"])
def test_keys_that_switch_open_or_use_the_clipboard_are_refused(rt, desk, keys):
    rt.set_blindfold(True)
    out = _call(rt, "press_keys", keys=keys)
    assert out.startswith("error:") and "Blindfold" in out and desk == []


@pytest.mark.parametrize("keys", ["Enter", "Tab", "shift+Enter", "ctrl+a", "BackSpace", "Down", "space"])
def test_keys_inside_the_window_are_allowed(rt, desk, keys):
    rt.set_blindfold(True)
    assert _call(rt, "press_keys", keys=keys) == f"pressed {keys}"


def test_keys_are_refused_when_they_would_go_to_another_window(rt, desk, monkeypatch):
    monkeypatch.setattr(screen, "key_target", lambda: "0xCLICKED_EARLIER")
    rt.set_blindfold(True)
    assert "only in the focused window" in _call(rt, "press_keys", keys="Enter")
    assert "only in the focused window" in _call(rt, "type_text", text="hi")
    assert desk == []


def test_herdr_agents_run_in_the_background(rt, monkeypatch):
    shown = []
    monkeypatch.setattr(herdr, "prompt_agent", lambda *a, show=False: shown.append(show) or json.dumps({"ok": True}))
    monkeypatch.setattr(herdr, "start_agent", lambda *a, show=False: shown.append(show) or json.dumps({"ok": True}))
    rt.set_blindfold(True)
    _call(rt, "prompt_agent", agent="codex", text="what's 2+2")
    _call(rt, "start_agent", task="research it")
    rt.set_blindfold(False)
    _call(rt, "prompt_agent", agent="codex", text="what's 2+2")
    assert shown == [False, False, True]


# --- typing: key by key, never the clipboard ------------------------------------------------------------

class FakeKeys:
    """The virtual keyboard typing into a plain text box: decodes key events with a US keymap, as a
    compositor would, and applies them (cursor, Shift+Home selection, Backspace, Delete). Fails if a key
    is left held."""

    def __init__(self):
        from evdev import ecodes as e
        self.e, self.down, self.text, self.at, self.anchor = e, set(), "", 0, None
        self.chars = {(getattr(e, k), shift): c for c, (k, shift) in screen._US.items()}
        self.chars[(e.KEY_ENTER, True)] = "\n"

    def _cut(self):
        if self.anchor is not None:
            a, b = sorted((self.anchor, self.at))
            self.text, self.at, self.anchor = self.text[:a] + self.text[b:], a, None
            return True
        return False

    def write(self, _type, code, value):
        e = self.e
        if not value:
            self.down.discard(code)
            return
        assert code not in self.down
        self.down.add(code)
        shift = e.KEY_LEFTSHIFT in self.down
        if code == e.KEY_LEFTSHIFT:
            return
        if code == e.KEY_HOME:
            if shift and self.anchor is None:
                self.anchor = self.at
            self.at = self.text.rfind("\n", 0, self.at) + 1
        elif code == e.KEY_LEFT:
            self.at, self.anchor = max(0, self.at - 1), None
        elif code == e.KEY_BACKSPACE:
            if not self._cut() and self.at:
                self.text, self.at = self.text[:self.at - 1] + self.text[self.at:], self.at - 1
        elif code == e.KEY_DELETE:
            if not self._cut():
                self.text = self.text[:self.at] + self.text[self.at + 1:]
        else:
            self._cut()
            c = self.chars[(code, shift)]
            self.text, self.at = self.text[:self.at] + c + self.text[self.at:], self.at + 1

    def syn(self):
        pass


@pytest.fixture
def keyboard(monkeypatch):
    """screen's shell commands, recorded, and the virtual keyboard's typing; the focused window is `app`
    and the layout is `layout`. The clipboard must not be touched."""
    sent, app, layout, kb = [], {"class": "chromium"}, {"kb_layout": "us", "kb_variant": ""}, FakeKeys()

    def sh(cmd, timeout=10):
        if cmd[:2] == ["hyprctl", "activewindow"]:
            return json.dumps({"address": FRONT, **app})
        if cmd[:2] == ["hyprctl", "getoption"]:
            return json.dumps({"str": layout[cmd[2].split(":")[1]]})
        sent.append(cmd)
        return ""

    def no_clipboard(*a, **k):
        raise AssertionError("the clipboard was used")
    monkeypatch.setattr(screen, "_sh", sh)
    monkeypatch.setattr(screen, "_clicked", None)
    monkeypatch.setattr(screen, "_keys", lambda: kb)
    monkeypatch.setattr(screen, "KEY_DELAY_MS", 0)
    for name in ("save", "copy", "restore"):
        monkeypatch.setattr(clipboard, name, no_clipboard)
    return sent, app, layout, kb


def test_type_text_types_real_keys_and_never_pastes(rt, keyboard, monkeypatch):
    sent, _, _, kb = keyboard
    monkeypatch.setattr(screen, "window", lambda a: {"class": "chromium", "title": "Quiz"})
    monkeypatch.setattr(tools, "_warm_judge", lambda: None)
    rt.set_blindfold(True)
    out = _call(rt, "type_text", text="Paris")
    assert "real key presses" in out and "look again" not in out
    assert kb.text == "Paris" and sent == []


def test_every_ascii_character_comes_out_exactly(keyboard):
    """The line Blindfold mangled through wtype (`echox`, lost brackets), and every printable character."""
    _, _, _, kb = keyboard
    line = """read -r x; if echo "$x" | grep -Eqx '[+-]?([0-9]+[.]?[0-9]*|[.][0-9]+)'; then echo ok; fi"""
    every = "".join(chr(i) for i in range(32, 127)) + "\t"
    screen.type_keys(line + "\n" + every)
    assert kb.text == line + "\n" + every and not kb.down


class FakeEditor(FakeKeys):
    """A code editor's habits, like Monaco's: a line break keeps the line's indent (and adds 4 after `{`
    or `:`), and a bracket or quote auto-closes unless a letter follows the cursor."""
    CLOSE = {"(": ")", "[": "]", "{": "}", '"': '"', "'": "'"}

    def write(self, _type, code, value):
        before = self.text
        super().write(_type, code, value)
        if not value or self.text == before or len(self.text) != len(before) + 1:
            return
        c = self.text[self.at - 1]
        if c == "\n":
            prev = before[before.rfind("\n", 0, self.at - 1) + 1:self.at - 1]
            indent = re.match(r" *", prev).group() + ("    " if prev.rstrip().endswith(("{", ":")) else "")
            self.text, self.at = self.text[:self.at] + indent + self.text[self.at:], self.at + len(indent)
        elif c in self.CLOSE and not self.text[self.at:self.at + 1].isalnum():
            self.text = self.text[:self.at] + self.CLOSE[c] + self.text[self.at:]


def test_an_editors_auto_indent_and_auto_close_change_nothing(keyboard, monkeypatch):
    """Without the guards this editor doubles indents and leaves a `)]}"` trail; with them the code is
    exact, typed into a function's empty body too."""
    ed = FakeEditor()
    monkeypatch.setattr(screen, "_keys", lambda: ed)
    code = 'def f(xs):\n    out = {"a": [1, (2, 3)]}\n    for x in xs:\n        print("x")\n\n    return out'
    screen.type_keys(code)
    assert ed.text == code and ed.at == len(code)
    ed.text, ed.at = "script() {\n    \n}", 15
    screen.type_keys('read x; echo "$x"')
    assert ed.text == 'script() {\n    read x; echo "$x"\n}'


def test_plain_text_gets_no_guard_keys(keyboard, monkeypatch):
    _, _, _, kb = keyboard
    pressed = []
    monkeypatch.setattr(screen, "_tap", lambda k, key, shift: pressed.append(key))
    screen.type_keys("ok then")
    assert len(pressed) == len("ok then")


def test_lines_are_typed_with_shift_enter_between_them(keyboard):
    sent, _, _, kb = keyboard
    screen.type_keys("Dear team,\n\nThanks.")
    assert kb.text == "Dear team,\n\nThanks." and sent == []


def test_other_characters_and_layouts_go_through_wtype_and_say_to_look(keyboard):
    sent, _, layout, kb = keyboard
    out = screen.type_keys("café ok")
    assert kb.text == "caf ok" and sent == [["wtype", "-d", "0", "--", "é"]] and "look again" in out
    sent.clear(); layout["kb_layout"] = "de"
    out = screen.type_keys("Hallo")
    assert sent == [["wtype", "-d", "0", "--", "Hallo"]] and "layout" in out


def test_several_lines_fail_in_a_terminal(keyboard):
    sent, app, _, kb = keyboard
    app["class"] = "Alacritty"
    with pytest.raises(screen.ScreenError, match="one line at a time"):
        screen.type_keys("ls\nrm -r x")
    assert kb.text == ""
    screen.type_keys("ls -la")                    # one line is fine (nothing presses Enter)
    assert kb.text == "ls -la"
