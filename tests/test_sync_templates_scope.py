"""`sync-templates.py` `TEMPLATE` scope: one installed User Script per repo template.

A run over every managed template would ship every other template's merged-but-not-yet-intended
changes along with the one being synced. `TEMPLATE` narrows a run to one repo
template and its live instances. These tests pin the four things that matter:

1. `TEMPLATE = None` (the repo copy) refuses before anything is read or written: the run over
   every template is retired (unraid-templates#102).
2. A scoped run writes nothing outside its own template — including the prefix trap: `tape` and
   `tape-db` are separate templates, and `my-tape-db-dev.xml` belongs to `tape-db` only.
3. A name the repo does not have is refused before anything is written.
4. The output names the scope, so a dry run shows it before a live one acts on it.
"""

import datetime as _dt
import importlib.util
import os
import pathlib

import pytest

SCRIPT = pathlib.Path(__file__).resolve().parents[1] / "scripts" / "sync-templates.py"


def cfg(name, value="", default="", masked=False, desc="x"):
    return (f'<Config Name="{name}" Target="{name}" Default="{default}" Mode="" '
            f'Description="{desc}" Type="Variable" Display="always" Required="false" '
            f'Mask="{"true" if masked else "false"}">{value}</Config>')


def container(name, configs, tpl=None, env=""):
    url = f"<TemplateURL>https://example.invalid/templates/{tpl}.xml</TemplateURL>" if tpl else ""
    return (f'<Container version="2"><Name>{name}</Name><Repository>example/{name}:1</Repository>'
            f'{url}{"".join(configs)}{env}</Container>')


def mirror(name, value):
    return f"<Environment><Variable><Value>{value}</Value><Name>{name}</Name></Variable></Environment>"


REPO = {
    "tape": container("tape", [cfg("TOKEN", masked=True, desc="token"),
                               cfg("PORT", default="8080", desc="port"),
                               cfg("NEWVAR", default="n", desc="new")], tpl="tape"),
    "tape-db": container("tape-db", [cfg("DB_PASS", masked=True, desc="db password, new text"),
                                     cfg("DB_NAME", default="tape", desc="db")], tpl="tape-db"),
    "widget": container("widget", [cfg("W", default="1")], tpl="widget"),
}

INSTANCES = {
    # TemplateURL mapping; one var added, one blank var dropped, one valued var dropped
    "my-tape.xml": container("tape", [cfg("TOKEN", "s3cret", masked=True, desc="old"),
                                      cfg("PORT", "9090", default="8080", desc="port"),
                                      cfg("OLDVAR", "", desc="gone"),
                                      cfg("DEPRECATED", "applied", desc="retired")],
                             tpl="tape", env=mirror("TOKEN", "s3cret")),
    # no TemplateURL: filename fallback
    "my-tape-dev.xml": container("tape-dev", [cfg("TOKEN", "dev-s3cret", masked=True, desc="old"),
                                              cfg("PORT", "8080", default="8080", desc="port")]),
    "my-tape-db.xml": container("tape-db", [cfg("DB_PASS", "pw1", masked=True, desc="old text"),
                                            cfg("DB_NAME", "tape", default="tape", desc="db")],
                                tpl="tape-db"),
    # the prefix trap: no TemplateURL, so only the longest dash-prefix keeps this off `tape`
    "my-tape-db-dev.xml": container("tape-db-dev", [cfg("DB_PASS", "pw2", masked=True, desc="old text"),
                                                    cfg("DB_NAME", "tape_dev", default="tape", desc="db")]),
    "my-foreign.xml": container("foreign", [cfg("X", "1")]),
}


def world(root, instances=True):
    """A templates-user dir: instances of both prefix-sharing templates, and a foreign file.
    Backups are not here: they go to the host's delete folder, not the flash."""
    if instances:
        for fname, xml in INSTANCES.items():
            (root / fname).write_bytes(xml.encode())


class Frozen(_dt.datetime):
    """Backup names carry a timestamp; freeze it so a snapshot is reproducible."""
    @classmethod
    def now(cls, tz=None):
        return cls(2026, 1, 2, 3, 4, 5)


@pytest.fixture()
def sync(monkeypatch):
    spec = importlib.util.spec_from_file_location("sync_templates_scope", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mod.DRY_RUN = False     # pinned: the README invites flipping it
    mod.resolve_sha = lambda: "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"     # no network: the commit is pinned per run (#103)
    monkeypatch.setattr(mod, "list_repo_templates", lambda sha: sorted(REPO))
    monkeypatch.setattr(mod, "fetch_template", lambda name, sha: REPO[name].encode())
    monkeypatch.setattr(mod, "datetime", Frozen)
    return mod


def snap(root):
    return {p.relative_to(root).as_posix(): p.read_bytes()
            for p in sorted(root.rglob("*")) if p.is_file()}


def run(sync, root, capsys):
    sync.TEMPLATES_USER = str(root)
    code = 0
    try:
        sync.main()
    except SystemExit as e:
        code = e.code
    captured = capsys.readouterr()
    return code, captured.out.replace(str(root), "<DIR>"), captured.err


def owned_by(path, instances):
    """Is `path` (relative to templates-user) one of `instances` or a backup of one?

    Deliberately NOT the script's own attribution rule: every backup in world() is
    `<instance>.<one-dot-free-suffix>.bak`, so its instance is exactly what precedes those two
    segments."""
    base = path.split("/")[-1]
    return (base if base.endswith(".xml") else base.rsplit(".", 2)[0]) in instances


# ------------------------------------------------------------- 1. unset = refuse (no full pass)

@pytest.mark.parametrize("body", ["tape-db instance", "foreign"])
def test_a_case_variant_stub_that_is_not_this_templates_is_never_rewritten(sync, tmp_path, capsys, body):
    """/boot is FAT32: there `my-Tape.xml` IS `my-tape.xml`, so a name comparison cannot see
    that the stub a `tape` run would update is really a tape-db (or foreign) container."""
    world(tmp_path)
    (tmp_path / "my-tape.xml").unlink()
    content = {"tape-db instance": INSTANCES["my-tape-db.xml"],
               "foreign": container("Tape", [cfg("X", "1")])}[body].encode()
    (tmp_path / "my-Tape.xml").write_bytes(content)
    case_insensitive = (tmp_path / "my-tape.xml").exists()
    sync.TEMPLATE = "tape"
    code, out, _ = run(sync, tmp_path, capsys)
    assert (tmp_path / "my-Tape.xml").read_bytes() == content
    if case_insensitive:
        assert isinstance(code, str) and "my-Tape.xml (the file at my-tape.xml)" in code, code
    else:
        assert code == 0, out   # a separate file: the tape stub is simply CREATEd beside it


def test_the_stub_refusal_compares_files_not_names_on_any_filesystem(sync, tmp_path, capsys):
    """The same alias as FAT32's case folding, built from a hard link so a case-sensitive CI
    runner exercises it too: my-tape.xml IS foreign my-plex.xml."""
    world(tmp_path)
    (tmp_path / "my-tape.xml").unlink()
    content = container("plex", [cfg("X", "1")]).encode()
    (tmp_path / "my-plex.xml").write_bytes(content)
    try:
        os.link(tmp_path / "my-plex.xml", tmp_path / "my-tape.xml")
    except (OSError, NotImplementedError) as e:
        pytest.skip(f"no hard links here: {e}")
    before = snap(tmp_path)
    sync.TEMPLATE = "tape"
    code, _out, _ = run(sync, tmp_path, capsys)
    assert isinstance(code, str) and "is not from these templates" in code, code
    assert snap(tmp_path) == before


@pytest.mark.parametrize("failures", [1, 99])
def test_a_scoped_run_refuses_when_it_cannot_list_the_templates_dir(
        sync, tmp_path, capsys, monkeypatch, failures):
    """`failures=1`: only the FIRST listing fails, which is the one the scope is built from —
    a second look that happens to succeed must not stand in for it. The stub here is a hard
    link to a foreign container, so a run that carried on would rewrite it."""
    world(tmp_path)
    (tmp_path / "my-tape.xml").unlink()
    (tmp_path / "my-plex.xml").write_bytes(container("plex", [cfg("X", "1")]).encode())
    try:
        os.link(tmp_path / "my-plex.xml", tmp_path / "my-tape.xml")
    except (OSError, NotImplementedError) as e:
        pytest.skip(f"no hard links here: {e}")
    before = snap(tmp_path)
    real, left = os.listdir, [failures]

    def flaky(path="."):
        if os.path.normcase(str(path)) == os.path.normcase(str(tmp_path)) and left[0]:
            left[0] -= 1
            raise PermissionError(13, "Permission denied")
        return real(path)

    monkeypatch.setattr(sync.os, "listdir", flaky)
    sync.TEMPLATE = "tape"
    code, _out, _ = run(sync, tmp_path, capsys)
    monkeypatch.undo()
    assert isinstance(code, str) and "could not list the directory" in code, code
    assert "cannot tell its own instances apart" in code and "Nothing changed" in code
    assert snap(tmp_path) == before


# ------------------------------------------- 6. a base stub is seeded only on a host with no instance

@pytest.mark.parametrize("dry", [False, True])
@pytest.mark.parametrize("url", [True, False])
def test_a_host_with_only_a_dev_instance_gets_no_base_stub(sync, tmp_path, capsys, dry, url):
    """A dev host runs my-tape-dev.xml only; the operator removed my-tape.xml on purpose. The
    -dev file counts whether it maps by TemplateURL or by its name."""
    world(tmp_path, instances=False)
    dev = container("tape-dev", [cfg("TOKEN", "dev-s3cret", masked=True)], tpl="tape" if url else None)
    (tmp_path / "my-tape-dev.xml").write_bytes(dev.encode())
    sync.TEMPLATE = "tape"
    sync.DRY_RUN = dry
    code, out, _ = run(sync, tmp_path, capsys)
    assert code == 0, out
    assert not (tmp_path / "my-tape.xml").exists()
    assert "CREATE" not in out
    assert ("my-tape.xml         seed SKIPPED  (this host already has my-tape-dev.xml; "
            "a base stub is seeded only where no instance of 'tape' exists)") in out
    assert ("would UPDATE" if dry else "UPDATED") in out.split("seed SKIPPED")[1]


@pytest.mark.parametrize("dry", [False, True])
def test_a_host_with_no_instance_still_gets_the_base_stub(sync, tmp_path, capsys, dry):
    """A fresh host: another template's instances do not count against tape's stub."""
    world(tmp_path, instances=False)
    (tmp_path / "my-tape-db-dev.xml").write_bytes(INSTANCES["my-tape-db-dev.xml"].encode())
    sync.TEMPLATE = "tape"
    sync.DRY_RUN = dry
    code, out, _ = run(sync, tmp_path, capsys)
    assert code == 0, out
    assert "seed SKIPPED" not in out
    assert ("would CREATE" if dry else "CREATED") in out
    assert (tmp_path / "my-tape.xml").exists() is not dry


def test_a_host_with_both_updates_both_and_seeds_nothing(sync, tmp_path, capsys):
    world(tmp_path)
    before = snap(tmp_path)
    sync.TEMPLATE = "tape"
    code, out, _ = run(sync, tmp_path, capsys)
    assert code == 0, out
    assert "CREATE" not in out and "seed SKIPPED" not in out
    for inst in ("my-tape.xml", "my-tape-dev.xml"):
        assert f"{inst:<22} UPDATED" in out and snap(tmp_path)[inst] != before[inst]


@pytest.mark.parametrize("body", ["tape-db instance", "foreign"])
def test_a_case_variant_or_foreign_base_with_a_dev_instance_is_still_refused(
        sync, tmp_path, capsys, body):
    """The skip does not soften the stub refusal: a my-tape.xml that is really another
    template's or a foreign container is refused as before, -dev instance or not."""
    world(tmp_path)
    (tmp_path / "my-tape.xml").unlink()
    content = {"tape-db instance": INSTANCES["my-tape-db.xml"],
               "foreign": container("Tape", [cfg("X", "1")])}[body].encode()
    (tmp_path / "my-Tape.xml").write_bytes(content)
    if not (tmp_path / "my-tape.xml").exists():      # case-sensitive here: alias it by hard link
        try:
            os.link(tmp_path / "my-Tape.xml", tmp_path / "my-tape.xml")
        except (OSError, NotImplementedError) as e:
            pytest.skip(f"no hard links here: {e}")
    before = snap(tmp_path)
    sync.TEMPLATE = "tape"
    code, _out, _ = run(sync, tmp_path, capsys)
    why ="maps to 'tape-db'" if body == "tape-db instance" else "is not from these templates"
    assert isinstance(code, str) and why in code and "Nothing changed" in code, code
    assert snap(tmp_path) == before


def test_a_scoped_run_still_reports_an_unreadable_instance_it_cannot_attribute(sync, tmp_path, capsys):
    """Unreadable and unmappable, so it may be this template's (its TemplateURL cannot be read):
    every scoped run reports it and exits non-zero rather than assume it is someone else's."""
    world(tmp_path)
    (tmp_path / "my-zzz.xml").write_bytes(b"<Container><unclosed>")
    sync.TEMPLATE = "tape"
    code, out, _ = run(sync, tmp_path, capsys)
    assert code == 1
    assert "my-zzz.xml" in out and "could not be read" in out


@pytest.fixture(autouse=True)
def _backups_go_to_the_host_not_the_flash(sync, tmp_path_factory):
    """BACKUP_DIR is the host's delete folder: a folder outside the templates tree, never the flash."""
    sync.BACKUP_DIR = str(tmp_path_factory.mktemp("host-delete"))
