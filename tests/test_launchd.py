import plistlib

from taylortrader import launchd


def test_agents_hold_paths_not_secrets_and_the_dashboard_is_its_own(tmp_path):
    project = tmp_path / "proj"
    paths = launchd.write_plists(project, dest=tmp_path / "agents")
    labels = {p.stem for p in paths}
    assert labels == {launchd.SERVICE, launchd.WATCHDOG, launchd.DASHBOARD}
    dash = plistlib.loads((tmp_path / "agents" / f"{launchd.DASHBOARD}.plist").read_bytes())
    assert dash["ProgramArguments"][-2:] == ["dashboard", "--no-open"] and dash["KeepAlive"] is True
    for p in paths:
        values = repr(plistlib.loads(p.read_bytes())).upper()
        assert not any(w in values for w in ("SECRET", "PASSWORD", "KEY_ID", "API_KEY", "TOKEN"))


def test_stopping_the_bot_never_stops_the_dashboard(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(launchd, "_launchctl", lambda *a: calls.append(a) or type("R", (), {"returncode": 0, "stdout": "", "stderr": ""})())
    monkeypatch.setattr(launchd, "status", lambda label, dest=None: {"loaded": True})
    launchd.stop(dest=tmp_path)
    assert all(launchd.DASHBOARD not in " ".join(c) for c in calls)
