"""`sync-templates.py`: where a run's backups go (UT_SYNC_BACKUPS_1).

Backups leave the flash drive for the host's delete folder (BACKUP_DIR). A live run refuses
before any write when BACKUP_DIR is blank or sits on the flash drive; a dry run writes nothing
whatever BACKUP_DIR is; a backup's name is `<instance>.sync-<stamp>`, with `<BACKUP_ROLE>-` in
front when a role is set; masked values stay redacted in it.

Every value here is synthetic. The flash path is the one the script names, /boot, and the
templates folder is a temp dir standing in for it.
"""

import importlib.util
import pathlib

import pytest

SCRIPT = pathlib.Path(__file__).resolve().parents[1] / "scripts" / "sync-templates.py"
SECRET = "synthetic-secret-value"


def container(plain, token="", desc="x"):
    return ('<Container version="2"><Name>app</Name><Repository>example/app:1</Repository>'
            '<TemplateURL>https://example.invalid/templates/app.xml</TemplateURL>'
            f'<Config Name="TOKEN" Target="TOKEN" Default="" Mode="" Description="{desc}" '
            f'Type="Variable" Display="always" Required="false" Mask="true">{token}</Config>'
            f'<Config Name="PLAIN" Target="PLAIN" Default="" Mode="" Description="{desc}" '
            f'Type="Variable" Display="always" Required="false" Mask="false">{plain}</Config>'
            '</Container>')


# The operator's applied values are kept by merge(); what forces a write is a metadata change the
# template makes (here, the description), so the run really overwrites the live file.
TEMPLATE_XML = container("kept", desc="new description")
LIVE_XML = container("changed", SECRET, desc="old description")


@pytest.fixture()
def sync(monkeypatch):
    spec = importlib.util.spec_from_file_location("sync_templates_dest", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mod.DRY_RUN = False
    mod.TEMPLATE = "app"
    mod.BACKUP_DIR = ""
    mod.BACKUP_ROLE = ""
    mod.resolve_sha = lambda: "a" * 40          # no network: the commit is pinned per run
    monkeypatch.setattr(mod, "list_repo_templates", lambda sha: ["app"])
    monkeypatch.setattr(mod, "fetch_template", lambda name, sha: TEMPLATE_XML.encode())
    return mod


def world(root):
    root.mkdir(parents=True, exist_ok=True)
    (root / "my-app.xml").write_text(LIVE_XML, encoding="utf-8")
    return root


def run(sync, root, capsys):
    sync.TEMPLATES_USER = str(root)
    code = 0
    try:
        sync.main()
    except SystemExit as e:
        code = e.code
    return code, capsys.readouterr().out


def snap(root):
    return {p.relative_to(root).as_posix(): p.read_bytes()
            for p in sorted(root.rglob("*")) if p.is_file()}


def test_a_live_run_with_a_blank_BACKUP_DIR_refuses_before_any_write(sync, tmp_path, capsys):
    root = world(tmp_path / "flash")
    before = snap(root)
    code, _ = run(sync, root, capsys)
    assert isinstance(code, str) and "BACKUP_DIR is blank" in code and "Nothing changed" in code, code
    assert snap(root) == before
    assert not (root / ".template-sync-backups").exists()


@pytest.mark.parametrize("where", ["templates", "templates-sub", "boot"])
def test_a_BACKUP_DIR_on_the_flash_drive_refuses_before_any_write(sync, tmp_path, capsys, where):
    """The backup must not land under the templates folder, nor anywhere under /boot (the flash)."""
    root = world(tmp_path / "flash")
    sync.BACKUP_DIR = {
        "templates": str(root),
        "templates-sub": str(root / "_delete"),
        "boot": "/boot/config/plugins/somewhere",
    }[where]
    before = snap(root)
    code, _ = run(sync, root, capsys)
    assert isinstance(code, str) and "on the flash drive" in code and "Nothing changed" in code, code
    assert snap(root) == before


def test_a_dry_run_writes_nothing_even_with_a_blank_BACKUP_DIR(sync, tmp_path, capsys):
    root = world(tmp_path / "flash")
    before = snap(root)
    sync.DRY_RUN = True
    code, out = run(sync, root, capsys)
    assert code == 0, out
    assert snap(root) == before


def test_a_live_run_writes_its_backup_to_the_host_folder_with_a_redacted_secret(sync, tmp_path, capsys):
    root = world(tmp_path / "flash")
    host = tmp_path / "host-delete"
    sync.BACKUP_DIR = str(host)
    code, out = run(sync, root, capsys)
    assert code == 0, out
    (bak,) = list(host.iterdir())
    assert bak.name.startswith("my-app.xml.sync-"), bak.name
    text = bak.read_text(encoding="utf-8")
    assert SECRET not in text, "the backup holds the masked value in clear"
    assert sync.REDACTED in text
    assert ">changed<" in text, "the backup should carry the pre-write PLAIN value, not the template's"
    assert not (root / ".template-sync-backups").exists(), "no backup folder on the flash"


def test_a_BACKUP_ROLE_goes_in_front_of_sync_in_the_backup_name(sync, tmp_path, capsys):
    root = world(tmp_path / "flash")
    host = tmp_path / "host-delete"
    sync.BACKUP_DIR = str(host)
    sync.BACKUP_ROLE = "gambit"
    code, out = run(sync, root, capsys)
    assert code == 0, out
    (bak,) = list(host.iterdir())
    assert bak.name.startswith("my-app.xml.gambit-sync-"), bak.name


def test_two_backups_of_one_instance_never_share_a_name(sync, tmp_path):
    host = tmp_path / "host-delete"
    live = tmp_path / "my-app.xml"
    live.write_text(LIVE_XML, encoding="utf-8")
    first, _ = sync.backup(str(live), str(host))
    second, _ = sync.backup(str(live), str(host))
    assert first != second
    assert all(pathlib.Path(p).name.startswith("my-app.xml.sync-") for p in (first, second))
