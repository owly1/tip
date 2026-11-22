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


def test_with_env_file(monkeypatch, tmp_path):
    (tmp_path / ".env").write_text("ELEVENLABS_API_KEY=abc\nSTRIPE_SECRET_KEY=sk_test_123\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    for k, _, _ in doctor.KEYS:  # other tests may have loaded the real .env into os.environ
        monkeypatch.delenv(k, raising=False)
    rows = {c: s for s, c, d in doctor.check_keys()}
    assert rows["ELEVENLABS_API_KEY"] == doctor.OK and rows["STRIPE_SECRET_KEY"] == doctor.OK


def test_cli_dispatch(monkeypatch, capsys):
    import cli
    monkeypatch.setenv("TIPD_URL", "http://127.0.0.1:1")
    code = cli.main(["doctor", "--no-mic", "--json"])
    assert code in (0, 1)
    assert '"sections"' in capsys.readouterr().out
