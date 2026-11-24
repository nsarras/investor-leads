import smtplib
from datetime import datetime, timezone

import pytest
from conftest import FakeLLM, make_news, make_post
from test_pipeline import KEYS, NOW, FakeLinkedIn, FakeX, do_run, read_csv, settings

from investor_leads import cli, emailer
from investor_leads.config import ConfigError, Settings, load_settings
from investor_leads.emailer import EmailError, SmtpConfig, compose, recipients, send

WHEN = datetime(2026, 11, 15, 7, 0)
SMTP = SmtpConfig(host="smtp.example.com", port=587, username="me@example.com", password="app-password",
                  sender="me@example.com")


class FakeServer:
    """Records what the emailer does with the SMTP connection."""

    def __init__(self, *args, fail=None, **kwargs):
        self.args, self.kwargs, self.fail, self.steps, self.sent = args, kwargs, fail, [], []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.steps.append("quit")
        return False

    def starttls(self, context=None):
        self.steps.append("starttls")

    def login(self, username, password):
        if self.fail:
            raise self.fail
        self.steps.append(f"login {username}")

    def send_message(self, message):
        self.steps.append("send")
        self.sent.append(message)


def run_with_leads(tmp_path, **kwargs):
    posts = [make_post(1, platform="linkedin"), make_news(1), make_news(2, investor="Play Ventures")]
    return do_run(tmp_path, FakeX([]), FakeLinkedIn(posts), **kwargs)[0]


# ---- settings ---------------------------------------------------------------------------------

def test_recipients_come_from_flag_then_env_then_config():
    s = Settings(industry="gaming", stage="seed")
    s.email.to = ["config@example.com"]
    assert recipients(s, None, {}) == ["config@example.com"]
    assert recipients(s, None, {"EMAIL_TO": "a@example.com; b@example.com, a@example.com"}) == ["a@example.com", "b@example.com"]
    assert recipients(s, "flag@example.com", {"EMAIL_TO": "env@example.com"}) == ["flag@example.com"]

    with pytest.raises(ConfigError, match="No email recipients"):
        recipients(Settings(industry="gaming", stage="seed"), None, {})
    with pytest.raises(ConfigError, match="not-an-address"):
        recipients(s, "ok@example.com, not-an-address", {})


def test_smtp_settings_from_env():
    env = {"SMTP_HOST": "smtp.gmail.com", "SMTP_USERNAME": "me@gmail.com", "SMTP_PASSWORD": " abcd efgh "}
    smtp = SmtpConfig.from_env(env)
    assert (smtp.host, smtp.port, smtp.sender, smtp.password) == ("smtp.gmail.com", 587, "me@gmail.com", "abcd efgh")
    smtp.check()
    assert SmtpConfig.from_env({**env, "SMTP_PORT": "465", "EMAIL_FROM": "leads@example.com"}).sender == "leads@example.com"

    with pytest.raises(ConfigError, match="SMTP_HOST, SMTP_PASSWORD"):
        SmtpConfig.from_env({"SMTP_USERNAME": "me@gmail.com"}).check()
    with pytest.raises(ConfigError, match="SMTP_PORT"):
        SmtpConfig.from_env({"SMTP_PORT": "tls"})


def test_email_settings_in_config_file(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text("industry: gaming\nstage: seed\nemail:\n  to: [a@example.com, b@example.com]\n  max_leads_in_body: 3\n")
    s = load_settings(path)
    assert (s.email.to, s.email.max_leads_in_body) == (["a@example.com", "b@example.com"], 3)


# ---- the message ------------------------------------------------------------------------------

def test_email_with_leads_has_summary_and_spreadsheet(tmp_path):
    result = run_with_leads(tmp_path, only_new=True)
    s = settings(tmp_path)
    s.days = 1
    message = compose(result, s, sender="me@example.com", to=["a@example.com", "b@example.com"], when=WHEN, only_new=True)

    assert message["Subject"] == "Investor leads, Nov 15: 3 new (1 high, 2 medium-high)"
    assert (message["From"], message["To"]) == ("me@example.com", "a@example.com, b@example.com")
    body = message.get_body(preferencelist=("plain",)).get_content()
    assert "3 new investor leads from posts in the last day: 1 high, 2 medium-high." in body
    assert "Industries: Gaming, Entertainment" in body and "Posts read: 3" in body
    assert "1. [high] Person 1 (Pixel Ventures): seeking deals" in body
    assert "https://example.com/linkedin/1" in body and "[medium-high] Merak Capital: made an investment" in body
    assert "API costs for this run:" in body

    attachments = list(message.iter_attachments())
    assert [a.get_filename() for a in attachments] == [result.xlsx_path.name]
    assert attachments[0].get_content_type() == "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    assert attachments[0].get_content() == result.xlsx_path.read_bytes()


def test_long_lists_are_cut_short_in_the_body(tmp_path):
    result = run_with_leads(tmp_path)
    s = settings(tmp_path)
    s.email.max_leads_in_body = 1
    message = compose(result, s, sender="me@example.com", to=["a@example.com"], when=WHEN, only_new=False)
    body = message.get_body(preferencelist=("plain",)).get_content()
    assert "Strongest leads:" in body and "2. [" not in body
    assert "...and 2 more in the attached spreadsheet." in body
    assert "3 investor leads from posts in the last 7 days" in body  # not "new" without --only-new


def test_quiet_day_sends_a_short_note_without_attachment(tmp_path):
    result, _ = do_run(tmp_path, FakeX([make_post(2, investor=False)]), FakeLinkedIn([]), only_new=True)
    message = compose(result, settings(tmp_path), sender="me@example.com", to=["a@example.com"], when=WHEN, only_new=True)
    assert message["Subject"] == "Investor leads, Nov 15: no new leads"
    body = message.get_content()
    assert body.startswith("No new investor leads today.") and "Posts read: 1" in body
    assert list(message.iter_attachments()) == []


# ---- sending ----------------------------------------------------------------------------------

def test_send_uses_starttls_and_logs_in(tmp_path):
    message = compose(run_with_leads(tmp_path), settings(tmp_path), sender="me@example.com", to=["a@example.com"],
                      when=WHEN, only_new=False)
    servers = []

    def connect(*args, **kwargs):
        servers.append(FakeServer(*args, **kwargs))
        return servers[-1]

    send(message, SMTP, connect=connect)
    assert servers[0].args == ("smtp.example.com", 587)
    assert servers[0].steps == ["starttls", "login me@example.com", "send", "quit"] and servers[0].sent == [message]

    send(message, SmtpConfig(host="smtp.example.com", port=465, username="u", password="p"), connect=connect)
    assert servers[1].steps == ["login u", "send", "quit"] and "context" in servers[1].kwargs  # TLS from the start


def test_send_failures_are_explained(tmp_path):
    message = compose(run_with_leads(tmp_path), settings(tmp_path), sender="me@example.com", to=["a@example.com"],
                      when=WHEN, only_new=False)
    rejected = smtplib.SMTPAuthenticationError(535, b"Username and Password not accepted")
    with pytest.raises(EmailError, match="app password"):
        send(message, SMTP, connect=lambda *a, **k: FakeServer(fail=rejected))

    def unreachable(*args, **kwargs):
        raise OSError("Connection refused")

    with pytest.raises(EmailError, match="smtp.example.com:587"):
        send(message, SMTP, connect=unreachable)


# ---- only new leads ---------------------------------------------------------------------------

def test_only_new_lists_leads_from_posts_first_seen_in_this_run(tmp_path):
    llm = FakeLLM()
    first, _ = do_run(tmp_path, FakeX([make_post(1)]), FakeLinkedIn([make_news(1)]), llm=llm, only_new=True)
    assert [r["Name"] for r in read_csv(first.csv_path)] == ["Person 1", "Merak Capital"]

    # The next day's run finds yesterday's posts again, plus two new ones.
    second, logs = do_run(tmp_path, FakeX([make_post(1), make_post(5)]),
                          FakeLinkedIn([make_news(1), make_news(2, investor="Play Ventures")]), llm=llm, only_new=True)
    assert [r["Name"] for r in read_csv(second.csv_path)] == ["Person 5", "Play Ventures"]
    assert second.posts_considered == 2 and any("2 new leads (1 high, 1 medium-high)" in line for line in logs)

    # A third run with nothing new is a quiet day; without --only-new everything is still there.
    third, _ = do_run(tmp_path, FakeX([make_post(1), make_post(5)]), FakeLinkedIn([make_news(1)]), llm=llm, only_new=True)
    assert third.leads == 0 and third.lead_items == []
    everything, _ = do_run(tmp_path, FakeX([]), FakeLinkedIn([]), llm=llm)
    assert everything.leads == 4


# ---- command line -----------------------------------------------------------------------------

@pytest.fixture
def cli_env(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    for name in ("SMTP_HOST", "SMTP_PORT", "SMTP_USERNAME", "SMTP_PASSWORD", "EMAIL_FROM", "EMAIL_TO",
                 "ANTHROPIC_API_KEY", "X_BEARER_TOKEN", "APIFY_API_TOKEN"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(cli, "load_dotenv", lambda: None)
    return monkeypatch


def test_cli_checks_mail_settings_before_running(cli_env, capsys):
    cli_env.setattr(cli, "run", lambda *a, **k: pytest.fail("the run must not start"))
    assert cli.main(["run", "--industry", "gaming", "--stage", "seed", "--email"]) == 2
    assert "SMTP_HOST, SMTP_USERNAME, SMTP_PASSWORD" in capsys.readouterr().err

    cli_env.setenv("SMTP_HOST", "smtp.gmail.com")
    cli_env.setenv("SMTP_USERNAME", "me@gmail.com")
    cli_env.setenv("SMTP_PASSWORD", "app-password")
    assert cli.main(["run", "--industry", "gaming", "--stage", "seed", "--email"]) == 2
    assert "No email recipients" in capsys.readouterr().err


def test_cli_run_passes_only_new_and_emails_the_result(cli_env, tmp_path, capsys):
    cli_env.setenv("SMTP_HOST", "smtp.gmail.com")
    cli_env.setenv("SMTP_USERNAME", "me@gmail.com")
    cli_env.setenv("SMTP_PASSWORD", "app-password")
    cli_env.setenv("EMAIL_TO", "a@example.com,b@example.com")
    seen, sent = {}, []

    def fake_run(settings_, keys, **kwargs):
        seen.update(kwargs, days=settings_.days)
        return run_with_leads(tmp_path / "run", only_new=kwargs["only_new"])

    cli_env.setattr(cli, "run", fake_run)
    cli_env.setattr(emailer, "send", lambda message, smtp: sent.append((message, smtp)))

    code = cli.main(["run", "--industry", "gaming", "--stage", "seed", "--days", "1", "--only-new", "--email"])
    assert code == 0 and seen["only_new"] is True and seen["days"] == 1
    message, smtp = sent[0]
    assert message["To"] == "a@example.com, b@example.com" and message["From"] == "me@gmail.com"
    assert " new (" in message["Subject"] and smtp.host == "smtp.gmail.com"
    assert "Emailed 2 recipients" in capsys.readouterr().out

    def broken(message, smtp):
        raise EmailError("Could not send")

    cli_env.setattr(emailer, "send", broken)
    assert cli.main(["run", "--industry", "gaming", "--stage", "seed", "--email"]) == 1  # failure is visible to schedulers

    cli_env.setattr(emailer, "send", lambda *a: pytest.fail("a dry run must not email"))
    assert cli.main(["run", "--industry", "gaming", "--stage", "seed", "--email", "--dry-run"]) == 0


def test_cli_export_can_email_too(cli_env, tmp_path):
    cli_env.setenv("SMTP_HOST", "smtp.gmail.com")
    cli_env.setenv("SMTP_USERNAME", "me@gmail.com")
    cli_env.setenv("SMTP_PASSWORD", "app-password")
    sent = []
    cli_env.setattr(cli, "export_only", lambda settings_: run_with_leads(tmp_path / "run"))
    cli_env.setattr(emailer, "send", lambda message, smtp: sent.append(message))
    assert cli.main(["export", "--industry", "gaming", "--stage", "seed", "--email-to", "me@example.com"]) == 0
    assert sent[0]["To"] == "me@example.com" and "new" not in sent[0]["Subject"]
