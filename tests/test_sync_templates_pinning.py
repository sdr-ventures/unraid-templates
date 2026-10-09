"""`sync-templates.py`: one commit per run (unraid-templates#103).

The run resolves BRANCH to a commit once, then reads the template listing and the template body
at that commit. A body read by branch from raw.githubusercontent.com can be up to 300 s stale, so
a run just after a merge could list the new templates and merge against the old body.

The HTTP layer here is fake (`urllib.request.urlopen` replaced): the branch URLs serve a STALE
body, the commit URLs the current one, so a run that read anything by branch shows it.
"""

import email.message
import importlib.util
import io
import json
import pathlib
import urllib.error

import pytest

SCRIPT = pathlib.Path(__file__).resolve().parents[1] / "scripts" / "sync-templates.py"
SHA = "0123456789abcdef0123456789abcdef01234567"


def cfg(name, value=""):
    return (f'<Config Name="{name}" Target="{name}" Default="" Mode="" Description="x" '
            f'Type="Variable" Display="always" Required="false" Mask="false">{value}</Config>')


def container(configs):
    return ('<Container version="2"><Name>app</Name><Repository>example/app:1</Repository>'
            '<TemplateURL>https://example.invalid/templates/app.xml</TemplateURL>'
            f'{"".join(configs)}</Container>')


STALE = container([cfg("OLD_NAME")]).encode()       # what the branch URL still serves
CURRENT = container([cfg("NEW_NAME")]).encode()     # the body at the resolved commit


def listing(*names):
    return json.dumps([{"type": "file", "name": f"{n}.xml"} for n in names]).encode()


class FakeHTTP:
    """Serves the branch and the commit differently, and records every request."""

    def __init__(self, sync):
        r, t = sync.REPO, sync.TEMPLATE_SUBDIR
        self.routes = {
            f"https://api.github.com/repos/{r}/commits/main": SHA.encode(),
            f"https://api.github.com/repos/{r}/contents/{t}?ref=main": listing("app", "stale-only"),
            f"https://api.github.com/repos/{r}/contents/{t}?ref={SHA}": listing("app"),
            f"https://raw.githubusercontent.com/{r}/main/{t}/app.xml": STALE,
            f"https://raw.githubusercontent.com/{r}/{SHA}/{t}/app.xml": CURRENT,
        }
        self.seen = []

    def __call__(self, req, timeout=None):
        self.seen.append((req.full_url, req.get_header("Accept")))
        if req.full_url not in self.routes:
            raise AssertionError(f"unexpected request {req.full_url}")
        return io.BytesIO(self.routes[req.full_url])


@pytest.fixture()
def sync(monkeypatch):
    spec = importlib.util.spec_from_file_location("sync_templates_pinning", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mod.DRY_RUN = False
    mod.TEMPLATE = "app"
    return mod


@pytest.fixture()
def http(sync, monkeypatch):
    fake = FakeHTTP(sync)
    monkeypatch.setattr(sync.urllib.request, "urlopen", fake)
    return fake


def run(sync, root, capsys):
    sync.TEMPLATES_USER = str(root)
    code = 0
    try:
        sync.main()
    except SystemExit as e:
        code = e.code
    return code, capsys.readouterr().out


def test_the_run_reads_the_listing_and_the_body_at_the_resolved_commit(sync, http, tmp_path, capsys):
    (tmp_path / "my-app.xml").write_text(container([cfg("NEW_NAME", "applied")]), encoding="utf-8")
    code, out = run(sync, tmp_path, capsys)
    assert code == 0, out
    live = (tmp_path / "my-app.xml").read_text(encoding="utf-8")
    assert "NEW_NAME" in live and "applied" in live and "OLD_NAME" not in live, (
        "the run merged against the stale branch body")
    assert "DROPPED" not in out and "added         :" not in out
    urls = [u for u, _ in http.seen]
    assert all(SHA in u for u in urls[1:]), f"read something by branch: {urls}"
    assert urls[0].endswith("/commits/main") and http.seen[0][1] == "application/vnd.github.sha"
    assert len(urls) == 3, "one resolve, one listing, one body"
    assert f"commit={SHA}" in out.splitlines()[0], "the report must name the commit"


def test_the_listing_is_the_commits_not_the_branchs(sync, http, tmp_path, capsys):
    """`stale-only` is in the branch listing only: a run listing by branch would accept it."""
    sync.TEMPLATE = "stale-only"
    code, out = run(sync, tmp_path, capsys)
    assert isinstance(code, str) and "is not a template" in code and SHA in code, code
    assert not any(tmp_path.iterdir())


@pytest.mark.parametrize("answer", [b"", b"main", b"<html>sign in</html>", SHA[:39].encode(),
                                    SHA.upper().encode()])
def test_an_answer_that_is_not_a_commit_sha_refuses_before_any_write(
        sync, http, tmp_path, capsys, answer):
    http.routes[f"https://api.github.com/repos/{sync.REPO}/commits/main"] = answer
    (tmp_path / "my-app.xml").write_text(container([cfg("NEW_NAME", "applied")]), encoding="utf-8")
    before = (tmp_path / "my-app.xml").read_bytes()
    code, out = run(sync, tmp_path, capsys)
    assert isinstance(code, str) and "did not return a commit sha" in code and "Nothing changed" in code
    assert (tmp_path / "my-app.xml").read_bytes() == before
    assert len(http.seen) == 1, "nothing may be read after a failed resolve"


def _limited(headers):
    def raise_limited(req, timeout=None):
        msg = email.message.Message()
        for k, v in headers.items():
            msg[k] = v
        raise urllib.error.HTTPError(req.full_url, 403, "rate limit exceeded", msg, None)
    return raise_limited


@pytest.mark.parametrize("headers,expect", [
    ({"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": "1700000000"}, "resets at 20"),
    ({"Retry-After": "60"}, "resets at in 60s"),
])
def test_the_rate_limit_is_named_with_its_reset_and_nothing_changes(
        sync, tmp_path, capsys, monkeypatch, headers, expect):
    monkeypatch.setattr(sync.urllib.request, "urlopen", _limited(headers))
    (tmp_path / "my-app.xml").write_text(container([cfg("NEW_NAME", "applied")]), encoding="utf-8")
    before = (tmp_path / "my-app.xml").read_bytes()
    code, out = run(sync, tmp_path, capsys)
    assert isinstance(code, str) and "rate limit reached" in code and "60 requests/hour" in code, code
    assert expect in code and "Nothing changed" in code
    assert (tmp_path / "my-app.xml").read_bytes() == before


def test_a_403_that_is_not_the_rate_limit_is_not_called_one(sync, tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(sync.urllib.request, "urlopen", _limited({}))
    code, out = run(sync, tmp_path, capsys)
    assert isinstance(code, str) and "HTTP Error 403" in code and "rate limit reached" not in code
