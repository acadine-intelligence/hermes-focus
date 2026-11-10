import importlib.util
import json
import sys
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "demo"))

from hermes_focus import engine, render, sources  # noqa: E402
from hermes_focus.service import FocusService, parse_deadline  # noqa: E402
from seed import seed  # noqa: E402

NOW = 1_800_000_000.0


class Clock:
    def __init__(self, t=NOW):
        self.t = t

    def __call__(self):
        return self.t


@pytest.fixture
def svc(tmp_path):
    home = seed(tmp_path / "home", now=NOW)
    clock = Clock()
    s = FocusService(home, tmp_path / "data", clock=clock)
    s.clock_ref = clock
    return s


# ---- sources -----------------------------------------------------------------------------------

def test_collect_reads_only_interactive_open_work(tmp_path):
    home = seed(tmp_path / "h", now=NOW)
    sig, errors = sources.collect(home, now=NOW, exclude_session="s_live")
    assert errors == []
    refs = {(s["source"], s["ref"]) for s in sig}
    assert ("kanban", "t_e5") not in refs  # done
    assert ("session", "s_cron") not in refs and ("session", "s_sub") not in refs
    assert ("session", "s_live") not in refs  # the current chat
    assert ("cron", "j_bad") in refs and ("cron", "j_ok") not in refs and ("cron", "j_off") not in refs
    q = next(s for s in sig if s["ref"] == "s_q")
    assert q["waiting_on_user"] is True
    assert next(s for s in sig if s["ref"] == "s_r")["waiting_on_user"] is False
    assert next(s for s in sig if s["ref"] == "t_a1")["unlocks"] == 2


def test_missing_home_is_empty_not_an_error(tmp_path):
    sig, errors = sources.collect(tmp_path / "nothing", now=NOW)
    assert sig == [] and errors == []


def test_unknown_schema_reports_and_continues(tmp_path):
    import sqlite3
    home = tmp_path / "h"
    home.mkdir()
    sqlite3.connect(home / "kanban.db").execute("CREATE TABLE tasks (x TEXT)").connection.commit()
    (home / "cron").mkdir()
    (home / "cron" / "jobs.json").write_text("{not json")
    sig, errors = sources.collect(home, now=NOW)
    assert sig == []
    assert any(e.startswith("kanban") for e in errors) and any(e.startswith("cron") for e in errors)


@pytest.mark.parametrize("text,expected", [
    ("Which one do you want?", True),
    ("Shall we ship it? **", True),
    ("Decisions for you:\n1. pick a name", True),
    ("Deployed and verified.", False),
    ("", False),
])
def test_waiting_on_user(text, expected):
    assert sources.waiting_on_user(text) is expected


# ---- engine ------------------------------------------------------------------------------------

def test_gate_order_beats_score(svc):
    d = svc.now(exclude_session="s_live")
    assert d["mode"] == "proposed"
    # Someone waiting on you beats a failing cron, which beats in-flight work and backlog.
    assert d["focus"]["gate"] == 1
    assert d["focus"]["id"] == "kanban:t_a1"  # waiting, priority 150, unblocks two tasks
    gates = [d["focus"]["gate"]] + [c["gate"] for c in d["next"]]
    assert gates == sorted(gates)


def test_deterministic(svc):
    a = svc.now(exclude_session="s_live")
    b = svc.now(exclude_session="s_live")
    assert a["focus"] == b["focus"] and a["next"] == b["next"]


def test_commitment_due_today_outranks_everything(svc):
    svc.add("Send the investor update", next_action="Paste numbers into the draft", deadline="+3h")
    d = svc.now(exclude_session="s_live")
    assert d["focus"]["source"] == "commitment" and d["focus"]["gate"] == 0
    assert "due in 3 h" in d["focus"]["evidence"]


def test_commitment_without_deadline_waits_its_turn(svc):
    svc.add("Someday learn Rust")
    d = svc.now(exclude_session="s_live")
    assert d["focus"]["source"] != "commitment"


# ---- lease, churn guard, interrupts ----------------------------------------------------------

def test_lease_holds_against_routine_reranking(svc):
    r = svc.accept("kanban:t_c3", minutes=60)  # in-flight work, gate 3
    assert r["focus"]["status"] == "active"
    svc.clock_ref.t += 10 * 60
    d = svc.now(exclude_session="s_live")
    assert d["mode"] == "leased" and d["focus"]["id"] == "kanban:t_c3"
    assert d["lease_left_min"] == 50
    # Gate-1 work exists, so it is offered as an interruption, never swapped in silently.
    assert d["interrupt"]["gate"] == 1


def test_no_interrupt_for_equal_or_lower_urgency(svc):
    svc.accept("kanban:t_a1")  # gate 1
    d = svc.now(exclude_session="s_live")
    assert d["interrupt"] is None  # s_q is gate 1 too: not strictly more urgent


def test_switching_records_override(svc):
    svc.accept("kanban:t_c3")
    r = svc.accept("1", reason="pricing is blocking two tasks")
    assert r["replaced"]["id"] == "kanban:t_c3"
    assert svc.store.read()["history"][-1]["status"] == "overridden"


def test_lease_expiry(svc):
    svc.accept(minutes=30)
    svc.clock_ref.t += 31 * 60
    d = svc.now(exclude_session="s_live")
    assert d["mode"] == "proposed"
    assert svc.store.read()["history"][-1]["status"] == "expired"


def test_done_needs_proof_and_does_not_bounce_back(svc):
    svc.accept("session:s_q")
    with pytest.raises(ValueError):
        svc.done("ok")
    r = svc.done("Awaited the cart; 50/50 green runs")
    assert r["completed"]["status"] == "completed"
    shown = [r["next"]["focus"]["id"]] + [c["id"] for c in r["next"]["next"]]
    assert "session:s_q" not in shown  # chat still ends with "?", but it is done


def test_kanban_completion_closes_focus(svc):
    svc.accept("kanban:t_c3")
    assert svc.kanban_completed("t_other") is False
    assert svc.kanban_completed("t_c3", "webhooks live, 0 errors in 1 h") is True
    h = svc.store.read()["history"][-1]
    assert h["status"] == "completed" and "0 errors" in h["proof"]


def test_kanban_task_leaving_open_list_reconciles(svc, tmp_path):
    import sqlite3
    svc.accept("kanban:t_c3")
    c = sqlite3.connect(svc.home / "kanban.db")
    c.execute("UPDATE tasks SET status='done' WHERE id='t_c3'")
    c.commit()
    c.close()
    d = svc.now(exclude_session="s_live")
    assert d["mode"] == "proposed"
    assert svc.store.read()["history"][-1]["status"] == "completed"


def test_defer_hides_candidate_until_time(svc):
    top = svc.now(exclude_session="s_live")["focus"]["id"]
    svc.defer(top, hours=2)
    assert svc.now(exclude_session="s_live")["focus"]["id"] != top
    svc.clock_ref.t += 2 * 3600 + 1
    assert svc.now(exclude_session="s_live")["focus"]["id"] == top


# ---- drift guard -----------------------------------------------------------------------------

def test_drift_nudges_once_then_cools_down(svc):
    svc.accept("kanban:t_c3", session_id="s_live")
    on = svc.on_turn("migrate the billing webhooks handler to the v2 signature", session_id="s_other")
    assert on is None
    note = svc.on_turn("can you research the best espresso machines under 500 dollars", session_id="s_other")
    assert note and "Migrate billing webhooks" in note
    again = svc.on_turn("and compare grinders for espresso at home please", session_id="s_other")
    assert again is None  # cooldown
    short = svc.on_turn("yes", session_id="s_other")
    assert short is None
    today = svc.now(exclude_session="s_live")["today"]
    assert today["drift"] == 2 and today["nudges"] == 1 and today["on_focus"] == 2


def test_anchored_session_never_drifts(svc):
    svc.accept("kanban:t_c3", session_id="s_live")
    assert svc.on_turn("totally unrelated question about espresso machines today", session_id="s_live") is None


def test_no_focus_no_drift(svc):
    assert svc.on_turn("anything at all goes here with many words", session_id="x") is None


def test_review(svc):
    svc.accept("session:s_q")
    svc.clock_ref.t += 40 * 60
    svc.done("merged PR with fixture fix")
    svc.on_turn("research espresso machines under five hundred", session_id="z")
    r = svc.review(7)
    assert r["completed"] == 1 and r["focus_minutes"] == 40
    assert "1 completed" in render.review(r)


# ---- misc --------------------------------------------------------------------------------------

def test_parse_deadline():
    assert parse_deadline("+3h", NOW) == NOW + 3 * 3600
    assert parse_deadline("+2d", NOW) == NOW + 2 * 86400
    assert parse_deadline(None, NOW) is None
    assert parse_deadline("2026-11-11 17:00", NOW) > 0
    with pytest.raises(ValueError):
        parse_deadline("whenever", NOW)


def test_corrupt_state_recovers(svc):
    svc.store.dir.mkdir(parents=True, exist_ok=True)
    svc.store.path.write_text("{broken")
    assert svc.now(exclude_session="s_live")["mode"] == "proposed"
    assert (svc.store.dir / "focus.corrupt.json").exists()


def test_card_renders_every_mode(svc):
    assert "PROPOSED" in render.card(svc.now(exclude_session="s_live"))
    svc.accept()
    assert "FOCUS" in render.card(svc.now(exclude_session="s_live"))
    assert "Nothing open" in render.card({"mode": "empty", "focus": None})


# ---- Hermes adapter (fake ctx) ---------------------------------------------------------------

class FakeState:
    def __init__(self, d):
        self.data_dir = d


class FakeCtx:
    def __init__(self, data_dir):
        self.tools, self.hooks, self.commands, self.cli, self.sections = {}, {}, {}, {}, {}
        self.state = FakeState(data_dir)
        self.config = {}

    def register_tool(self, name, toolset, schema, handler, **kw):
        assert schema["name"] == name
        self.tools[name] = handler

    def register_hook(self, name, cb):
        self.hooks[name] = cb

    def register_command(self, name, handler, **kw):
        self.commands[name] = handler

    def register_cli_command(self, name, help, setup_fn, handler_fn=None, description=""):
        self.cli[name] = (setup_fn, handler_fn)

    def register_system_prompt_section(self, id, content, **kw):
        self.sections[id] = content

    def get_config(self, key, default=None):
        return self.config.get(key, default)

    def current_cron_execution(self):
        return None


@pytest.fixture
def plugin(tmp_path, monkeypatch):
    home = seed(tmp_path / "home")
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.delenv("HERMES_KANBAN_DB", raising=False)
    spec = importlib.util.spec_from_file_location("focus_plugin", ROOT / "__init__.py",
                                                  submodule_search_locations=[str(ROOT)])
    mod = importlib.util.module_from_spec(spec)
    sys.modules["focus_plugin"] = mod
    spec.loader.exec_module(mod)
    monkeypatch.setattr(mod.host, "hermes_home", lambda: home)
    monkeypatch.setattr(mod.host, "data_dir", lambda: tmp_path / "data")
    ctx = FakeCtx(tmp_path / "data")
    mod.register(ctx)
    return mod, ctx


def test_register_surface(plugin):
    mod, ctx = plugin
    assert set(ctx.tools) == {"focus_now", "focus_accept", "focus_done", "focus_defer", "focus_add"}
    assert set(ctx.hooks) == {"pre_llm_call", "kanban_task_completed"}
    assert "now" in ctx.commands and "focus" in ctx.cli and "focus.current" in ctx.sections


def test_tools_round_trip(plugin):
    mod, ctx = plugin
    now = json.loads(ctx.tools["focus_now"]({}, session_id="s_live"))
    assert now["success"] and now["decision"]["mode"] == "proposed"
    acc = json.loads(ctx.tools["focus_accept"]({}, session_id="s_live"))
    assert acc["success"]
    assert "leased focus" in ctx.sections["focus.current"]({})
    bad = json.loads(ctx.tools["focus_done"]({"proof": ""}))
    assert bad["success"] is False
    done = json.loads(ctx.tools["focus_done"]({"proof": "pricing copy approved in thread"}))
    assert done["success"] and "PROPOSED" in done["next_card"]


def test_slash_command(plugin):
    mod, ctx = plugin
    h = ctx.commands["now"]
    assert "PROPOSED" in h("", session_id="s_live")
    assert "Leased until" in h("accept 2 45", session_id="s_live")
    assert "FOCUS" in h("", session_id="s_live")
    assert "Added" in h('add "Send invoice to Acme" by tomorrow', session_id="s_live")
    assert "Done:" in h("done invoice sent, copy in Sent folder", session_id="s_live")
    assert "Last 7 days" in h("review")
    assert "/now accept" in h("bogus")


def test_hook_skips_unattended_and_subagents(plugin):
    mod, ctx = plugin
    ctx.tools["focus_accept"]({"candidate": "kanban:t_c3"}, session_id="s_live")
    msg = "research the best espresso machines under five hundred dollars"
    assert ctx.hooks["pre_llm_call"](session_id="a", user_message=msg, platform="cron") is None
    assert ctx.hooks["pre_llm_call"](session_id="a", user_message=msg, platform="cli", parent_session_id="p") is None
    out = ctx.hooks["pre_llm_call"](session_id="a", user_message=msg, platform="cli")
    assert out and "[Focus]" in out["context"]
    ctx.config["drift_guard"] = False
    assert ctx.hooks["pre_llm_call"](session_id="b", user_message=msg, platform="cli") is None


def test_hook_never_raises(plugin, monkeypatch):
    mod, ctx = plugin
    monkeypatch.setattr(mod, "service", lambda: (_ for _ in ()).throw(RuntimeError("boom")))
    assert ctx.hooks["pre_llm_call"](session_id="a", user_message="x " * 20, platform="cli") is None
    assert ctx.hooks["kanban_task_completed"](task_id="t") is None
