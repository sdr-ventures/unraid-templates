"""`sync-templates.py`: the five identity fields follow the repo copy, and nothing else new does.

A saved template keeps Project, Repository, Registry, TemplateURL and Icon from the day it was
created, so after an owner move it names the old repository and a re-add pulls a stale image.
The sync refreshes those five. Repository keeps the instance's tag or digest, because the image
tag is operator-owned (TEMPLATE_MANAGEMENT.md, rule 6). Every other container field stays the
instance's, exactly as before.
"""

import importlib.util
import pathlib
import xml.etree.ElementTree as ET

import pytest

SCRIPT = pathlib.Path(__file__).resolve().parents[1] / "scripts" / "sync-templates.py"

OLD = (
    '<Container version="2"><Name>widget</Name>'
    "<Project>https://old.example/widget</Project>"
    "<Repository>ghcr.io/old-owner/widget:stable</Repository>"
    "<Registry>https://ghcr.io/old-owner/widget</Registry>"
    "<TemplateURL>https://old.example/templates/widget.xml</TemplateURL>"
    "<Icon>https://old.example/widget.png</Icon>"
    "<Network>bridge</Network><Privileged>false</Privileged>"
    "</Container>"
)
NEW = (
    '<Container version="2"><Name>widget</Name>'
    "<Project>https://new.example/widget</Project>"
    "<Repository>ghcr.io/new-owner/widget:latest</Repository>"
    "<Registry>https://ghcr.io/new-owner/widget</Registry>"
    "<TemplateURL>https://new.example/templates/widget.xml</TemplateURL>"
    "<Icon>https://new.example/widget.png</Icon>"
    "<Network>br0</Network><Privileged>true</Privileged>"
    "</Container>"
)


@pytest.fixture()
def sync():
    spec = importlib.util.spec_from_file_location("sync_templates", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mod.DRY_RUN = False
    return mod


def field(root, name):
    return root.findtext(name)


def test_the_five_identity_fields_follow_the_repo(sync):
    merged, st = sync.merge(ET.fromstring(OLD), ET.fromstring(NEW))
    for name in ("Project", "Registry", "TemplateURL", "Icon"):
        assert field(merged, name) == field(ET.fromstring(NEW), name), name
    assert field(merged, "Repository") == "ghcr.io/new-owner/widget:stable"
    assert st["identity"] == ["Project", "Repository", "Registry", "TemplateURL", "Icon"]


def test_an_operator_pinned_digest_is_kept_with_the_repo_image_name(sync):
    op = OLD.replace("ghcr.io/old-owner/widget:stable", "ghcr.io/old-owner/widget@sha256:abc123")
    merged, _ = sync.merge(ET.fromstring(op), ET.fromstring(NEW))
    assert field(merged, "Repository") == "ghcr.io/new-owner/widget@sha256:abc123"


def test_an_untagged_repository_takes_the_repo_tag(sync):
    op = OLD.replace("ghcr.io/old-owner/widget:stable", "ghcr.io/old-owner/widget")
    merged, _ = sync.merge(ET.fromstring(op), ET.fromstring(NEW))
    assert field(merged, "Repository") == "ghcr.io/new-owner/widget:latest"


def test_no_other_container_field_is_refreshed(sync):
    merged, st = sync.merge(ET.fromstring(OLD), ET.fromstring(NEW))
    assert field(merged, "Network") == "bridge"
    assert field(merged, "Privileged") == "false"
    assert st["identity"] and "Network" not in st["identity"]


def test_a_template_without_an_identity_field_adds_nothing(sync):
    tpl = NEW.replace("<Icon>https://new.example/widget.png</Icon>", "")
    merged, st = sync.merge(ET.fromstring(OLD), ET.fromstring(tpl))
    assert field(merged, "Icon") == "https://old.example/widget.png"
    assert "Icon" not in st["identity"]


def test_a_dry_run_names_the_refreshed_fields_and_no_value(sync, tmp_path, capsys):
    path = tmp_path / "my-widget.xml"
    path.write_text(OLD, encoding="utf-8")
    sync.DRY_RUN = True
    assert sync.update_instance(str(path), ET.fromstring(NEW), str(tmp_path))
    out = capsys.readouterr().out
    assert "identity refreshed from repo: Project, Repository, Registry, TemplateURL, Icon" in out
    assert "new.example" not in out and "new-owner" not in out
    assert path.read_text(encoding="utf-8") == OLD


def test_an_empty_identity_field_is_not_filled_from_the_repo(sync):
    op = OLD.replace("<Icon>https://old.example/widget.png</Icon>", "<Icon></Icon>")
    merged, st = sync.merge(ET.fromstring(op), ET.fromstring(NEW))
    assert field(merged, "Icon") in ("", None)
    assert "Icon" not in st["identity"]


def test_a_different_image_is_left_alone(sync):
    # The live instance runs another image than the repo names: an operator's choice, not an owner move.
    tpl = NEW.replace("ghcr.io/new-owner/widget:latest", "ghcr.io/upstream/other-image:sha-1")
    merged, st = sync.merge(ET.fromstring(OLD), ET.fromstring(tpl))
    assert field(merged, "Repository") == "ghcr.io/old-owner/widget:stable"
    assert "Repository" not in st["identity"]
