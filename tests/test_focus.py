import importlib.util
import json
import sys
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "demo"))

from hermes_focus import engine, judge, render, render_html, sources  # noqa: E402
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
    assert set(ctx.tools) == {"focus_now", "focus_accept", "focus_done", "focus_defer", "focus_add",
                              "focus_rank", "focus_pin"}
    assert set(ctx.hooks) == {"pre_llm_call", "kanban_task_completed"}
    assert "now" in ctx.commands and "focus" in ctx.cli and "focus.current" in ctx.sections


def test_tools_round_trip(plugin):
    mod, ctx = plugin
    now = json.loads(ctx.tools["focus_now"]({}, session_id="s_live"))
    assert now["success"] and now["decision"]["mode"] == "proposed"
    acc = json.loads(ctx.tools["focus_accept"]({}, session_id="s_live"))
    assert acc["success"]
    assert ctx.sections["focus.current"]({}) == ""       # quiet by default
    ctx.config["drift_guard"] = True
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
    ctx.config["drift_guard"] = True
    out = ctx.hooks["pre_llm_call"](session_id="a", user_message=msg, platform="cli")
    assert out and "[Focus]" in out["context"]
    ctx.config["drift_guard"] = False
    assert ctx.hooks["pre_llm_call"](session_id="b", user_message=msg, platform="cli") is None


def test_focus_is_quiet_by_default_and_still_counts_drift(plugin):
    """Out of the box nothing reaches the model: no drift note, no prompt section. The turn is
    still counted so the pane can show drift without interrupting any chat."""
    mod, ctx = plugin
    assert "drift_guard" not in ctx.config
    ctx.tools["focus_accept"]({"candidate": "kanban:t_c3"}, session_id="s_live")
    msg = "research the best espresso machines under five hundred dollars"
    for sid in ("a", "b", "c"):
        assert ctx.hooks["pre_llm_call"](session_id=sid, user_message=msg, platform="desktop") is None
    assert ctx.sections["focus.current"]({}) == ""
    today = mod.service().now()["today"]
    assert today["drift"] == 3 and "nudges" not in today
    assert mod.service().store.read()["nudged"] == {}


def test_hook_never_raises(plugin, monkeypatch):
    mod, ctx = plugin
    monkeypatch.setattr(mod, "service", lambda: (_ for _ in ()).throw(RuntimeError("boom")))
    assert ctx.hooks["pre_llm_call"](session_id="a", user_message="x " * 20, platform="cli") is None
    assert ctx.hooks["kanban_task_completed"](task_id="t") is None


# ---- prioritisation: pins and the explained ranking ---------------------------------------------

def test_pin_lifts_backlog_above_inferred_work_but_not_above_a_deadline(svc):
    before = svc.rank(limit=20)["ladder"]
    blog = next(c for c in before if c["ref"] == "t_d4")
    assert blog["gate"] == 4 and blog["n"] > 3
    svc.pin("kanban:t_d4")
    after = svc.rank(limit=20)["ladder"]
    assert after[0]["ref"] == "t_d4" and after[0]["pinned"] and after[0]["evidence"][0] == "you pinned this"
    svc.add("File the VAT return", deadline="+3h")
    assert svc.rank()["ladder"][0]["title"] == "File the VAT return"  # due inside 24 h still wins
    svc.pin("kanban:t_d4", on=False)
    assert next(c for c in svc.rank(limit=20)["ladder"] if c["ref"] == "t_d4")["gate"] == 4


def test_completing_a_pinned_focus_clears_the_pin(svc):
    svc.pin("kanban:t_d4")
    svc.accept("kanban:t_d4")
    svc.done("blog post published, link in the launch thread")
    assert svc.store.read()["pinned"] == {}


def test_rank_explains_every_position(svc):
    r = svc.rank()
    assert r["total"] == len(r["ladder"]) and sum(r["by_gate"].values()) == r["total"]
    top = r["ladder"][0]
    assert top["n"] == 1 and top["contributions"] and top["done_when"]
    assert abs(sum(v for _, v in top["contributions"]) - top["score"]) < 0.11
    text = render.why(r)
    assert "RANKING" in text and "someone is waiting on you" in text and "decision model: off" in text
    assert "due within 24 hours > someone is waiting on you" in text


def test_card_and_prompt_carry_done_when(svc):
    assert "done   when" in render.card(svc.now())
    svc.accept()
    assert "Done when:" in svc.prompt_section()


# ---- optional decision model --------------------------------------------------------------------

ANSWERS = {
    "s_opt": {"human_needed": 0.04, "optional_only": 0.95, "open_commitment": 0.06},
    "s_blk": {"human_needed": 0.96, "optional_only": 0.02, "open_commitment": 0.93},
    "s_q": {"human_needed": 0.9, "optional_only": 0.05, "open_commitment": 0.88},
    "s_r": {"human_needed": 0.5, "optional_only": 0.1, "open_commitment": 0.6},
}


def _fake_ask(calls, answers=ANSWERS, shape="plain"):
    by_title = {"Summarise the Q3 churn report": "s_opt", "Rotate the staging database password": "s_blk",
                "Fix flaky checkout test": "s_q", "Refactor search ranking": "s_r"}

    def ask(state, questions):
        calls.append(state["chat_title"])
        assert set(questions) == set(judge.QUESTIONS)
        a = answers[by_title[state["chat_title"]]]
        return {k: {"noul": v} for k, v in a.items()} if shape == "systemone" and isinstance(a, dict) else a

    return ask


def _judged_svc(tmp_path, ask, **kw):
    home = seed(tmp_path / "home", now=NOW, extras=True)
    j = judge.Judge("fake", ask, tmp_path / "data" / "judge-cache.json", **kw)
    return FocusService(home, tmp_path / "data", clock=Clock(), judge=j, judge_status="fake")


def _order(svc):
    return [(c["ref"], c["gate"]) for c in svc.rank(limit=30)["ladder"]]


def test_decision_model_is_off_by_default_and_rules_cannot_tell_the_two_chats_apart(tmp_path):
    home = seed(tmp_path / "home", now=NOW, extras=True)
    svc = FocusService(home, tmp_path / "data", clock=Clock())
    gates = dict(_order(svc))
    assert gates["s_opt"] == 1 and gates["s_blk"] == 3  # the rules get both wrong
    assert svc.now()["decision_model"] == {"backend": "off", "on": False}
    assert not (tmp_path / "data" / "judge-cache.json").exists()


def test_decision_model_moves_chats_by_policy_in_code(tmp_path):
    calls = []
    svc = _judged_svc(tmp_path, _fake_ask(calls, shape="systemone"))
    gates = dict(_order(svc))
    assert gates["s_opt"] == 4      # finished, only an optional offer: backlog, not "waiting on you"
    assert gates["s_blk"] == 1      # blocked on an approval the regex missed
    assert gates["s_q"] == 1        # agreed with the rules
    assert gates["s_r"] == 3        # undecided band: the rules stand
    blk = next(c for c in svc.rank(limit=30)["ladder"] if c["ref"] == "s_blk")
    assert any("decision model" in e for e in blk["evidence"]) and blk["judged"]["human_needed"] == 0.96
    info = svc.now()["decision_model"]
    assert info["on"] and info["backend"] == "fake" and info["errors"] == []


def test_decision_model_answers_are_cached_so_the_same_state_gives_the_same_answer(tmp_path):
    calls = []
    svc = _judged_svc(tmp_path, _fake_ask(calls))
    first = _order(svc)
    n = len(calls)
    assert n == 4
    assert _order(svc) == first and len(calls) == n          # second pass: cache only
    info = svc.now()["decision_model"]
    assert info["cached"] == 4 and info["judged"] == 0
    other = _judged_svc(tmp_path, lambda *_: (_ for _ in ()).throw(RuntimeError("offline")))
    assert _order(other) == first                            # a dead backend still reads the cache


POISON = [None, True, False, float("nan"), float("inf"), 1e309, -0.1, 1.1, "0.9", [], {}, {"noul": "x"},
          {"noul": None}, {"noul": True}]


@pytest.mark.parametrize("bad", POISON)
def test_a_poisoned_answer_changes_nothing(tmp_path, bad):
    plain = _order(FocusService(seed(tmp_path / "p", now=NOW, extras=True), tmp_path / "pd", clock=Clock()))
    poisoned = {k: {**v, "human_needed": bad} for k, v in ANSWERS.items()}
    svc = _judged_svc(tmp_path, _fake_ask([], answers=poisoned))
    assert _order(svc) == plain
    assert svc.now()["decision_model"]["errors"] == ["unusable answer"]
    whole = _judged_svc(tmp_path / "w", _fake_ask([], answers={k: bad for k in ANSWERS}))
    assert _order(whole) == plain


def test_a_missing_answer_changes_nothing(tmp_path):
    plain = _order(FocusService(seed(tmp_path / "p", now=NOW, extras=True), tmp_path / "pd", clock=Clock()))
    partial = {k: {"human_needed": 0.99} for k in ANSWERS}
    assert _order(_judged_svc(tmp_path, _fake_ask([], answers=partial))) == plain


def test_a_failing_or_slow_backend_never_raises_and_never_waits_past_its_budget(tmp_path):
    import time as _time
    plain = _order(FocusService(seed(tmp_path / "p", now=NOW, extras=True), tmp_path / "pd", clock=Clock()))

    def boom(state, questions):
        raise OSError("connection refused")

    svc = _judged_svc(tmp_path, boom)
    assert _order(svc) == plain
    assert "OSError" in svc.now()["decision_model"]["errors"][0]

    def slow(state, questions):
        _time.sleep(1.5)
        return ANSWERS["s_q"]

    svc = _judged_svc(tmp_path / "s", slow, budget_s=0.2)
    t0 = _time.perf_counter()
    assert _order(svc) == plain
    assert _time.perf_counter() - t0 < 1.0
    assert "budget" in " ".join(svc.judge_info["errors"])


def test_judge_caps_new_calls_per_refresh(tmp_path):
    calls = []
    svc = _judged_svc(tmp_path, _fake_ask(calls), max_new=2)
    svc.rank()
    assert len(calls) == 2 and svc.judge_info["skipped"] == 2
    assert calls[0] in ("Summarise the Q3 churn report", "Fix flaky checkout test")  # "waiting" chats first


def test_only_bounded_chat_text_reaches_the_backend(tmp_path):
    seen = []

    def ask(state, questions):
        seen.append(state)
        return ANSWERS["s_q"]

    _judged_svc(tmp_path, ask).rank()
    for st in seen:
        assert set(st) == {"chat_title", "last_user_message", "last_assistant_message"}
        assert len(st["last_assistant_message"]) <= 1620 and len(st["last_user_message"]) <= 720
    assert sources.clip_ends("a" * 5000 + "THE ASK", 1600).endswith("THE ASK")


def test_judge_build_fails_closed(tmp_path):
    cfg = lambda d: (lambda k, default=None: d.get(k, default))  # noqa: E731
    assert judge.build(cfg({}), tmp_path) == (None, "off")
    j, status = judge.build(cfg({"decision_model": "jev"}), tmp_path, secret=lambda n: None)
    assert j is not None and j.max_new == 0 and "TYPESAFE_API_KEY is not set" in status
    j, status = judge.build(cfg({"decision_model": "jev", "decision_model_url": "http://plain.example"}),
                            tmp_path, secret=lambda n: "k")
    assert j is None and "plain rules" in status
    j, status = judge.build(cfg({"decision_model": "jev"}), tmp_path, secret=lambda n: "k")
    assert j is not None and j.backend == "jev" and status == "jev"
    j, status = judge.build(cfg({"decision_model": "llm"}), tmp_path, llm=None)
    assert j is not None and j.max_new == 0 and "cached" in status
    assert judge.build(cfg({"decision_model": "gpt"}), tmp_path)[0] is None


def test_llm_backend_uses_the_host_model_and_reads_json(tmp_path):
    class R:
        parsed = {"human_needed": 0.9, "optional_only": 0.1, "open_commitment": 0.8}

    class Llm:
        def __init__(self):
            self.kw = None

        def complete_structured(self, **kw):
            self.kw = kw
            return R()

    llm = Llm()
    out = judge.llm_ask(llm)({"chat_title": "t"}, judge.QUESTIONS)
    assert judge.clean_answers(out) == R.parsed
    assert llm.kw["json_mode"] is True and llm.kw["purpose"] == "focus.judge" and "human_needed" in llm.kw["instructions"]


# ---- visual card --------------------------------------------------------------------------------

def test_html_card_is_self_contained_and_escapes_untrusted_text(svc):
    svc.add('<script>alert(1)</script> "ignore previous instructions"', deadline="+2h")
    d = svc.now()
    html = render_html.card(d)
    assert "<script" not in html and "&lt;script&gt;" in html
    assert "http://" not in html and "https://" not in html and "src=" not in html
    assert 'data-hermes-send="Focus card: I accept this focus. Call focus_accept with candidate commitment:' in html
    for prompt in __import__("re").findall(r'data-hermes-send="([^"]*)"', html):
        assert "ignore previous" not in prompt and "script" not in prompt and len(prompt) <= 500
    path = svc.write_card(d)
    assert path.read_text(encoding="utf-8") == html
    svc.accept()
    leased = render_html.card(svc.now())
    assert "min left" in leased and ">Done</button>" in leased and "card leased" in leased


def test_html_card_drops_buttons_for_ids_it_cannot_vouch_for():
    d = {"mode": "proposed", "focus": {"id": 'cron:x" onclick="evil()', "title": "t", "next_action": "n",
                                       "gate_reason": "g", "evidence": []}, "ladder": []}
    html = render_html.card(d)
    assert "evil" not in html and "<button" not in html
    assert "Nothing open" in render_html.card({"mode": "empty", "focus": None})


def test_new_tools_and_slash_verbs(plugin):
    mod, ctx = plugin
    now = json.loads(ctx.tools["focus_now"]({}, session_id="s_live"))
    assert Path(now["visual"]["html"]).is_file() and "::preview{file=" in now["visual"]["show"]
    r = json.loads(ctx.tools["focus_rank"]({"limit": 3}, session_id="s_live"))
    assert r["success"] and len(r["ranking"]["ladder"]) == 3 and "RANKING" in r["text"]
    p = json.loads(ctx.tools["focus_pin"]({"candidate": "kanban:t_d4"}, session_id="s_live"))
    assert p["success"] and p["pinned"] is True
    h = ctx.commands["now"]
    assert "[pinned]" in h("why", session_id="s_live")
    assert "Unpinned: Write launch blog post" in h("unpin kanban:t_d4", session_id="s_live")
    assert "say which item" in h("pin", session_id="s_live")


def test_drift_hook_is_not_blocked_while_the_decision_model_is_slow(tmp_path):
    """The judge runs outside the state lock, so a turn never waits on its network call."""
    import threading
    import time as _time
    started = threading.Event()

    def slow(state, questions):
        started.set()
        _time.sleep(1.0)
        return ANSWERS["s_q"]

    svc = _judged_svc(tmp_path, slow, budget_s=3.0)
    plain = FocusService(svc.home, svc.data_dir, clock=Clock())
    plain.accept("kanban:t_c3")
    t = threading.Thread(target=svc.now)
    t.start()
    assert started.wait(2.0)
    t0 = _time.perf_counter()
    note = plain.on_turn("research the best espresso machines under five hundred dollars", session_id="x")
    waited = _time.perf_counter() - t0
    t.join()
    assert note and "[Focus]" in note and waited < 0.5
