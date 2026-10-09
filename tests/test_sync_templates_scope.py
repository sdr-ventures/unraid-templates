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
BACKUPS = ".template-sync-backups"


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
    """A templates-user dir: instances of both prefix-sharing templates, a foreign file, and a
    backup dir holding work for BOTH housekeeping steps on BOTH templates — more than
    KEEP_BACKUPS stamped backups each (prune) and a cleartext backup each (redact)."""
    if instances:
        for fname, xml in INSTANCES.items():
            (root / fname).write_bytes(xml.encode())
    b = root / BACKUPS
    b.mkdir()
    for i in range(11):
        (b / f"my-tape-dev.xml.20200101-0000{i:02d}.bak").write_bytes(
            container("tape-dev", [cfg("TOKEN", "***REDACTED***", masked=True)]).encode())
        (b / f"my-tape-db.xml.20200101-0000{i:02d}.bak").write_bytes(
            container("tape-db", [cfg("DB_PASS", "***REDACTED***", masked=True)]).encode())
    (b / "my-tape.xml.20190101-000000.bak").write_bytes(
        container("tape", [cfg("TOKEN", "old-s3cret", masked=True)]).encode())
    (b / "my-tape-db.xml.BEFORE-MIGRATION.bak").write_bytes(
        container("tape-db", [cfg("DB_PASS", "hand-s3cret", masked=True)]).encode())


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
    monkeypatch.setattr(mod, "list_repo_templates", lambda: sorted(REPO))
    monkeypatch.setattr(mod, "fetch_template", lambda name: REPO[name].encode())
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

@pytest.mark.parametrize("dry", [False, True])
def test_the_committed_copy_is_unset_and_refuses_before_reading_anything(
        sync, tmp_path, capsys, monkeypatch, dry):
    """unraid-templates#102: the run over every template reconciled a my-<name>.xml against two
    templates. It is retired: the repo copy (TEMPLATE = None) refuses, and does so before the
    network listing or the backup redaction, so it cannot write or report anything else."""
    assert sync.TEMPLATE is None
    world(tmp_path)
    before = snap(tmp_path)
    monkeypatch.setattr(sync, "list_repo_templates",
                        lambda: pytest.fail("the unset copy must refuse before the listing"))
    sync.DRY_RUN = dry
    code, out, _ = run(sync, tmp_path, capsys)
    assert isinstance(code, str) and "TEMPLATE is not set" in code and "Nothing changed" in code
    assert out == ""
    assert snap(tmp_path) == before


# ------------------------------------------------------ 2. scoped = that template, nothing else

SCOPES = {
    "tape": ["my-tape.xml", "my-tape-dev.xml"],
    "tape-db": ["my-tape-db.xml", "my-tape-db-dev.xml"],
}


@pytest.mark.parametrize("name", sorted(SCOPES))
def test_a_scoped_run_writes_only_its_own_instances_and_their_backups(sync, tmp_path, capsys, name):
    world(tmp_path)
    before = snap(tmp_path)
    sync.TEMPLATE = name
    code, out, _ = run(sync, tmp_path, capsys)
    after = snap(tmp_path)
    assert code == 0, out

    changed = {p for p in before.keys() | after.keys() if before.get(p) != after.get(p)}
    stray = sorted(p for p in changed if not owned_by(p, SCOPES[name]))
    assert not stray, f"TEMPLATE={name!r} touched another template's files: {stray}"
    for inst in SCOPES[name]:
        assert before[inst] != after[inst], f"{inst} is in scope and was not updated"
    assert "my-widget.xml" not in after, "CREATE must not seed another template's stub"

    # the housekeeping steps ran for this template's instances, not just the merges
    own_bak = [p for p in changed if p.startswith(BACKUPS) and p not in after]
    assert own_bak, "the scoped run did not prune its own instances' backups"
    cleartext = {"tape": "old-s3cret", "tape-db": "hand-s3cret"}[name]
    assert not any(cleartext.encode() in v for v in after.values()), "own backup not redacted"
    other = {"tape": "hand-s3cret", "tape-db": "old-s3cret"}[name]
    assert any(other.encode() in v for v in after.values()), "another template's backup was rewritten"


@pytest.mark.parametrize("name,expect", [("tape", "my-tape.xml"), ("tape-db", "my-tape-db.xml")])
def test_a_scoped_run_creates_only_its_own_stub(sync, tmp_path, capsys, name, expect):
    world(tmp_path, instances=False)
    sync.TEMPLATE = name
    code, out, _ = run(sync, tmp_path, capsys)
    assert code == 0, out
    stubs = sorted(p.name for p in tmp_path.glob("my-*.xml"))
    assert stubs == [expect]
    # the stub's own backups are this run's even before the stub exists; nobody else's are
    after = b"".join(p.read_bytes() for p in (tmp_path / BACKUPS).iterdir())
    mine, other = {"tape": ("old-s3cret", "hand-s3cret"), "tape-db": ("hand-s3cret", "old-s3cret")}[name]
    assert mine.encode() not in after and other.encode() in after


@pytest.mark.parametrize("name,own,other", [("tape", "my-tape.xml", "my-tape-db.xml"),
                                            ("tape-db", "my-tape-db.xml", "my-tape.xml")])
def test_a_scoped_run_drops_strays_only_from_its_own_templates_instances(
        sync, tmp_path, capsys, name, own, other):
    world(tmp_path)
    for fname in ("my-tape.xml", "my-tape-db.xml"):
        text = (tmp_path / fname).read_text(encoding="utf-8")
        (tmp_path / fname).write_text(
            text.replace("</Container>", cfg("STRAY", "applied") + "</Container>"), encoding="utf-8")
    other_before = (tmp_path / other).read_bytes()
    sync.TEMPLATE = name
    code, out, _ = run(sync, tmp_path, capsys)
    assert code == 0, out
    assert "STRAY" not in (tmp_path / own).read_text(encoding="utf-8"), "own stray not dropped"
    assert (tmp_path / other).read_bytes() == other_before, "another template's instance was touched"
    assert "STRAY" in (tmp_path / other).read_text(encoding="utf-8")


def test_the_scope_is_decided_against_the_FULL_template_list(sync, tmp_path, capsys):
    """The trap spelled out: narrowing the name list before mapping instances would hand
    `my-tape-db-dev.xml` to `tape` (its longest prefix among ["tape"]). Its DB_PASS must
    survive a `tape` run untouched — neither re-described nor given TOKEN/PORT/NEWVAR."""
    world(tmp_path)
    before = (tmp_path / "my-tape-db-dev.xml").read_bytes()
    sync.TEMPLATE = "tape"
    run(sync, tmp_path, capsys)
    assert (tmp_path / "my-tape-db-dev.xml").read_bytes() == before


# ------------------------------------------------------------- 3. unknown name = refuse, loudly

@pytest.mark.parametrize("bad", ["tap", "tape.xml", "TAPE", "", " tape", "tape-db-dev", "plex"])
@pytest.mark.parametrize("dry", [False, True])
def test_an_unknown_TEMPLATE_refuses_and_writes_nothing(sync, tmp_path, capsys, bad, dry):
    world(tmp_path)
    # A container of that name, with a cleartext backup: a refusal placed after the redaction
    # step would redact it (the scope treats my-<TEMPLATE>.xml as its own) before refusing.
    # Unreadable on purpose: a readable foreign one would trip the stub refusal first and hide
    # the ordering this is here to catch.
    if not (tmp_path / f"my-{bad}.xml").exists():
        (tmp_path / f"my-{bad}.xml").write_bytes(b"<Container><unclosed>")
    (tmp_path / BACKUPS / f"my-{bad}.xml.20190101-000000.bak").write_bytes(
        container("other", [cfg("K", "bad-s3cret", masked=True)]).encode())
    before = snap(tmp_path)
    sync.TEMPLATE = bad
    sync.DRY_RUN = dry
    code, out, _ = run(sync, tmp_path, capsys)
    assert isinstance(code, str) and repr(bad) in code and "Nothing changed" in code
    assert "tape-db" in code, "the refusal should list the names that ARE valid"
    assert snap(tmp_path) == before
    assert "UPDATE" not in out and "CREATE" not in out


# ----------------------------------------------------------------- 4. the output names the scope

@pytest.mark.parametrize("dry", [True, False])
def test_the_run_names_its_scope_before_acting(sync, tmp_path, capsys, dry):
    world(tmp_path)
    before = snap(tmp_path)
    sync.TEMPLATE = "tape-db"
    sync.DRY_RUN = dry
    code, out, _ = run(sync, tmp_path, capsys)
    head = out.split("\n\n")[0]
    assert "scope: TEMPLATE='tape-db'" in head, head
    assert "the other 2 repo template(s) are left alone" in head
    assert "templates: tape-db\n" in head + "\n"
    assert "[tape]" not in out and "[widget]" not in out
    # the housekeeping figures are the scoped ones, in the rehearsal as well as the live run:
    # tape-db owns one cleartext backup, and 11 stamped ones (+1 from this live run)
    if dry:
        assert snap(tmp_path) == before
        assert "would redact 1 masked value(s) across 1 pre-existing backup(s)" in out
        assert "would prune at least 1 backup(s)" in out
    else:
        assert "REDACTED 1 masked value(s) across 1 pre-existing backup(s)" in out
        assert "pruned 2 backup(s)" in out


# -------------------------------------------------------------------- 5. edges of the scope

@pytest.mark.parametrize("dry", [False, True])
def test_a_stub_whose_TemplateURL_names_another_template_is_refused_not_rewritten(
        sync, tmp_path, capsys, dry):
    """my-tape.xml would be processed as `tape` by name while the mapping says `tape-db`: a
    `tape` run would pour tape's variables into a tape-db container. Refuse; the `tape-db` run
    owns it."""
    world(tmp_path)
    (tmp_path / "my-tape.xml").write_bytes(INSTANCES["my-tape-db.xml"].encode())
    before = snap(tmp_path)
    sync.DRY_RUN = dry
    sync.TEMPLATE = "tape"
    code, out, _ = run(sync, tmp_path, capsys)
    assert isinstance(code, str) and "maps to 'tape-db'" in code and "Nothing changed" in code
    assert snap(tmp_path) == before

    sync.TEMPLATE = "tape-db"
    code, out, _ = run(sync, tmp_path, capsys)
    assert code == 0, out
    assert "[tape-db]" in out and "my-tape.xml" in out.split("[tape-db]")[1]


@pytest.mark.parametrize("kind", ["tape-db instance", "foreign", "unreadable"])
def test_a_backup_belongs_to_the_LONGEST_instance_name_it_extends(sync, tmp_path, capsys, kind):
    """`my-tape.xml.old.xml` — a tape-db container by TemplateURL, a foreign one, or one that
    cannot be read — has backups that all start with `my-tape.xml.`. They are its backups, not
    my-tape.xml's: a `tape` run leaves them."""
    world(tmp_path)
    body = {"tape-db instance": INSTANCES["my-tape-db.xml"].encode(),
            "foreign": container("old", [cfg("X", "1")]).encode(),
            "unreadable": b"<Container><unclosed>"}[kind]
    (tmp_path / "my-tape.xml.old.xml").write_bytes(body)
    for i in range(11):
        (tmp_path / BACKUPS / f"my-tape.xml.old.xml.20200101-0000{i:02d}.bak").write_bytes(
            container("tape-db", [cfg("DB_PASS", "dotted-s3cret", masked=True)]).encode())
    before = {k: v for k, v in snap(tmp_path).items() if "my-tape.xml.old.xml" in k}
    sync.TEMPLATE = "tape"
    code, out, _ = run(sync, tmp_path, capsys)
    assert code == (1 if kind == "unreadable" else 0), out   # an unreadable instance is reported
    assert {k: v for k, v in snap(tmp_path).items() if "my-tape.xml.old.xml" in k} == before

    if kind == "tape-db instance":
        sync.TEMPLATE = "tape-db"
        run(sync, tmp_path, capsys)
        left = {k: v for k, v in snap(tmp_path).items()
                if "my-tape.xml.old.xml" in k and k.endswith(".bak")}
        assert len(left) == 10 and not any(b"dotted-s3cret" in v for v in left.values())


def test_a_backup_whose_instance_is_gone_is_touched_by_no_run(sync, tmp_path, capsys):
    """A gone container named like a template's instance (`my-tape-old.xml`) and a hand-dropped
    ownerless `.bak`: no run may redact or prune them (the operator deletes them by hand)."""
    world(tmp_path)
    b = tmp_path / BACKUPS
    for i in range(11):
        (b / f"my-tape-old.xml.20200101-0000{i:02d}.bak").write_bytes(
            container("tape-old", [cfg("TOKEN", "gone-s3cret", masked=True)]).encode())
    (b / "notes.bak").write_bytes(container("x", [cfg("K", "loose-s3cret", masked=True)]).encode())
    # extends a stub's name, but not with a dot: not my-tape.xml's
    (b / "my-tape.xml-pre-upgrade.bak").write_bytes(
        container("tape", [cfg("TOKEN", "loose-s3cret", masked=True)]).encode())

    def orphans():
        return {k: v for k, v in snap(tmp_path).items()
                if "tape-old" in k or k.endswith(("notes.bak", "pre-upgrade.bak"))}

    before = orphans()
    for name in ("tape", "tape-db", "widget"):
        sync.TEMPLATE = name
        code, out, _ = run(sync, tmp_path, capsys)
        assert code == 0, out
        assert orphans() == before, f"TEMPLATE={name!r} touched an orphan backup"


def test_another_templates_unreadable_backup_is_not_this_runs_failure(sync, tmp_path, capsys):
    world(tmp_path)
    (tmp_path / BACKUPS / "my-tape-db.xml.20200101-000099.bak").write_bytes(b"<Container><unclosed>")
    sync.TEMPLATE = "tape"
    code, out, _ = run(sync, tmp_path, capsys)
    assert code == 0 and "000099" not in out, out
    sync.TEMPLATE = "tape-db"
    code, out, _ = run(sync, tmp_path, capsys)
    assert code == 1 and "my-tape-db.xml.20200101-000099.bak" in out, out


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
    code, out, _ = run(sync, tmp_path, capsys)
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
    code, out, _ = run(sync, tmp_path, capsys)
    monkeypatch.undo()
    assert isinstance(code, str) and "could not list the directory" in code, code
    assert "cannot tell its own instances apart" in code and "Nothing changed" in code
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
