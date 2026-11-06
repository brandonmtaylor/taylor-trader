import plistlib

from taylortrader import launchd


def test_agents_hold_paths_not_secrets_and_the_dashboard_is_its_own(tmp_path, monkeypatch):
    from taylortrader import home

    # Nothing installed: no checkout environment and no `taylortrader` on PATH (CI's `uv run` adds one).
    monkeypatch.setattr(home.shutil, "which", lambda n: None if n == "taylortrader" else f"/usr/bin/{n}")
    project = tmp_path / "proj"
    paths = launchd.write_plists(project, dest=tmp_path / "agents")
    labels = {p.stem for p in paths}
    assert labels == {launchd.SERVICE, launchd.WATCHDOG, launchd.DASHBOARD}
    dash = plistlib.loads((tmp_path / "agents" / f"{launchd.DASHBOARD}.plist").read_bytes())
    assert dash["ProgramArguments"][-2:] == ["dashboard", "--no-open"] and dash["KeepAlive"] is True
    assert "run" in dash["ProgramArguments"][1]  # no environment yet: falls back to `uv run`
    exe = project / ".venv" / "bin" / "taylortrader"
    exe.parent.mkdir(parents=True)
    exe.write_text("")
    launchd.write_plists(project, dest=tmp_path / "agents")
    dash = plistlib.loads((tmp_path / "agents" / f"{launchd.DASHBOARD}.plist").read_bytes())
    assert dash["ProgramArguments"][0] == str(exe)  # launchd's stop signal reaches Python directly
    for p in paths:
        values = repr(plistlib.loads(p.read_bytes())).upper()
        assert not any(w in values for w in ("SECRET", "PASSWORD", "KEY_ID", "API_KEY", "TOKEN"))


def test_stopping_the_bot_never_stops_the_dashboard(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(launchd, "_launchctl", lambda *a: calls.append(a) or type("R", (), {"returncode": 0, "stdout": "", "stderr": ""})())
    monkeypatch.setattr(launchd, "status", lambda label, dest=None: {"loaded": True})
    launchd.stop(dest=tmp_path)
    assert all(launchd.DASHBOARD not in " ".join(c) for c in calls)


def test_home_folder(tmp_path, monkeypatch):
    from taylortrader import home

    monkeypatch.delenv(home.HOME_VAR, raising=False)
    monkeypatch.setattr(home, "default_home", lambda: tmp_path / "AppSupport")
    assert home.find_home(tmp_path) == tmp_path / "AppSupport"          # a random folder: the default home
    (tmp_path / "config.toml").write_text("")
    assert home.find_home(tmp_path) == tmp_path                         # a folder with settings: that one
    co = tmp_path / "co"
    co.mkdir()
    (co / "pyproject.toml").write_text('[project]\nname = "taylortrader"\n')
    assert home.find_home(co) == co                                     # a source checkout
    monkeypatch.setenv(home.HOME_VAR, str(tmp_path / "x"))
    assert home.find_home(co) == tmp_path / "x"                         # the override wins


def test_background_jobs_prefer_the_checkout_then_the_installed_command(tmp_path, monkeypatch):
    from taylortrader import home

    monkeypatch.setattr(home.shutil, "which", lambda n: f"/usr/local/bin/{n}")
    assert home.command(tmp_path) == ["/usr/local/bin/taylortrader"]
    exe = tmp_path / ".venv" / "bin" / "taylortrader"
    exe.parent.mkdir(parents=True)
    exe.write_text("")
    assert home.command(tmp_path) == [str(exe)]
