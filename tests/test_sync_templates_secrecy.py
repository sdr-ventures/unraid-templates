"""`sync-templates.py`: which values a backup treats as secret (unraid-templates#104).

A value is secret when the live file OR the repo template masks its variable, in the <Config> and
in dockerMan's legacy <Environment><Variable> mirror alike. That holds for the backup a run takes
and for the pass over backups earlier runs left behind. A mirror entry with no template Target is
dropped from the live file with its Config, and a mirror with no Config at all is redacted in a
backup, since nothing says it is safe.

Every value here is synthetic.
"""

import importlib.util
import pathlib
import xml.etree.ElementTree as ET

import pytest

SCRIPT = pathlib.Path(__file__).resolve().parents[1] / "scripts" / "sync-templates.py"
BACKUPS = ".template-sync-backups"
SECRET = "synthetic-secret-value"


def cfg(name, value="", masked=False):
    return (f'<Config Name="{name}" Target="{name}" Default="" Mode="" Description="x" '
            f'Type="Variable" Display="always" Required="false" '
            f'Mask="{"true" if masked else "false"}">{value}</Config>')


def mirror(**values):
    return ("<Environment>" + "".join(
        f"<Variable><Value>{v}</Value><Name>{k}</Name></Variable>" for k, v in values.items())
        + "</Environment>")


def container(configs, env="", name="app"):
    return (f'<Container version="2"><Name>{name}</Name><Repository>example/{name}:1</Repository>'
            f'<TemplateURL>https://example.invalid/templates/{name}.xml</TemplateURL>'
            f'{"".join(configs)}{env}</Container>')


# The repo template masks TOKEN; PLAIN is an ordinary variable.
REPO = {
    "app": container([cfg("TOKEN", masked=True), cfg("PLAIN")]),
    "other": container([cfg("OTHER_TOKEN")], name="other"),
}


@pytest.fixture()
def sync(monkeypatch):
    spec = importlib.util.spec_from_file_location("sync_templates_secrecy", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mod.DRY_RUN = False
    mod.TEMPLATE = "app"
    mod.resolve_sha = lambda: "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"     # no network: the commit is pinned per run (#103)
    monkeypatch.setattr(mod, "list_repo_templates", lambda sha: sorted(REPO))
    monkeypatch.setattr(mod, "fetch_template", lambda name, sha: REPO[name].encode())
    return mod


def run(sync, root, capsys):
    sync.TEMPLATES_USER = str(root)
    code = 0
    try:
        sync.main()
    except SystemExit as e:
        code = e.code
    return code, capsys.readouterr().out


def backups(root):
    d = root / BACKUPS
    return {p.name: p.read_text(encoding="utf-8") for p in d.iterdir()} if d.is_dir() else {}


# ------------------------------------------------------------- the issue's repro, through a run

def test_the_issue_repro_a_live_Mask_false_secret_is_redacted_when_the_template_masks_it(
        sync, tmp_path, capsys):
    """#104's repro: the live Config holds the secret with Mask="false"; the repo template masks
    TOKEN. Before the fix the backup held the value and the run counted 0 redactions."""
    (tmp_path / "my-app.xml").write_text(
        container([cfg("TOKEN", SECRET, masked=False), cfg("PLAIN", "kept")],
                  env=mirror(TOKEN=SECRET, PLAIN="kept")), encoding="utf-8")
    code, out = run(sync, tmp_path, capsys)
    assert code == 0, out
    (bak,) = backups(tmp_path).values()
    assert SECRET not in bak, "the backup holds the secret the repo template masks"
    assert bak.count("***REDACTED***") == 2, "Config AND its mirror"
    assert "2 masked value(s) REDACTED" in out, "the count must include it"
    assert bak.count("kept") == 2, "a variable neither side masks keeps its value"
    # the live file keeps its applied value; only the backup is redacted
    assert SECRET in (tmp_path / "my-app.xml").read_text(encoding="utf-8")


@pytest.mark.parametrize("mask_attr", ['Mask="false"', ""])
def test_backup_with_the_templates_masks_redacts_config_and_mirror(sync, tmp_path, mask_attr):
    """The function the issue named, called the way the run calls it: an absent Mask is the same
    as Mask="false", and the template's set covers both halves."""
    inst = tmp_path / "my-app.xml"
    inst.write_text(container([cfg("TOKEN", SECRET).replace('Mask="false"', mask_attr)],
                              env=mirror(TOKEN=SECRET)), encoding="utf-8")
    dest, n = sync.backup(str(inst), str(tmp_path / "bk"), {"TOKEN"})
    assert n == 2
    assert SECRET not in pathlib.Path(dest).read_text(encoding="utf-8")


def test_only_the_live_file_masks_it_still_redacted(sync, tmp_path, capsys):
    """The other direction: the file's own Mask still counts when the template's does not."""
    (tmp_path / "my-app.xml").write_text(
        container([cfg("TOKEN", "t"), cfg("PLAIN", SECRET, masked=True)],
                  env=mirror(TOKEN="t", PLAIN=SECRET)), encoding="utf-8")
    code, out = run(sync, tmp_path, capsys)
    assert code == 0, out
    (bak,) = backups(tmp_path).values()
    assert SECRET not in bak


# --------------------------------------------------------------- backups earlier runs left

def test_the_existing_backups_pass_redacts_by_the_templates_masks_too(sync, tmp_path, capsys):
    """An old backup taken while the live Config was unmasked holds the secret in clear, in both
    halves. The scoped run's pass over its own backups redacts it; another template's backup is
    not this run's and is left alone."""
    (tmp_path / "my-app.xml").write_text(
        container([cfg("TOKEN", "t", masked=True), cfg("PLAIN")]), encoding="utf-8")
    (tmp_path / "my-other.xml").write_text(
        container([cfg("OTHER_TOKEN", "o")], name="other"), encoding="utf-8")
    b = tmp_path / BACKUPS
    b.mkdir()
    old = container([cfg("TOKEN", SECRET), cfg("PLAIN", "kept")], env=mirror(TOKEN=SECRET, PLAIN="kept"))
    (b / "my-app.xml.20200101-000000.bak").write_text(old, encoding="utf-8")
    foreign = container([cfg("TOKEN", "not-this-runs")], name="other")
    (b / "my-other.xml.20200101-000000.bak").write_text(foreign, encoding="utf-8")

    code, out = run(sync, tmp_path, capsys)
    assert code == 0, out
    assert "REDACTED 2 masked value(s) across 1 pre-existing backup(s)" in out
    got = backups(tmp_path)
    assert SECRET not in got["my-app.xml.20200101-000000.bak"]
    assert got["my-app.xml.20200101-000000.bak"].count("kept") == 2
    assert got["my-other.xml.20200101-000000.bak"] == foreign

    capsys.readouterr()
    code, out = run(sync, tmp_path, capsys)
    assert "pre-existing backup" not in out, "the pass must be idempotent"


def test_a_dry_run_reports_the_template_masked_backup_and_writes_nothing(sync, tmp_path, capsys):
    b = tmp_path / BACKUPS
    b.mkdir()
    old = container([cfg("TOKEN", SECRET)], env=mirror(TOKEN=SECRET))
    (b / "my-app.xml.20200101-000000.bak").write_text(old, encoding="utf-8")
    sync.DRY_RUN = True
    code, out = run(sync, tmp_path, capsys)
    assert code == 0, out
    assert "would redact 2 masked value(s) across 1 pre-existing backup(s)" in out
    assert backups(tmp_path)["my-app.xml.20200101-000000.bak"] == old


def test_a_template_that_cannot_load_still_runs_the_pass_on_the_files_own_masks(
        sync, tmp_path, capsys, monkeypatch):
    b = tmp_path / BACKUPS
    b.mkdir()
    (b / "my-app.xml.20200101-000000.bak").write_text(
        container([cfg("TOKEN", SECRET, masked=True)]), encoding="utf-8")

    def down(name, sha):
        raise OSError("network down")

    monkeypatch.setattr(sync, "fetch_template", down)
    code, out = run(sync, tmp_path, capsys)
    assert code == 1 and "could not fetch/parse repo template" in out, out
    assert SECRET not in backups(tmp_path)["my-app.xml.20200101-000000.bak"]


# -------------------------------------------- the legacy mirror of a dropped variable (RULE-2 #1)

def test_a_dropped_masked_variable_leaves_no_mirror_behind(sync, tmp_path, capsys):
    """The template no longer has OLD_TOKEN. The sync drops its Config; its mirror used to stay in
    the live file, where no Config marked it secret any more."""
    (tmp_path / "my-app.xml").write_text(
        container([cfg("TOKEN", "t", masked=True), cfg("OLD_TOKEN", SECRET, masked=True)],
                  env=mirror(TOKEN="t", OLD_TOKEN=SECRET)), encoding="utf-8")
    code, out = run(sync, tmp_path, capsys)
    assert code == 0, out
    live = (tmp_path / "my-app.xml").read_text(encoding="utf-8")
    assert SECRET not in live and "OLD_TOKEN" not in live
    assert "<Name>TOKEN</Name>" in live, "a mirror the template still has stays"
    assert "DROPPED legacy <Environment> mirror, not in repo template: OLD_TOKEN" in out
    (bak,) = backups(tmp_path).values()
    assert SECRET not in bak


def test_a_mirror_whose_config_an_earlier_sync_dropped_is_redacted_and_pruned(sync, tmp_path, capsys):
    """The state an earlier sync left: OLD_TOKEN's Config is gone, its mirror is not, and nothing
    marks it secret. This run's backup must not copy it in clear, and the live file loses it."""
    (tmp_path / "my-app.xml").write_text(
        container([cfg("TOKEN", "t", masked=True), cfg("PLAIN", "kept")],
                  env=mirror(TOKEN="t", PLAIN="kept", OLD_TOKEN=SECRET)), encoding="utf-8")
    b = tmp_path / BACKUPS
    b.mkdir()
    (b / "my-app.xml.20200101-000000.bak").write_text(
        container([cfg("PLAIN", "kept")], env=mirror(PLAIN="kept", OLD_TOKEN=SECRET)), encoding="utf-8")
    code, out = run(sync, tmp_path, capsys)
    assert code == 0, out
    assert SECRET not in (tmp_path / "my-app.xml").read_text(encoding="utf-8")
    assert not any(SECRET in v for v in backups(tmp_path).values()), "a backup kept the orphan mirror"
    assert all(v.count("kept") == 2 for v in backups(tmp_path).values()), "PLAIN over-redacted"


def test_a_mirror_the_template_still_has_is_never_pruned_from_the_live_file(sync, tmp_path, capsys):
    text = container([cfg("TOKEN", "t", masked=True), cfg("PLAIN", "kept")],
                     env=mirror(TOKEN="t", PLAIN="kept"))
    (tmp_path / "my-app.xml").write_text(text, encoding="utf-8")
    code, out = run(sync, tmp_path, capsys)   # the first run re-serialises (Configs move last)
    assert code == 0, out
    assert "DROPPED" not in out, out
    live = (tmp_path / "my-app.xml").read_text(encoding="utf-8")
    for name, value in (("TOKEN", "t"), ("PLAIN", "kept")):
        assert f"<Value>{value}</Value>" in live and f"<Name>{name}</Name>" in live
    code, out = run(sync, tmp_path, capsys)
    assert "up to date" in out, out


@pytest.mark.parametrize("env", ["", "<Environment/>", "<Environment></Environment>"])
def test_an_empty_or_absent_mirror_is_left_as_it_is(sync, env):
    """The prune only removes Variables: it never adds, removes or fills an <Environment>."""
    op = ET.fromstring(container([cfg("TOKEN", "t", masked=True)], env=env))
    merged, st = sync.merge(op, ET.fromstring(REPO["app"]))
    assert st["mirror_dropped"] == []
    envs = merged.findall("Environment")
    assert len(envs) == (1 if env else 0)
    assert all(len(e) == 0 and not (e.text or "").strip() for e in envs)


# ------------------------------------------------------------------- edges of matching by name

def test_a_padded_live_Target_still_matches_the_templates_mask(sync, tmp_path, capsys):
    (tmp_path / "my-app.xml").write_text(
        container([cfg("TOKEN", SECRET).replace('Target="TOKEN"', 'Target=" TOKEN "')]),
        encoding="utf-8")
    code, out = run(sync, tmp_path, capsys)
    assert code == 0, out
    assert not any(SECRET in v for v in backups(tmp_path).values())


def test_a_Config_with_no_Target_matches_the_templates_mask_by_Name(sync, tmp_path, capsys, monkeypatch):
    def no_target(xml):
        return xml.replace(' Target="TOKEN"', "")
    monkeypatch.setitem(REPO, "app", no_target(container([cfg("TOKEN", masked=True)])))
    (tmp_path / "my-app.xml").write_text(no_target(container([cfg("TOKEN", SECRET)])), encoding="utf-8")
    code, out = run(sync, tmp_path, capsys)
    assert code == 0, out
    (bak,) = backups(tmp_path).values()
    assert SECRET not in bak


def test_a_padded_mirror_Name_the_template_has_is_not_pruned(sync, tmp_path, capsys):
    (tmp_path / "my-app.xml").write_text(
        container([cfg("TOKEN", "t", masked=True), cfg("PLAIN", "kept")],
                  env="<Environment><Variable><Value>mirror-kept</Value><Name> PLAIN </Name>"
                      "</Variable></Environment>"), encoding="utf-8")
    code, out = run(sync, tmp_path, capsys)
    assert code == 0, out
    assert "DROPPED" not in out, out
    assert "mirror-kept" in (tmp_path / "my-app.xml").read_text(encoding="utf-8")


def test_a_mirror_only_drop_is_not_reported_as_metadata_only(sync, tmp_path, capsys):
    (tmp_path / "my-app.xml").write_text(
        container([cfg("TOKEN", "t", masked=True), cfg("PLAIN", "kept")],
                  env=mirror(TOKEN="t", PLAIN="kept", GONE="x")), encoding="utf-8")
    run(sync, tmp_path, capsys)                                  # first run also re-serialises
    (tmp_path / "my-app.xml").write_text(
        (tmp_path / "my-app.xml").read_text(encoding="utf-8").replace(
            "</Environment>", "<Variable><Value>x</Value><Name>GONE</Name></Variable></Environment>"),
        encoding="utf-8")
    code, out = run(sync, tmp_path, capsys)
    assert code == 0, out
    assert "DROPPED legacy <Environment> mirror, not in repo template: GONE" in out
    assert "no variables added or removed" not in out, out
