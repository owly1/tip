import doctor


def test_stubs_detected_and_no_crash(monkeypatch, tmp_path):
    monkeypatch.setenv("TIP_HOME", str(tmp_path))
    monkeypatch.setenv("TIPD_URL", "http://127.0.0.1:1")
    sections = dict(doctor.run(listen=False))
    mods = {c: (s, d) for s, c, d in sections["modules"]}
    assert all(k.split(".")[0] in doctor.INTERFACES for k in mods)
    assert sections["tipd"][0][0] == doctor.WARN  # not running


def test_live_stripe_key_is_bad(monkeypatch):
    monkeypatch.chdir("/")
    monkeypatch.setenv("STRIPE_SECRET_KEY", "sk_live_abc")
    rows = {c: s for s, c, d in doctor.check_keys()}
    assert rows["STRIPE_SECRET_KEY"] == doctor.BAD


def test_cli_dispatch(monkeypatch, capsys):
    import cli
    monkeypatch.setenv("TIPD_URL", "http://127.0.0.1:1")
    code = cli.main(["doctor", "--no-mic", "--json"])
    assert code in (0, 1)
    assert '"sections"' in capsys.readouterr().out
