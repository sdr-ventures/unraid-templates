"""`sync-templates.py`: the closing line counts the files a live run wrote.

A live run with no file write closes `(LIVE — nothing changed)`; any write closes with the number
of files written. The count includes every backup, including one written before a failed
instance write, so no run can leave a file on disk while claiming nothing changed.
"""

import datetime as _dt
import importlib.util
import pathlib

import pytest

SCRIPT = pathlib.Path(__file__).resolve().parents[1] / "scripts" / "sync-templates.py"
BACKUPS = ".template-sync-backups"

TPL = (
    '<Container version="2"><Name>widget</Name><Repository>example/widget:1</Repository>'
    '<TemplateURL>https://example.invalid/templates/widget.xml</TemplateURL>'
    '<Config Name="W" Target="W" Default="1" Mode="" Description="{desc}" Type="Variable" '
    'Display="always" Required="false" Mask="{mask}">{value}</Config></Container>'
)
TEMPLATE_XML = TPL.format(desc="w", mask="false", value="")


class Frozen(_dt.datetime):
    @classmethod
    def now(cls, tz=None):
        return cls(2026, 1, 2, 3, 4, 5)


@pytest.fixture()
def sync(monkeypatch):
    spec = importlib.util.spec_from_file_location("sync_templates_banner", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mod.DRY_RUN = False
    mod.TEMPLATE = "widget"
    mod.resolve_sha = lambda: "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    monkeypatch.setattr(mod, "list_repo_templates", lambda sha: ["widget"])
    monkeypatch.setattr(mod, "fetch_template", lambda name, sha: TEMPLATE_XML.encode())
    monkeypatch.setattr(mod, "datetime", Frozen)
    return mod


def run(sync, root, capsys):
    sync.TEMPLATES_USER = str(root)
    code = 0
    try:
        sync.main()
    except SystemExit as e:
        code = e.code
    return code, capsys.readouterr().out


def closing_line(out):
    return [line for line in out.splitlines() if line.startswith("done")][-1]


def test_a_live_run_that_wrote_nothing_does_not_claim_it_wrote(sync, tmp_path, capsys):
    (tmp_path / "my-widget.xml").write_bytes(TPL.format(desc="w", mask="false", value="5").encode())
    before = (tmp_path / "my-widget.xml").read_bytes()
    code, out = run(sync, tmp_path, capsys)
    assert code == 0, out
    assert (tmp_path / "my-widget.xml").read_bytes() == before
    assert closing_line(out) == "done.  (LIVE — nothing changed)"


def test_a_live_run_that_updates_an_instance_counts_the_backup_and_the_instance(sync, tmp_path, capsys):
    (tmp_path / "my-widget.xml").write_bytes(TPL.format(desc="old", mask="false", value="5").encode())
    code, out = run(sync, tmp_path, capsys)
    assert code == 0, out
    assert len(list((tmp_path / BACKUPS).iterdir())) == 1
    assert closing_line(out) == "done.  (LIVE — changes written: 2)"


def test_a_backup_written_before_a_failed_instance_write_is_counted(sync, tmp_path, capsys, monkeypatch):
    """The backup is on disk even though the instance write failed, so the run must say it wrote."""
    (tmp_path / "my-widget.xml").write_bytes(TPL.format(desc="old", mask="false", value="5").encode())
    real_write = sync.atomic_write

    def fail_the_instance(path, root):
        if str(path).endswith("my-widget.xml"):
            raise OSError("disk full")
        return real_write(path, root)

    monkeypatch.setattr(sync, "atomic_write", fail_the_instance)
    code, out = run(sync, tmp_path, capsys)
    assert code == 1, out
    assert len(list((tmp_path / BACKUPS).iterdir())) == 1
    assert closing_line(out) == "done. 1 failure(s) — see above  (LIVE — changes written: 1)"


def test_a_live_run_that_creates_the_stub_counts_it_as_a_write(sync, tmp_path, capsys):
    code, out = run(sync, tmp_path, capsys)
    assert code == 0, out
    assert (tmp_path / "my-widget.xml").exists()
    assert closing_line(out) == "done.  (LIVE — changes written: 1)"


def test_a_live_run_whose_only_change_is_a_prune_says_it_wrote(sync, tmp_path, capsys):
    """Pruning deletes a file, so it is a change: the banner must not call that a no-op."""
    (tmp_path / "my-widget.xml").write_bytes(TPL.format(desc="w", mask="false", value="5").encode())
    b = tmp_path / BACKUPS
    b.mkdir()
    for i in range(sync.KEEP_BACKUPS + 2):
        (b / f"my-widget.xml.20200101-0000{i:02d}.bak").write_bytes(
            TPL.format(desc="w", mask="false", value="5").encode())
    code, out = run(sync, tmp_path, capsys)
    assert code == 0, out
    assert len(list(b.iterdir())) == sync.KEEP_BACKUPS
    assert closing_line(out) == "done.  (LIVE — changes written: 2)"


def test_a_live_run_that_redacts_an_old_backup_says_it_wrote(sync, tmp_path, capsys):
    (tmp_path / "my-widget.xml").write_bytes(TPL.format(desc="w", mask="false", value="5").encode())
    b = tmp_path / BACKUPS
    b.mkdir()
    (b / "my-widget.xml.20200101-000000.bak").write_bytes(
        TPL.format(desc="w", mask="true", value="s3cret").encode())
    code, out = run(sync, tmp_path, capsys)
    assert code == 0, out
    assert b"s3cret" not in (b / "my-widget.xml.20200101-000000.bak").read_bytes()
    assert closing_line(out) == "done.  (LIVE — changes written: 1)"


def test_a_live_run_with_a_failure_still_reports_what_it_wrote(sync, tmp_path, capsys):
    (tmp_path / "my-widget.xml").write_bytes(b"<Container>not closed")
    code, out = run(sync, tmp_path, capsys)
    assert code == 1, out
    assert closing_line(out) == "done. 1 failure(s) — see above  (LIVE — nothing changed)"


def test_a_dry_run_says_nothing_changed_and_writes_nothing(sync, tmp_path, capsys):
    (tmp_path / "my-widget.xml").write_bytes(TPL.format(desc="old", mask="false", value="5").encode())
    before = (tmp_path / "my-widget.xml").read_bytes()
    sync.DRY_RUN = True
    code, out = run(sync, tmp_path, capsys)
    assert code == 0, out
    assert (tmp_path / "my-widget.xml").read_bytes() == before
    assert closing_line(out) == "done.  (dry-run — nothing changed)"
