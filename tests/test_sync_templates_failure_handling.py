"""`sync-templates.py`: the failure paths a read-only sweep found unwrapped (unraid-templates#61)
and the exit-code/reporting gaps that ride along with them (#62).

Before this file: only the BACKUP write was protected against an `OSError` mid-loop — the
live-instance write, the CREATE write, the instance read, and the prune `os.remove` were not,
so a full or read-only flash drive gave a traceback out of `main()` instead of a per-instance
SKIP. And even where a failure WAS caught and printed, nothing counted it: a run that skipped
every template still printed "done." and exited 0.

Each test here injects the actual failure (the suite's own established pattern —
`test_a_backup_that_cannot_be_WRITTEN_skips_the_instance_instead_of_crashing` in
test_sync_templates_metadata.py is the template), not a happy-path assertion.
"""

import importlib.util
import pathlib
import xml.etree.ElementTree as ET

import pytest

SCRIPT = pathlib.Path(__file__).resolve().parents[1] / "scripts" / "sync-templates.py"

HEAD = (
    '<Container version="2"><Name>widget</Name><Repository>example/widget:1</Repository>'
    '<TemplateURL>https://example.invalid/templates/widget.xml</TemplateURL>'
)
TOKEN = (
    '<Config Name="TOKEN" Target="TOKEN" Default="" Mode="" Description="{desc}" '
    'Type="Variable" Display="always" Required="true" Mask="true"{tail}'
)
TEMPLATE_BYTES = (
    '<Container version="2"><Name>widget</Name>'
    '<Repository>example/widget:1</Repository></Container>'
).encode()


def template(desc="TEXT"):
    return ET.fromstring(HEAD + TOKEN.format(desc=desc, tail="/>") + "</Container>")


def instance_xml(desc="TEXT", extra=""):
    return HEAD + TOKEN.format(desc=desc, tail=">s3cret</Config>") + extra + "</Container>"


@pytest.fixture()
def sync():
    spec = importlib.util.spec_from_file_location("sync_templates_fh", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mod.DRY_RUN = False
    mod.TEMPLATE = "widget"     # the repo copy is unset and refuses (unraid-templates#102)
    mod.resolve_sha = lambda: "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"     # no network: the commit is pinned per run (#103)
    return mod


@pytest.fixture()
def inst(tmp_path):
    def _make(xml, name="my-widget.xml"):
        p = tmp_path / name
        p.write_text(xml, encoding="utf-8")
        return p
    return _make


# --------------------------------------------------------- #61: the unwrapped write/read sites


def test_the_live_instance_write_that_cannot_be_written_skips_instead_of_crashing(
    sync, inst, tmp_path, monkeypatch, capsys,
):
    """The sibling to the already-fixed backup-write path: a backup was already taken (the
    operator's applied values are safe), but writing the reconciled instance itself fails."""
    p = inst(instance_xml(desc="OLD TEXT"))
    before = p.read_bytes()
    backups = tmp_path / "b"

    def no_space(path, root):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(sync, "atomic_write", no_space)
    ok = sync.update_instance(str(p), template(desc="NEW TEXT"), str(backups))

    assert ok is False, "a failed write must be reported as a failure, not swallowed"
    assert p.read_bytes() == before, "the instance must be left exactly as it was"
    assert "SKIP" in capsys.readouterr().out
    assert not list(tmp_path.glob("*.tmp")), "an orphaned .tmp was left behind"


def test_the_CREATE_write_that_cannot_be_written_is_reported_and_counted(
    sync, tmp_path, monkeypatch, capsys,
):
    """process_template's CREATE path (no my-*.xml yet) had the same unwrapped atomic_write."""
    sync.TEMPLATES_USER = str(tmp_path)

    def no_space(path, root):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(sync, "atomic_write", no_space)
    failures = sync.process_template("widget", ET.fromstring(TEMPLATE_BYTES), {}, str(tmp_path / "b"))

    assert failures == 1, "a failed CREATE must count as a failure"
    assert "FAILED to create" in capsys.readouterr().out
    assert not (tmp_path / "my-widget.xml").exists(), "no partial file was left in its place"
    assert not list(tmp_path.glob("*.tmp")), "an orphaned .tmp was left behind"


# ------------------------------------------------------------------------------------- #60


def test_a_stat_failure_on_the_base_file_is_refused_not_treated_as_absent(
    sync, tmp_path, monkeypatch, capsys,
):
    """os.path.exists() answers False on ANY OSError, not only absence. Deciding CREATE-vs-
    UPDATE on that would let a stat failure on a POPULATED instance (EACCES/EIO/a mount hiccup)
    look identical to "not created yet" and replace the operator's applied values with the bare
    template - no backup, no merge (unraid-templates#60)."""
    sync.TEMPLATES_USER = str(tmp_path)
    base = tmp_path / "my-widget.xml"
    base.write_text(instance_xml(desc="OLD TEXT"), encoding="utf-8")

    real_stat = sync.os.stat

    def flaky(path, *a, **kw):
        if str(path) == str(base):
            raise PermissionError(13, "Permission denied")
        return real_stat(path, *a, **kw)

    monkeypatch.setattr(sync.os, "stat", flaky)
    failures = sync.process_template("widget", ET.fromstring(TEMPLATE_BYTES), {}, str(tmp_path / "b"))

    assert failures == 1, "a masked stat failure must be counted, not silently treated as absent"
    assert "OLD TEXT" in base.read_text(encoding="utf-8"), (
        "the populated instance was overwritten by the bare template")
    assert "could not be checked" in capsys.readouterr().out
    assert not list(tmp_path.glob("*.tmp")), "an orphaned .tmp was left behind"


def test_a_genuinely_absent_base_file_still_creates_normally(sync, tmp_path, monkeypatch):
    """The positive direction - the new os.stat-based check must not turn an ordinary
    first-ever-sync into a false refusal."""
    sync.TEMPLATES_USER = str(tmp_path)

    failures = sync.process_template("widget", ET.fromstring(TEMPLATE_BYTES), {}, str(tmp_path / "b"))

    assert failures == 0
    assert (tmp_path / "my-widget.xml").exists()


def test_an_instance_that_cannot_be_READ_is_skipped_not_crashed(sync, tmp_path, monkeypatch, capsys):
    """update_instance's own ET.parse only caught ParseError; a real OSError (e.g. the file
    vanishing or becoming unreadable between listing and parsing) escaped the same way."""
    p = tmp_path / "my-widget.xml"
    p.write_text(instance_xml(), encoding="utf-8")

    real_parse = ET.parse

    def flaky(path, *a, **kw):
        if str(path) == str(p):
            raise OSError(13, "Permission denied")
        return real_parse(path, *a, **kw)

    monkeypatch.setattr(sync.ET, "parse", flaky)
    ok = sync.update_instance(str(p), template(), str(tmp_path / "b"))

    assert ok is False
    assert "SKIP" in capsys.readouterr().out


def test_a_prune_remove_that_fails_is_reported_not_silently_dropped(sync, tmp_path, monkeypatch):
    """The prune `os.remove` was the last unwrapped call site — a file it could not delete (a
    concurrent lock, a read-only mount) must stay counted as a FAILURE, and must stay on disk."""
    backups = tmp_path / "b"
    backups.mkdir()
    victim = "my-noisy.xml.20260101-000000.bak"
    for i in range(sync.KEEP_BACKUPS + 3):
        (backups / f"my-noisy.xml.202601{i + 2:02d}-000000.bak").write_text(
            "<Container/>", encoding="utf-8")
    (backups / victim).write_text("<Container/>", encoding="utf-8")

    real_remove = sync.os.remove

    def flaky(path):
        if victim in path:
            raise OSError(13, "Permission denied")
        return real_remove(path)

    monkeypatch.setattr(sync.os, "remove", flaky)
    dropped, failed = sync.prune_backups(str(backups))

    assert victim not in dropped, "a remove that failed must not be reported as dropped"
    assert failed and failed[0][0] == victim
    assert (backups / victim).exists(), "the file that could not be removed must remain on disk"


# ------------------------------------------------- #62: exit code, summary, and the two siblings


def test_a_run_where_every_template_is_skipped_exits_nonzero(sync, tmp_path, monkeypatch, capsys):
    """The regression: every skip branch used to reach the same 'done.' with an implicit exit 0
    — a wrapper, a schedule, or a person skimming the tail all saw success."""
    sync.TEMPLATES_USER = str(tmp_path)
    monkeypatch.setattr(sync, "list_repo_templates", lambda sha: ["widget"])

    def boom(name, sha):
        raise RuntimeError("network down")

    monkeypatch.setattr(sync, "fetch_template", boom)

    with pytest.raises(SystemExit) as exc:
        sync.main()

    assert exc.value.code == 1, "a fully-skipped run must not exit 0"
    out = capsys.readouterr().out
    assert "failure" in out, "the run must say WHY it failed, not just 'done.'"


def test_a_clean_run_still_exits_zero(sync, tmp_path, monkeypatch):
    """The positive direction — the new counter must not turn a genuinely clean run red."""
    sync.TEMPLATES_USER = str(tmp_path)
    monkeypatch.setattr(sync, "list_repo_templates", lambda sha: ["widget"])
    monkeypatch.setattr(sync, "fetch_template", lambda name, sha: TEMPLATE_BYTES)

    sync.main()  # must return normally - no SystemExit

    assert (tmp_path / "my-widget.xml").exists()


def test_main_exits_nonzero_on_an_unreadable_pre_existing_backup_alone(
    sync, tmp_path, monkeypatch,
):
    """Every contributor to `failures` in main() must actually move the exit code — not just
    the process_template one exercised above. This one is an unparseable pre-existing .bak,
    with everything else in the run clean."""
    sync.TEMPLATES_USER = str(tmp_path)
    monkeypatch.setattr(sync, "list_repo_templates", lambda sha: ["widget"])
    monkeypatch.setattr(sync, "fetch_template", lambda name, sha: TEMPLATE_BYTES)
    backups = tmp_path / ".template-sync-backups"
    backups.mkdir()
    (backups / "my-widget.xml.20260101-000000.bak").write_text(
        "<Container><Name>x</Name>", encoding="utf-8")  # truncated: unparseable

    with pytest.raises(SystemExit) as exc:
        sync.main()

    assert exc.value.code == 1, "an unreadable pre-existing backup alone must fail the run"


def test_main_exits_nonzero_on_a_broken_instance_alone(sync, tmp_path, monkeypatch):
    """Same, for the `broken` bucket — one of our own corrupt instances, everything else clean.

    ⚠️ A confirming round found this test was vacuous: naming the corrupt file `my-widget.xml`
    made it collide with the "widget" template's own base_path, so `process_template` ALSO
    counted a failure on it via `update_instance`'s independent ET.parse — deleting the
    `failures += len(broken)` line in main() still left this test green. The filename here
    (`my-orphan.xml`) deliberately matches no repo template by TemplateURL OR naming fallback,
    so `broken` is the ONLY contributor a run over it can produce.
    """
    sync.TEMPLATES_USER = str(tmp_path)
    monkeypatch.setattr(sync, "list_repo_templates", lambda sha: ["widget"])
    monkeypatch.setattr(sync, "fetch_template", lambda name, sha: TEMPLATE_BYTES)
    (tmp_path / "my-orphan.xml").write_text("<Container><Name>x</Name>", encoding="utf-8")

    with pytest.raises(SystemExit) as exc:
        sync.main()

    assert exc.value.code == 1, "a broken instance of ours alone must fail the run"
    assert (tmp_path / "my-widget.xml").exists(), (
        "the unrelated template's own CREATE must still have succeeded")


def test_main_exits_nonzero_on_a_prune_failure_alone(sync, tmp_path, monkeypatch):
    """Same, for `prune_failed` — every template/instance clean, only the prune step fails."""
    sync.TEMPLATES_USER = str(tmp_path)
    monkeypatch.setattr(sync, "list_repo_templates", lambda sha: ["widget"])
    monkeypatch.setattr(sync, "fetch_template", lambda name, sha: TEMPLATE_BYTES)
    backups = tmp_path / ".template-sync-backups"
    backups.mkdir()
    victim = "my-widget.xml.20260101-000000.bak"
    for i in range(sync.KEEP_BACKUPS + 3):
        (backups / f"my-widget.xml.202601{i + 2:02d}-000000.bak").write_text(
            "<Container/>", encoding="utf-8")
    (backups / victim).write_text("<Container/>", encoding="utf-8")

    real_remove = sync.os.remove

    def flaky(path):
        if victim in path:
            raise OSError(13, "Permission denied")
        return real_remove(path)

    monkeypatch.setattr(sync.os, "remove", flaky)

    with pytest.raises(SystemExit) as exc:
        sync.main()

    assert exc.value.code == 1, "a prune failure alone must fail the run"
    assert (backups / victim).exists()


# --------------------------------------------------- the two siblings _backup_files fed unwrapped


def test_redact_existing_backups_reports_an_unlistable_dir_instead_of_crashing(
    sync, tmp_path, monkeypatch,
):
    """`_backup_files`' own `os.listdir` was unwrapped, feeding BOTH redact_existing_backups and
    prune_backups — the same crash-mid-loop shape #61 fixed at every other call site."""
    backups = tmp_path / "b"
    backups.mkdir()

    def flaky(path):
        raise OSError(13, "Permission denied")

    monkeypatch.setattr(sync.os, "listdir", flaky)
    files, values, unreadable = sync.redact_existing_backups(str(backups))

    assert (files, values) == (0, 0)
    assert unreadable, "a directory that cannot be listed must be reported, not silently empty"


def test_prune_backups_reports_an_unlistable_dir_instead_of_crashing(sync, tmp_path, monkeypatch):
    backups = tmp_path / "b"
    backups.mkdir()

    def flaky(path):
        raise OSError(13, "Permission denied")

    monkeypatch.setattr(sync.os, "listdir", flaky)
    dropped, failed = sync.prune_backups(str(backups))

    assert dropped == []
    assert failed, "a directory that cannot be listed must be reported, not silently empty"


def test_discover_instances_reports_an_unlistable_dir_instead_of_crashing(sync, tmp_path):
    """discover_instances' own `os.listdir` had the same exposure as the two above."""
    import stat as stat_mod

    locked = tmp_path / "locked"
    locked.mkdir()
    # A directory with no read/execute permission raises PermissionError on listdir; on some
    # CI runners root/administrator bypasses this, so fall back to a monkeypatch if it does.
    try:
        locked.chmod(0o000)
        by_template, unmapped, broken = sync.discover_instances(str(locked), ["widget"])
    finally:
        locked.chmod(stat_mod.S_IRWXU)
    if not broken:
        pytest.skip("this environment does not enforce directory permissions (likely root)")
    assert by_template == {} and unmapped == []


def test_map_instance_reports_a_read_error_separately_from_a_genuine_foreign_file(
    sync, tmp_path,
):
    """map_instance used to swallow ANY exception the same way it swallowed a merely-empty
    TemplateURL, so one of OUR OWN corrupt instances — unreadable, and matching no fallback name
    either — fell through to 'foreign / not from these templates'. It is ours, and broken."""
    corrupt = tmp_path / "my-widget.xml"
    corrupt.write_text("<Container><Name>x</Name>", encoding="utf-8")  # truncated, unparseable

    t, error = sync.map_instance(str(corrupt), "my-widget.xml", ["not-widget"])

    assert t is None
    assert error is not None, "a read/parse failure must be reported, not treated as a naming miss"


def test_discover_instances_separates_broken_from_genuinely_unmapped(sync, tmp_path):
    corrupt = tmp_path / "my-widget.xml"
    corrupt.write_text("<Container><Name>x</Name>", encoding="utf-8")
    foreign = tmp_path / "my-something-else.xml"
    foreign.write_text(HEAD + "</Container>", encoding="utf-8")  # parses fine, matches nothing

    by_template, unmapped, broken = sync.discover_instances(str(tmp_path), ["not-widget"])

    assert by_template == {}
    assert unmapped == ["my-something-else.xml"], unmapped
    assert len(broken) == 1 and broken[0][0] == "my-widget.xml", broken


def test_map_instance_still_matches_by_naming_fallback_with_no_flag_to_pass(sync, tmp_path):
    """The dead `naming_fallback=False` parameter is gone (it was never called with False —
    grepped the whole tree); the fallback behaviour itself is unchanged."""
    p = tmp_path / "my-tape-dev.xml"
    p.write_text(HEAD + "</Container>", encoding="utf-8")
    p.write_text('<Container><Name>x</Name></Container>', encoding="utf-8")  # no TemplateURL

    t, error = sync.map_instance(str(p), "my-tape-dev.xml", ["tape", "tape-db"])

    assert t == "tape", "the longest dash-prefix match must still win"
    assert error is None
