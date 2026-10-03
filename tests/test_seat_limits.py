"""Per-seat limits (each Claude account its own 5-hour / weekly stop and agent count) and usage telemetry."""
import json
import urllib.request

import pytest

from conftest import cli
from clodfarm.config import load
from clodfarm.governor import FIVE_HOURS, SEVEN_DAYS, Policy, Snapshot, Window, clean_limits, decide, with_limits
from clodfarm.store import Store, now

NOW = 1_800_000_000.0


def snap(u5, u7=0.1, t=None):
    t = t or now()
    return Snapshot(observed_at=t, status="allowed", rate_limit_type="five_hour", resets_at=t + FIVE_HOURS / 2,
                    five_hour=Window(u5, t + FIVE_HOURS / 2), seven_day=Window(u7, t + SEVEN_DAYS / 2))


# ------------------------------------------------------------------ governor
def test_clean_limits_validates_and_drops_unknown_keys():
    assert clean_limits({"five_hour_ceiling": "0.5", "weekly_target": None, "nope": 1}) == {"five_hour_ceiling": 0.5}
    assert clean_limits({"max_workers": "2"}) == {"max_workers": 2}
    with pytest.raises(ValueError):
        clean_limits({"five_hour_ceiling": 50})  # 50 means 5000%: a share is 0-1
    with pytest.raises(ValueError):
        clean_limits({"max_workers": -1})


def test_a_seat_with_a_lower_five_hour_limit_stops_where_the_default_runs():
    base = Policy(max_workers=3, min_workers=1, five_hour_ceiling=0.85, weekly_target=0.80)
    s = snap(0.55, t=NOW)
    assert decide(s, base, NOW).workers >= 1
    half = with_limits(base, {"five_hour_ceiling": 0.5})
    d = decide(s, half, NOW)
    assert d.workers == 0 and "ceiling 50%" in d.reason and d.pause_until == s.five_hour.resets_at
    assert with_limits(base, {}) is base


def test_a_seat_with_fewer_agents_runs_fewer():
    base = Policy(max_workers=3, min_workers=1)
    assert decide(snap(0.01, t=NOW), with_limits(base, {"max_workers": 1}), NOW).workers == 1


# ------------------------------------------------------------------ config + store
def test_env_limits_and_stored_limits_layer_over_the_defaults(store, monkeypatch):
    monkeypatch.setenv("FARM_SEAT_LIMITS", json.dumps({"alice-1": {"five_hour_ceiling": 0.6, "weekly_target": 0.7}}))
    monkeypatch.setenv("FARM_FIVE_HOUR_CEILING", "0.85")
    cfg = load()
    assert cfg.policy_for(store, "bob-2").five_hour_ceiling == 0.85
    assert cfg.policy_for(store, "alice-1").five_hour_ceiling == 0.6
    store.set_seat_limits("alice-1", {"five_hour_ceiling": 0.5})  # the stored one wins over the env
    p = cfg.policy_for(store, "alice-1")
    assert (p.five_hour_ceiling, p.weekly_target) == (0.5, 0.7)
    store.set_seat_limits("alice-1", {}, clear=["five_hour_ceiling"])
    assert cfg.policy_for(store, "alice-1").five_hour_ceiling == 0.6
    assert store.all_seat_limits() == {"alice-1": {}}


def test_bad_env_limits_are_ignored(monkeypatch, env):
    monkeypatch.setenv("FARM_SEAT_LIMITS", "{not json")
    assert load().seat_limits == {}


def test_usage_history_keeps_one_point_per_minute_across_days(store):
    t0 = (now() // 86400) * 86400 - 90  # 90 s before UTC midnight: the history spans two day partitions
    store.put_snapshot(snap(0.10, t=t0), "s1")
    store.put_snapshot(snap(0.12, t=t0 + 5), "s1")      # same minute: replaces
    store.put_snapshot(snap(0.11, t=t0 + 2), "s1")      # older than what's there: ignored
    store.put_snapshot(snap(0.20, t=t0 + 120), "s1")    # next day
    store.put_snapshot(snap(0.90, t=t0 + 60), "other")  # another seat
    pts = store.usage_history("s1", t0 - 10, t0 + 200)
    assert [p["five_hour"] for p in pts] == [0.12, 0.20]
    assert pts[0]["seven_day"] == 0.1 and pts[0]["seat"] == "s1"
    assert store.usage_history("s1", t0 + 100, t0 + 200)[0]["five_hour"] == 0.20


# ------------------------------------------------------------------ CLI
def test_cli_limits_and_usage(store):
    store.put_snapshot(snap(0.42), "alice-1")
    store.put_snapshot(snap(0.10), "bob-2")
    out = cli("limits", "--seat", "alice-1", "--five-hour", "50", "--weekly", "60%", "--json")
    seats = {s["seat"]: s for s in json.loads(out.stdout)["seats"]}
    assert seats["alice-1"]["five_hour_ceiling"] == 0.5 and seats["alice-1"]["weekly_target"] == 0.6
    assert seats["alice-1"]["own"] == {"five_hour_ceiling": 0.5, "weekly_target": 0.6}
    assert seats["bob-2"]["own"] == {}
    assert "alice-1" in cli("limits").stdout
    bad = cli("limits", "--five-hour", "50", check=False)  # two seats: which one?
    assert bad.returncode != 0 and "--seat" in bad.stderr
    budget = json.loads(cli("budget", "--json").stdout)
    a = next(s for s in budget["seats"] if s["seat"] == "alice-1")
    assert a["limits"]["five_hour_ceiling"] == 0.5
    u = json.loads(cli("usage", "--seat", "alice-1", "--json").stdout)
    assert u["seats"]["alice-1"]["five_hour"]["now"] == 0.42
    assert u["seats"]["alice-1"]["limits"]["five_hour_ceiling"] == 0.5
    csv = cli("usage", "--csv").stdout.splitlines()
    assert csv[0].startswith("time,seat,five_hour") and any(",alice-1,0.42," in r for r in csv)
    cli("limits", "--seat", "alice-1", "--clear")
    assert store.seat_limits("alice-1") == {}


# ------------------------------------------------------------------ web
@pytest.fixture
def ui(env, backend):
    if backend != "sqlite":
        pytest.skip("one backend is enough for the UI")
    import threading
    from http.server import ThreadingHTTPServer
    from clodfarm.web import FarmUI, make_handler
    cfg = load()
    st = Store.from_config(cfg)
    st.ensure_table()
    farm_ui = FarmUI(cfg, st)
    srv = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(farm_ui))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}", farm_ui
    srv.shutdown()
    farm_ui.manager.shutdown()


def test_usage_api_metrics_and_limits_endpoint(ui, monkeypatch):
    from test_web import client, login
    base, farm_ui = ui
    farm_ui.store.put_snapshot(snap(0.33, 0.21), "alice-1")
    farm_ui.store.heartbeat("alice@box", "w0", "idle", seat="alice-1")
    watcher = client()
    code, body, _ = watcher(base + "/api/usage?hours=5")
    assert code == 200, body
    seat = next(s for s in body["seats"] if s["seat"] == "alice-1")
    assert seat["claudes"] == ["alice"] and seat["now"]["five_hour"] == 0.33 and len(seat["points"]) == 1
    assert seat["can_edit"] is False
    assert watcher(base + "/api/limits", {"seat": "alice-1", "five_hour_ceiling": 0.5})[0] == 403

    with urllib.request.urlopen(base + "/metrics") as r:  # a public farm: anyone who watches may scrape
        text = r.read().decode()
    assert 'clodfarm_utilization_ratio{seat="alice-1",claudes="alice",window="five_hour"} 0.33' in text
    assert 'clodfarm_limit_ratio{seat="alice-1",claudes="alice",window="five_hour"} 0.85' in text

    manager = client()
    login(manager, base)
    code, body, _ = manager(base + "/api/limits", {"seat": "alice-1", "five_hour_ceiling": 0.5, "max_workers": 2})
    assert code == 200, body
    assert body["seats"][0]["limits"]["own"] == {"five_hour_ceiling": 0.5, "max_workers": 2}
    assert manager(base + "/api/limits", {"seat": "alice-1", "five_hour_ceiling": 5})[0] == 400

    monkeypatch.setenv("FARM_METRICS_TOKEN", "s3cret")
    with pytest.raises(urllib.error.HTTPError):
        urllib.request.urlopen(base + "/metrics")
    req = urllib.request.Request(base + "/metrics", headers={"Authorization": "Bearer s3cret"})
    with urllib.request.urlopen(req) as r:
        assert 'window="five_hour"} 0.5' in r.read().decode()

    with urllib.request.urlopen(base + "/usage") as r:
        assert b"usage.js" in r.read()
