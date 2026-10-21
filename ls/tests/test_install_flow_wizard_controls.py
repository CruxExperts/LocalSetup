from __future__ import annotations

from ls.tests.test_install_flow import *

def test_wizard_selection_helpers_accept_numbers_and_back_cancel() -> None:
    term = TerminalWizard(
        input_stream=io.StringIO("2\nb\nq\n"),
        output_stream=io.StringIO(),
        color=False,
    )

    assert choose_one(term, "Mode", [("global", "Global"), ("current", "Current")], default="global") == "current"
    assert choose_many(term, "Platforms", [("codex", "Codex")], default=["codex"]) == "__back__"
    assert choose_one(term, "Mode", [("global", "Global")], default="global") == "__cancel__"


def test_wizard_prompt_returns_cancel_on_keyboard_interrupt() -> None:
    output = io.StringIO()
    term = TerminalWizard(
        input_stream=KeyboardInterruptStream(),
        output_stream=output,
        color=False,
    )

    assert term.prompt("Mode") == "__cancel__"
    assert output.getvalue().endswith("\n")


def test_wizard_color_policy_honors_tty_env_and_explicit_modes(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.delenv("FORCE_COLOR", raising=False)
    monkeypatch.setenv("TERM", "xterm-256color")

    assert TerminalWizard(io.StringIO(), FakeTtyStringIO(), color_mode="auto").color is True
    assert TerminalWizard(io.StringIO(), io.StringIO(), color_mode="auto").color is False

    monkeypatch.setenv("NO_COLOR", "1")
    assert TerminalWizard(io.StringIO(), FakeTtyStringIO(), color_mode="auto").color is False
    assert TerminalWizard(io.StringIO(), FakeTtyStringIO(), color_mode="always").color is True

    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.setenv("FORCE_COLOR", "1")
    assert TerminalWizard(io.StringIO(), io.StringIO(), color_mode="auto").color is True
    assert TerminalWizard(io.StringIO(), FakeTtyStringIO(), color_mode="never").color is False

    monkeypatch.delenv("FORCE_COLOR", raising=False)
    monkeypatch.setenv("TERM", "dumb")
    assert TerminalWizard(io.StringIO(), FakeTtyStringIO(), color_mode="auto").color is False


def test_wizard_glyph_policy_uses_ascii_for_scripted_or_ascii_terminals(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TERM", "xterm-256color")

    legacy_plain = TerminalWizard(io.StringIO(), FakeTtyStringIO(), color=False)
    assert legacy_plain.glyph("ok") == "[OK]"

    scripted = TerminalWizard(io.StringIO(), io.StringIO(), glyph_mode="auto")
    assert scripted.glyph("ok") == "[OK]"

    ascii_tty = TerminalWizard(io.StringIO(), FakeAsciiTtyStringIO(), glyph_mode="auto")
    assert ascii_tty.glyph("suggested") == "[SUGGESTED]"

    unicode_forced = TerminalWizard(io.StringIO(), io.StringIO(), glyph_mode="unicode")
    assert unicode_forced.glyph("ok").startswith("[OK]")
    assert unicode_forced.glyph("ok") != "[OK]"

    ascii_forced = TerminalWizard(io.StringIO(), FakeTtyStringIO(), glyph_mode="ascii")
    assert ascii_forced.glyph("fail") == "[FAIL]"


def test_wizard_semantic_renderer_wraps_paths_and_keeps_text_labels() -> None:
    output = io.StringIO()
    term = TerminalWizard(
        input_stream=io.StringIO(),
        output_stream=output,
        color=False,
        glyph_mode="ascii",
    )

    term.step_header("Platforms", progress="Step 3/7")
    term.key_value_block([("Long path", "/tmp/" + "nested/" * 16 + "repo")])
    term.status_line("ok", "LocalSetup installed successfully.")
    term.status_line("warn", "Manual dependency setup may still be needed.")
    term.status_line("fail", "A blocker prevents apply.")
    term.action_list(["Attach selected adapter: /tmp/" + "nested/" * 12 + ".codex/skills"])
    term.diagnostic_command(["python3", "/tmp/" + "nested/" * 12 + "localsetup.py", "doctor"])

    rendered = output.getvalue()
    assert "Step 3/7 - Platforms" in rendered
    assert "Long path:" in rendered
    assert "[OK] LocalSetup installed successfully." in rendered
    assert "[WARN] Manual dependency setup may still be needed." in rendered
    assert "[FAIL] A blocker prevents apply." in rendered
    assert "[PLAN] Attach selected adapter:" in rendered
    assert "Diagnostic command:" in rendered
    assert "python3" in rendered


def test_wizard_choice_detail_mode_renders_extended_context() -> None:
    output = io.StringIO()
    term = TerminalWizard(
        input_stream=io.StringIO("1\n"),
        output_stream=output,
        color=False,
    )
    choice = Choice(
        "global",
        "Global library only",
        "Safest default.",
        "Updates the managed skill library.",
        "You want a low-risk install.",
        "No repo adapter paths are created.",
    )

    assert choose_one(term, "Mode", [choice], default="global", decides="Install scope.") == "global"
    rendered = output.getvalue()
    assert "Decides: Install scope." in rendered
    assert "Safest default." in rendered
    assert "Does: Updates the managed skill library." in rendered
    assert "Choose when: You want a low-risk install." in rendered
    assert "Tradeoff: No repo adapter paths are created." in rendered
    assert "Enter number(s) | d details | b back | q quit | ? help" in rendered


def test_wizard_choice_compact_mode_hides_extended_reasoning() -> None:
    output = io.StringIO()
    term = TerminalWizard(
        input_stream=io.StringIO("1\n"),
        output_stream=output,
        color=False,
    )
    term.detail_mode = False
    choice = Choice(
        "core",
        "core",
        "Everyday skills.",
        "Installs the core pack.",
        "You want normal use.",
        "Specialized packs stay out.",
    )

    assert choose_many(term, "Packs", [choice], default=["core"], allow_none=False) == ["core"]
    rendered = output.getvalue()
    assert "Everyday skills." in rendered
    assert "Does: Installs the core pack." not in rendered
    assert "Choose when: You want normal use." not in rendered
    assert "Tradeoff: Specialized packs stay out." not in rendered
    assert "Enter number(s) | d details | b back | q quit | ? help" in rendered


def test_wizard_detail_toggle_rerenders_choices() -> None:
    output = io.StringIO()
    term = TerminalWizard(
        input_stream=io.StringIO("d\n1\n"),
        output_stream=output,
        color=False,
    )
    choice = Choice(
        "symlink",
        "Symlink adapters",
        "Points at managed skills.",
        "Creates repo adapter symlinks.",
        "You want easy updates.",
        "Requires the managed library path.",
    )

    assert choose_one(term, "Adapter mode", [choice], default="symlink") == "symlink"
    rendered = output.getvalue()
    assert "Detail mode: compact." in rendered
    assert rendered.count("Points at managed skills.") == 2
    assert rendered.count("Does: Creates repo adapter symlinks.") == 1
    assert term.detail_mode is False


def test_wizard_help_prints_without_selecting() -> None:
    output = io.StringIO()
    term = TerminalWizard(
        input_stream=io.StringIO("?\n2\n"),
        output_stream=output,
        color=False,
    )

    result = choose_one(
        term,
        "Mode",
        [("global", "Global"), ("current", "Current")],
        default="global",
        help_text="Pick the install scope.",
    )

    assert result == "current"
    rendered = output.getvalue()
    assert "Pick the install scope." in rendered
    assert rendered.count("1. Global") == 2


def test_wizard_selection_helpers_accept_labels_and_comma_lists() -> None:
    term = TerminalWizard(
        input_stream=io.StringIO("Current\ncodex,cursor\nClaude Code\n"),
        output_stream=io.StringIO(),
        color=False,
    )

    assert choose_one(term, "Mode", [("global", "Global"), ("current", "Current")], default="global") == "current"
    assert choose_many(
        term,
        "Platforms",
        [("codex", "Codex"), ("cursor", "Cursor")],
        default=["codex"],
    ) == ["codex", "cursor"]
    assert choose_many(
        term,
        "Platforms",
        [("codex", "Codex"), ("claude-code", "Claude Code")],
        default=["codex"],
    ) == ["claude-code"]


def test_wizard_checkbox_falls_back_to_line_mode_for_scripted_streams() -> None:
    term = TerminalWizard(
        input_stream=io.StringIO("1,2\n"),
        output_stream=io.StringIO(),
        color=False,
    )

    assert choose_many_checkbox(
        term,
        "Skills",
        [("ls-localsetup-project-context", "ls-localsetup-project-context"), ("ls-test-runner", "ls-test-runner")],
        default=["ls-localsetup-project-context"],
    ) == ["ls-localsetup-project-context", "ls-test-runner"]


@pytest.mark.parametrize(
    ("input_text", "expected"),
    [
        ("\x1b[A", "up"),
        ("\x1b[B", "down"),
        ("\x1bOA", "up"),
        ("\x1bOB", "down"),
        ("\x1b[1;5A", "up"),
        ("\x1b[1;5B", "down"),
    ],
)
def test_wizard_read_key_recognizes_arrow_sequences(
    monkeypatch: pytest.MonkeyPatch, input_text: str, expected: str
) -> None:
    key_input = FakeKeyInput(input_text)
    patch_fake_key_input(monkeypatch, key_input)
    term = TerminalWizard(input_stream=key_input, output_stream=io.StringIO(), color=False)

    assert wizard._read_key(term) == expected


@pytest.mark.parametrize("input_text", ["\x1b", "\x1b[", "\x1b[1;5", "\x1bOC"])
def test_wizard_read_key_treats_incomplete_or_unsupported_escape_as_unknown(
    monkeypatch: pytest.MonkeyPatch, input_text: str
) -> None:
    key_input = FakeKeyInput(input_text)
    patch_fake_key_input(monkeypatch, key_input)
    term = TerminalWizard(input_stream=key_input, output_stream=io.StringIO(), color=False)

    assert wizard._read_key(term) == "unknown"


def test_wizard_read_key_recognizes_ctrl_c() -> None:
    key_input = FakeKeyInput("\x03")
    term = TerminalWizard(input_stream=key_input, output_stream=io.StringIO(), color=False)

    assert wizard._read_key(term) == "ctrl-c"


def test_wizard_checkbox_unknown_printable_key_is_noop(monkeypatch: pytest.MonkeyPatch) -> None:
    key_input = FakeKeyInput("x\n")
    enable_checkbox_key_mode(monkeypatch, key_input)
    term = TerminalWizard(input_stream=key_input, output_stream=io.StringIO(), color=False)

    assert choose_many_checkbox(
        term,
        "Skills",
        [("ls-localsetup-project-context", "ls-localsetup-project-context"), ("ls-test-runner", "ls-test-runner")],
        default=["ls-localsetup-project-context"],
    ) == ["ls-localsetup-project-context"]


def test_wizard_checkbox_application_cursor_arrows_move_selection(monkeypatch: pytest.MonkeyPatch) -> None:
    key_input = FakeKeyInput("\x1bOB\x1bOA \n")
    enable_checkbox_key_mode(monkeypatch, key_input)
    term = TerminalWizard(input_stream=key_input, output_stream=io.StringIO(), color=False)

    assert choose_many_checkbox(
        term,
        "Skills",
        [("ls-localsetup-project-context", "ls-localsetup-project-context"), ("ls-test-runner", "ls-test-runner")],
        default=[],
    ) == ["ls-localsetup-project-context"]


def test_wizard_checkbox_application_cursor_down_selects_next_item(monkeypatch: pytest.MonkeyPatch) -> None:
    key_input = FakeKeyInput("\x1bOB \n")
    enable_checkbox_key_mode(monkeypatch, key_input)
    term = TerminalWizard(input_stream=key_input, output_stream=io.StringIO(), color=False)

    assert choose_many_checkbox(
        term,
        "Skills",
        [("ls-localsetup-project-context", "ls-localsetup-project-context"), ("ls-test-runner", "ls-test-runner")],
        default=[],
    ) == ["ls-test-runner"]


def test_wizard_checkbox_q_still_cancels(monkeypatch: pytest.MonkeyPatch) -> None:
    key_input = FakeKeyInput("q")
    enable_checkbox_key_mode(monkeypatch, key_input)
    term = TerminalWizard(input_stream=key_input, output_stream=io.StringIO(), color=False)

    assert (
        choose_many_checkbox(term, "Skills", [("ls-localsetup-project-context", "ls-localsetup-project-context")], default=["ls-localsetup-project-context"])
        == wizard.CANCEL
    )


def test_wizard_checkbox_ctrl_c_cancels(monkeypatch: pytest.MonkeyPatch) -> None:
    key_input = FakeKeyInput("\x03")
    enable_checkbox_key_mode(monkeypatch, key_input)
    term = TerminalWizard(input_stream=key_input, output_stream=io.StringIO(), color=False)

    assert (
        choose_many_checkbox(term, "Skills", [("ls-localsetup-project-context", "ls-localsetup-project-context")], default=["ls-localsetup-project-context"])
        == wizard.CANCEL
    )
