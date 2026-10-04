"""`sync-templates.py`: what it must write, what it must NOT write, and what it must SAY.

Two bugs in one week made this file necessary, and they are the same shape twice over —
`merge()` computed the right answer and `update_instance` threw part of it away because the
gate on the write asked the wrong question.

1. The gate was ``st["added"] or st["dropped"]``, so a template edit that changed only a
   field's Description, Default, Display or Required was discarded. Every operator-facing
   instruction in this repo lives in a `Description`, so the one edit the templates exist to
   deliver was the one edit that could not arrive.
2. Comparing content instead fixed that but made a preserved operator Path Mode invisible:
   the merged tree equals the operator's, so the run said "nothing to change" while having
   made a decision. A preserved Mode is therefore reported on every run, with no write.

So the assertions below cover the whole surface, not just the bug of the day: writes,
non-writes, the reported Mode, the backup, and the rule that a variable absent from the
repo template is dropped whatever it holds. Each one was checked by mutating
`sync-templates.py` and confirming it goes red.
"""

import importlib.util
import pathlib
import sys
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
# a mapping the template does not define, holding a real value: it is deprecated, so it is dropped
SOCKET = (
    '<Config Name="Docker socket" Target="/var/run/docker.sock" Default="" Mode="rw" '
    'Description="x" Type="Path" Display="always" Required="false" Mask="false">'
    '/var/run/docker.sock</Config>'
)


def template(desc="TEXT"):
    return ET.fromstring(HEAD + TOKEN.format(desc=desc, tail="/>") + "</Container>")


def instance_xml(desc="TEXT", extra=""):
    return HEAD + TOKEN.format(desc=desc, tail=">s3cret</Config>") + extra + "</Container>"


@pytest.fixture()
def sync():
    spec = importlib.util.spec_from_file_location("sync_templates", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    # Pin it. README tells operators to flip DRY_RUN to True for a rehearsal, and under that
    # value four of these tests fail and the no-spurious-write one passes VACUOUSLY, because
    # nothing writes at all. A test suite whose verdict depends on a module constant the docs
    # invite you to change is not a test suite.
    mod.DRY_RUN = False
    return mod


@pytest.fixture()
def inst(tmp_path):
    def _make(xml):
        p = tmp_path / "my-widget.xml"
        p.write_text(xml, encoding="utf-8")
        return p
    return _make


def config_of(path, target="TOKEN"):
    for c in ET.parse(path).getroot().findall("Config"):
        if c.get("Target") == target:
            return c
    return None


# --------------------------------------------------------------------------- bug 1: writes

def test_a_description_only_edit_reaches_the_instance(sync, inst, tmp_path):
    p = inst(instance_xml(desc="OLD TEXT"))
    sync.update_instance(str(p), template(desc="NEW TEXT the operator must act on"), str(tmp_path / "b"))
    assert config_of(p).get("Description") == "NEW TEXT the operator must act on"
    assert config_of(p).text == "s3cret", "the applied value must survive"


@pytest.mark.parametrize("attr,value", [("Display", "advanced"), ("Required", "false"), ("Default", "xyz")])
def test_every_other_metadata_attribute_reaches_the_instance_too(sync, inst, tmp_path, attr, value):
    """The broken gate saw none of the metadata, so pin the class and not just Description."""
    p = inst(instance_xml())
    tpl = template()
    tpl.find("Config").set(attr, value)
    sync.update_instance(str(p), tpl, str(tmp_path / "b"))
    assert config_of(p).get(attr) == value
    assert config_of(p).text == "s3cret"


def test_an_unchanged_template_does_not_rewrite_the_instance(sync, inst, tmp_path):
    """The other direction: an unconditional rewrite would churn a secret-bearing backup."""
    p = inst(instance_xml())
    before = p.read_bytes()
    backups = tmp_path / "b"
    sync.update_instance(str(p), template(), str(backups))
    assert p.read_bytes() == before
    assert not backups.exists() or not list(backups.iterdir())


def test_dry_run_writes_nothing_even_when_there_is_a_real_change(sync, inst, tmp_path):
    p = inst(instance_xml(desc="OLD TEXT"))
    before = p.read_bytes()
    sync.DRY_RUN = True
    sync.update_instance(str(p), template(desc="NEW TEXT"), str(tmp_path / "b"))
    assert p.read_bytes() == before


# --------------------------------------------------- UT-SYNC-SAFE: Path Mode is operator-owned
#
# The lane queue note claimed the sync never reconciles Mode at all. A scratch run against
# `origin/main@fb8d8cf` (the pre-fix script) proved the OPPOSITE: `merge()` built every new
# Config from `copy.deepcopy(template_config)` and touched only `.text`, so an operator's Path
# Mode (e.g. a slave/secondary mount) was silently overwritten by the template's every run — and
# the run's own report said "metadata refreshed ... no variables added or removed", naming
# nothing, because the overwritten Config came out byte-identical to the template's own copy.
# Port Mode is explicitly NOT part of this — only a Path's Mode is an operator decision; a
# Port's protocol (tcp/udp) stays template-owned, same as before.

PATH_CFG = (
    '<Config Name="Data" Target="/data" Default="" Mode="{mode}" Description="{desc}" '
    'Type="Path" Display="always" Required="true" Mask="false">{value}</Config>'
)
PORT_CFG = (
    '<Config Name="P" Target="8080" Default="8080" Mode="{mode}" Description="{desc}" '
    'Type="Port" Display="always" Required="true" Mask="false">8080</Config>'
)


def test_an_operator_set_path_mode_survives_a_template_side_mode_change(sync):
    op_root = ET.fromstring(HEAD + PATH_CFG.format(
        mode="rw,slave", desc="x", value="/mnt/pool/data") + "</Container>")
    tpl_root = ET.fromstring(HEAD + PATH_CFG.format(
        mode="rw", desc="y", value="") + "</Container>")
    merged, st = sync.merge(op_root, tpl_root)
    cfg = merged.find("Config")
    assert cfg.get("Mode") == "rw,slave", "the operator's Path Mode must survive"
    assert cfg.text == "/mnt/pool/data", "the applied value must still survive too"
    assert cfg.get("Description") == "y", "every OTHER attribute keeps refreshing"
    assert st["mode_kept"] == [("Data", "rw,slave", "rw")]


def test_a_blank_operator_path_mode_takes_the_templates(sync):
    op_root = ET.fromstring(HEAD + PATH_CFG.format(
        mode="", desc="x", value="/mnt/pool/data") + "</Container>")
    tpl_root = ET.fromstring(HEAD + PATH_CFG.format(
        mode="rw", desc="y", value="") + "</Container>")
    merged, st = sync.merge(op_root, tpl_root)
    assert merged.find("Config").get("Mode") == "rw", "blank is not an operator decision to keep"
    assert st["mode_kept"] == []


def test_a_path_new_to_the_operator_takes_the_templates_mode(sync):
    op_root = ET.fromstring(HEAD + "</Container>")
    tpl_root = ET.fromstring(HEAD + PATH_CFG.format(
        mode="rw", desc="y", value="") + "</Container>")
    merged, st = sync.merge(op_root, tpl_root)
    assert merged.find("Config").get("Mode") == "rw"
    assert st["added"] == ["Data"]
    assert st["mode_kept"] == []


REAL_TLDW_REDIS = pathlib.Path(__file__).resolve().parents[1] / "templates" / "tldw-redis.xml"


def _old_redis_port_config(value):
    return (
        '<Config Name="Redis Port" Target="6379" Default="6380" Mode="tcp" Description="old" '
        f'Type="Port" Display="always" Required="true" Mask="false">{value}</Config>'
    )


def test_a_fresh_instance_synced_against_the_real_tldw_redis_template_gets_no_port(sync):
    tpl_root = ET.parse(REAL_TLDW_REDIS).getroot()
    op_root = ET.fromstring(
        '<Container version="2"><Name>tldw-redis</Name><Repository>redis:7-alpine</Repository>'
        '<Config Name="Data" Target="/data" Default="" Mode="rw" Description="d" Type="Path" '
        'Display="always" Required="true" Mask="false">/mnt/pool/appdata/tldw-redis/data'
        "</Config></Container>"
    )
    merged, st = sync.merge(op_root, tpl_root)
    assert not [c for c in merged.findall("Config") if c.get("Type") == "Port"]
    assert st["added"] == []


def test_an_instance_still_carrying_the_old_port_at_its_default_loses_it(sync):
    tpl_root = ET.parse(REAL_TLDW_REDIS).getroot()
    op_root = ET.fromstring(
        '<Container version="2"><Name>tldw-redis</Name><Repository>redis:7-alpine</Repository>'
        f"{_old_redis_port_config('6380')}</Container>"
    )
    merged, st = sync.merge(op_root, tpl_root)
    assert not [c for c in merged.findall("Config") if c.get("Type") == "Port"]
    assert st["dropped"] == [("Redis Port", True)]


def test_an_instance_with_the_port_set_to_a_real_value_loses_it_too(sync):
    tpl_root = ET.parse(REAL_TLDW_REDIS).getroot()
    op_root = ET.fromstring(
        '<Container version="2"><Name>tldw-redis</Name><Repository>redis:7-alpine</Repository>'
        f"{_old_redis_port_config('7777')}</Container>"
    )
    merged, st = sync.merge(op_root, tpl_root)
    assert not [c for c in merged.findall("Config") if c.get("Type") == "Port"]
    assert st["dropped"] == [("Redis Port", True)]


def test_port_mode_is_never_operator_owned(sync):
    op_root = ET.fromstring(HEAD + PORT_CFG.format(mode="udp", desc="x") + "</Container>")
    tpl_root = ET.fromstring(HEAD + PORT_CFG.format(mode="tcp", desc="y") + "</Container>")
    merged, st = sync.merge(op_root, tpl_root)
    assert merged.find("Config").get("Mode") == "tcp", "Port Mode must keep refreshing"
    assert st["mode_kept"] == [], "mode_kept is a Path-only concept"


def test_a_mode_only_difference_is_named_in_the_report_instead_of_being_invisible(sync, inst, tmp_path, capsys):
    """The exact regression: a preserved Mode must not be reported as 'nothing to change'."""
    p = inst(HEAD + PATH_CFG.format(mode="rw,slave", desc="SAME", value="/mnt/pool/data") + "</Container>")
    tpl = ET.fromstring(HEAD + PATH_CFG.format(mode="rw", desc="SAME", value="") + "</Container>")
    backups = tmp_path / "b"
    before = p.read_bytes()

    sync.update_instance(str(p), tpl, str(backups))

    assert p.read_bytes() == before, "a preserved-Mode-only run must not write the instance"
    assert not backups.exists() or not list(backups.iterdir()), "nor take a backup"
    out = capsys.readouterr().out
    assert "rw,slave" in out and "Mode" in out, "the preserved Mode must be named in the report"
    assert "nothing to change" not in out, "it is not 'nothing to change' - the Mode differs"


def test_a_mode_only_difference_is_reported_on_every_run_not_just_the_first(sync, inst, tmp_path, capsys):
    p = inst(HEAD + PATH_CFG.format(mode="rw,slave", desc="SAME", value="/mnt/pool/data") + "</Container>")
    tpl = ET.fromstring(HEAD + PATH_CFG.format(mode="rw", desc="SAME", value="") + "</Container>")
    for run in range(3):
        capsys.readouterr()
        sync.update_instance(str(p), tpl, str(tmp_path / "b"))
        assert "rw,slave" in capsys.readouterr().out, f"Mode preservation went unreported on run {run + 1}"


# ------------------------------------------------ the rule: not in the repo template = dropped
#
# The repo template is the schema; the live instance is the values. A Config the instance has
# and the template lacks is deprecated and dropped, whatever it holds. There is no keep-and-flag.

BLANK_EXTRA = (
    '<Config Name="Spare" Target="SPARE" Default="" Mode="" Description="x" '
    'Type="Variable" Display="always" Required="false" Mask="false"></Config>'
)


def test_a_valued_variable_absent_from_the_template_is_dropped_and_reported_as_valued(
        sync, inst, tmp_path, capsys):
    p = inst(instance_xml(extra=SOCKET))
    sync.update_instance(str(p), template(), str(tmp_path / "b"))
    out = capsys.readouterr().out
    assert config_of(p, "/var/run/docker.sock") is None, "a valued variable not in the template must go"
    assert "DROPPED, not in repo template (deprecated): Docker socket (held a value)" in out
    assert config_of(p).text == "s3cret", "a variable the template has keeps its applied value"


def test_a_blank_variable_absent_from_the_template_is_dropped_and_reported_as_blank(
        sync, inst, tmp_path, capsys):
    p = inst(instance_xml(extra=BLANK_EXTRA))
    sync.update_instance(str(p), template(), str(tmp_path / "b"))
    out = capsys.readouterr().out
    assert config_of(p, "SPARE") is None
    line = [ln for ln in out.splitlines() if "Spare" in ln]
    assert line and "DROPPED, not in repo template (deprecated): Spare" in line[0]
    assert "held a value" not in line[0], "a blank drop must not claim it held a value"


def test_a_variable_at_its_old_default_is_dropped_too_there_is_no_unused_exemption(
        sync, inst, tmp_path):
    at_default = (
        '<Config Name="Spare" Target="SPARE" Default="d" Mode="" Description="x" '
        'Type="Variable" Display="always" Required="false" Mask="false">d</Config>'
    )
    p = inst(instance_xml(extra=at_default))
    sync.update_instance(str(p), template(), str(tmp_path / "b"))
    assert config_of(p, "SPARE") is None


def test_a_drop_is_a_real_write_with_a_backup_that_still_holds_the_dropped_variable(
        sync, inst, tmp_path):
    p = inst(instance_xml(extra=SOCKET))
    backups = tmp_path / "b"
    sync.update_instance(str(p), template(), str(backups))
    saved = list(backups.iterdir())
    assert len(saved) == 1 and "/var/run/docker.sock" in saved[0].read_text(encoding="utf-8")


def test_a_second_run_after_a_drop_is_up_to_date(sync, inst, tmp_path, capsys):
    p = inst(instance_xml(extra=SOCKET))
    sync.update_instance(str(p), template(), str(tmp_path / "b"))
    capsys.readouterr()
    sync.update_instance(str(p), template(), str(tmp_path / "b"))
    out = capsys.readouterr().out
    assert "up to date" in out and "DROPPED" not in out


def test_there_is_no_keep_and_flag_path_left_in_merge(sync):
    """Behavioural regression: every kind of absent Config must leave `merged`, whatever it holds.

    Blank, at its old default, a real value, a secret, a Path, a Port: if any branch keeps one
    of them (the old keep-and-flag), it shows up in `merged` and this fails.
    """
    absent = [
        ("Variable", "A_BLANK", "", ""),
        ("Variable", "B_DEFAULT", "d", "d"),
        ("Variable", "C_VALUE", "d", "real"),
        ("Variable", "D_SECRET", "", "hunter2"),
        ("Path", "/e/path", "", "/mnt/POOL/x"),
        ("Port", "9999", "9999", "7777"),
    ]
    xml = "".join(
        f'<Config Name="{t}-{tg}" Target="{tg}" Default="{d}" Mode="" Description="x" Type="{t}" '
        f'Display="always" Required="false" Mask="false">{v}</Config>'
        for t, tg, d, v in absent
    )
    op_root = ET.fromstring(instance_xml(extra=xml))
    merged, st = sync.merge(op_root, template())
    left = {c.get("Target") for c in merged.findall("Config")}
    assert left == {"TOKEN"}, f"an absent Config survived the merge: {left - {'TOKEN'}}"
    assert len(st["dropped"]) == len(absent)
    assert "kept_flag" not in st, "the keep-and-flag stat is gone"
    assert [h for _, h in st["dropped"]] == [False, True, True, True, True, True]


# ------------------------------------------------------------- promises made in the README

def test_a_real_write_is_backed_up_first(sync, inst, tmp_path):
    """README: 'creates/updates/drops with timestamped backups'. Nothing was pinning it."""
    p = inst(instance_xml(desc="OLD TEXT"))
    backups = tmp_path / "b"
    sync.update_instance(str(p), template(desc="NEW TEXT"), str(backups))
    saved = list(backups.iterdir())
    assert len(saved) == 1, "an overwritten instance must be backed up"
    assert "OLD TEXT" in saved[0].read_text(encoding="utf-8"), "the backup must hold the PRE-write file"


def test_only_variables_absent_from_the_template_are_dropped(sync, inst, tmp_path):
    """The drop must not reach a variable the template defines, whatever its value."""
    p = inst(instance_xml(extra=BLANK_EXTRA + SOCKET))
    sync.update_instance(str(p), template(), str(tmp_path / "b"))
    assert config_of(p, "SPARE") is None and config_of(p, "/var/run/docker.sock") is None
    assert config_of(p).text == "s3cret", "a template variable's applied value must survive"


# ------------------------------------------------------- mutations that used to survive
# A verification round mutated the script and found these still green. Each one breaks a
# promise the README makes, so each gets the case that proves it bites.


def test_the_no_write_path_really_does_not_write_or_back_up(sync, inst, tmp_path):
    """Reporting a preserved Path Mode must not smuggle in a write.

    The Mode tests above assert on stdout only. With that alone, making the `unchanged`
    branch write and back up left the suite green — and the cost is a timestamped copy of a
    secret-bearing template churned on EVERY run, which is exactly the accumulation
    unraid-templates#27 is about.
    """
    p = inst(HEAD + PATH_CFG.format(mode="rw,slave", desc="SAME", value="/mnt/POOL/data") + "</Container>")
    tpl = ET.fromstring(HEAD + PATH_CFG.format(mode="rw", desc="SAME", value="") + "</Container>")
    before = p.read_bytes()
    backups = tmp_path / "b"
    sync.update_instance(str(p), tpl, str(backups))
    assert p.read_bytes() == before, "the no-write path wrote"
    assert not backups.exists() or not list(backups.iterdir()), "the no-write path took a backup"


def test_an_invalid_merge_result_is_refused_and_the_instance_is_left_alone(sync, inst, tmp_path):
    """README: 'a merged result is validated before it replaces the original.'

    Deleting the `validate(merged)` call left the whole suite green. The guard exists so a
    template that would produce a structurally broken instance cannot overwrite a working
    one — the failure mode being a container Unraid can no longer render or start.
    """
    # merge() bases the result on the OPERATOR's tree, so an invalid merge comes from a
    # damaged instance rather than a damaged template - a truncated my-*.xml, say, or a
    # half-written file from an interrupted Apply. validate() requires a non-empty
    # <Repository>; without the guard the script would happily rewrite that file.
    damaged = instance_xml(desc="OLD TEXT").replace(
        "<Repository>example/widget:1</Repository>", "<Repository></Repository>"
    )
    p = inst(damaged)
    before = p.read_bytes()

    sync.update_instance(str(p), template(desc="NEW TEXT"), str(tmp_path / "b"))

    assert p.read_bytes() == before, "an invalid merge result overwrote the instance"
    assert not (tmp_path / "b").exists() or not list((tmp_path / "b").iterdir()), (
        "a refused merge must not leave a backup either"
    )


def test_dry_run_reports_no_write_rather_than_would_update_when_there_is_nothing_to_write(
    sync, inst, tmp_path, capsys
):
    """Ordering: `if unchanged` must be tested before `elif DRY_RUN`.

    Swapping them made a dry run announce `would UPDATE` for an instance with nothing to
    write — a rehearsal that misreports what the real run will do is worse than no rehearsal.
    """
    p = inst(HEAD + PATH_CFG.format(mode="rw,slave", desc="SAME", value="/mnt/POOL/data") + "</Container>")
    tpl = ET.fromstring(HEAD + PATH_CFG.format(mode="rw", desc="SAME", value="") + "</Container>")
    sync.DRY_RUN = True
    sync.update_instance(str(p), tpl, str(tmp_path / "b"))
    out = capsys.readouterr().out
    assert "would UPDATE" not in out, "a dry run claimed it would write when there is nothing to write"
    assert "rw,slave" in out, "a dry run must still report the preserved Mode"


def test_a_dry_run_names_a_drop_but_removes_nothing(sync, inst, tmp_path, capsys):
    p = inst(instance_xml(extra=SOCKET))
    before = p.read_bytes()
    sync.DRY_RUN = True
    sync.update_instance(str(p), template(), str(tmp_path / "b"))
    out = capsys.readouterr().out
    assert p.read_bytes() == before
    assert "would UPDATE" in out
    assert "DROPPED, not in repo template (deprecated): Docker socket (held a value)" in out


def test_metadata_refreshed_is_only_claimed_when_that_is_what_happened(sync, inst, tmp_path, capsys):
    """The line says "no variables added or removed" - so it must not print when some were.

    It must not claim that for a duplicate-keyed Config (merge() DROPS it: a variable
    removed), for a dropped variable, or on the no-write path where nothing was refreshed.
    """
    # (a) a real metadata-only write: the claim is true and must be made
    p = inst(instance_xml(desc="OLD TEXT"))
    sync.update_instance(str(p), template(desc="NEW TEXT"), str(tmp_path / "b"))
    assert "metadata refreshed" in capsys.readouterr().out

    # (b) nothing written at all: nothing was refreshed, so the claim must not be made
    p2 = inst(HEAD + PATH_CFG.format(mode="rw,slave", desc="SAME", value="/mnt/POOL/data") + "</Container>")
    tpl2 = ET.fromstring(HEAD + PATH_CFG.format(mode="rw", desc="SAME", value="") + "</Container>")
    capsys.readouterr()
    sync.update_instance(str(p2), tpl2, str(tmp_path / "b2"))
    out = capsys.readouterr().out
    assert "metadata refreshed" not in out, "claimed a refresh on a run that wrote nothing"

    # (b2) a dropped variable is a variable removed
    p4 = inst(instance_xml(extra=SOCKET))
    capsys.readouterr()
    sync.update_instance(str(p4), template(), str(tmp_path / "b4"))
    assert "no variables added or removed" not in capsys.readouterr().out

    # (c) a duplicate is dropped, which IS a variable removed
    dupe = instance_xml(desc="SAME").replace("</Container>", "") + (
        '<Config Name="TOKEN" Target="TOKEN" Default="" Mode="" Description="SAME" '
        'Type="Variable" Display="always" Required="true" Mask="true">other</Config></Container>'
    )
    p3 = inst(dupe)
    capsys.readouterr()
    sync.update_instance(str(p3), template(desc="SAME"), str(tmp_path / "b3"))
    out = capsys.readouterr().out
    # unconditional: guarding this on "duplicate keys" in out meant deleting the dupes
    # bookkeeping entirely left the whole suite green, with this assertion never running.
    assert "duplicate keys" in out, "a duplicate-keyed Config must be reported"
    assert "no variables added or removed" not in out, (
        "claimed no variables were removed while dropping a duplicate"
    )


# =============================================================================================
# unraid-templates#27 — backups were a byte-for-byte PLAINTEXT copy of every masked value.
#
# `Mask="true"` only tells the Unraid web form to render a password box; the XML on the flash
# drive stores the value in cleartext regardless. So `shutil.copy2` wrote one more cleartext copy
# of every API token and PAT on every write, and nothing ever pruned them. RED before / GREEN
# after: on the previous script `test_a_masked_value_is_REDACTED_in_the_backup` fails outright.
# =============================================================================================

# A masked field with NO value, and an ordinary unmasked one — the two negative directions.
EMPTY_SECRET = (
    '<Config Name="SPARE_KEY" Target="SPARE_KEY" Default="" Mode="" Description="x" '
    'Type="Variable" Display="always" Required="false" Mask="true"></Config>'
)
SECRET = "s3cret"       # the value `instance_xml()` puts in the Mask="true" TOKEN field


# dockerMan writes every variable TWICE: the <Config> this script reconciles, and a legacy
# <Environment> mirror holding the same value. Assembled the way Unraid actually emits it.
ENVIRONMENT_MIRROR = (
    "<Environment><Variable><Value>s3cret</Value><Name>TOKEN</Name><Mode/></Variable>"
    "</Environment>"
)
# A masked field whose value sits in a CHILD element rather than in its own text.
NESTED_SECRET = (
    '<Config Name="NESTED" Target="NESTED" Default="" Mode="" Description="x" '
    'Type="Variable" Display="always" Required="false" Mask="true">pre<b>s3cret</b></Config>'
)
# ...and the same thing with NO leading text at all. This is the case that actually exercises the
# "a child element is content too" branch: with `pre` present the element is already caught by its
# own text, so a version that ignored children entirely still passed. The mutation matrix found
# that — the test above was proving less than its name claimed.
NESTED_SECRET_NO_TEXT = (
    '<Config Name="NESTED2" Target="NESTED2" Default="" Mode="" Description="x" '
    'Type="Variable" Display="always" Required="false" Mask="true"><b>s3cret</b></Config>'
)


def _backups(d):
    return sorted(p for p in d.iterdir() if p.name.endswith(".bak"))


def test_a_masked_value_is_REDACTED_in_the_backup(sync, inst, tmp_path):
    """⭐ THE ISSUE. The backup must not be a cleartext copy of the operator's PAT."""
    p = inst(instance_xml(desc="OLD TEXT"))
    backups = tmp_path / "b"
    assert SECRET in p.read_text(encoding="utf-8"), "precondition: the instance holds a secret"

    sync.update_instance(str(p), template(desc="NEW TEXT"), str(backups))

    saved = _backups(backups)
    assert len(saved) == 1, "an overwritten instance must still be backed up"
    text = saved[0].read_text(encoding="utf-8")
    assert SECRET not in text, (
        "the backup still holds the masked value in PLAINTEXT — this is unraid-templates#27")
    assert sync.REDACTED in text, "the masked field should be marked as redacted, not dropped"


def test_the_ENVIRONMENT_mirror_of_a_masked_variable_is_redacted_too(sync, inst, tmp_path):
    """⭐ dockerMan STORES EVERY VARIABLE TWICE, and redacting only the <Config> left the secret
    sitting in the other copy — in cleartext, in every backup, while the run reported it redacted.

    `merge()` deepcopies the operator tree and reconciles <Config> only, so the <Environment>
    block is preserved verbatim. Two independent reviews reproduced this against the real Unraid
    shape, which is why it is fixed rather than filed.
    """
    p = inst(instance_xml(desc="OLD TEXT", extra=ENVIRONMENT_MIRROR))
    backups = tmp_path / "b"
    raw = p.read_text(encoding="utf-8")
    assert raw.count(SECRET) == 2, "precondition: the secret is stored in both places"

    sync.update_instance(str(p), template(desc="NEW TEXT"), str(backups))

    text = _backups(backups)[0].read_text(encoding="utf-8")
    assert SECRET not in text, (
        "the <Environment> mirror still holds the secret in plaintext — redacting the <Config> "
        "half only is not closing #27")
    assert text.count(sync.REDACTED) == 2


def test_a_masked_value_held_in_a_CHILD_element_is_redacted_and_counted_honestly(sync, tmp_path):
    """⚠️ `el.text` is only the text BEFORE the first child.

    Setting it alone left the secret in the child while COUNTING the field as redacted — so the
    run reported the file clean, and the idempotence check then saw a redacted `text` and never
    looked at that file again. The value was permanently classified as cleared. A false "this is
    now safe" is worse than no clear-out at all, which is why this is not merely cosmetic.
    """
    backups = tmp_path / "b"
    backups.mkdir()
    bak = backups / "my-widget.xml.20260101-000000.bak"
    # BOTH nested shapes: one with text before the child, one with the child alone. Only the
    # second exercises the "a child is content too" branch — with leading text the element is
    # already caught by its own text, so a version ignoring children entirely still passed.
    bak.write_text(instance_xml(extra=NESTED_SECRET + NESTED_SECRET_NO_TEXT), encoding="utf-8")

    files, values, unreadable = sync.redact_existing_backups(str(backups))

    text = bak.read_text(encoding="utf-8")
    assert SECRET not in text, "the secret survived inside a child element"
    assert (files, values, unreadable) == (1, 3, []), (
        "the count must include BOTH nested fields, or the report is a false all-clear")
    assert sync.redact_existing_backups(str(backups)) == (0, 0, []), "not idempotent"


def test_the_mirror_matches_the_VARIABLE_name_not_the_human_label(sync):
    """⚠️ `Target` is the environment variable; `Name` is the label the UI shows.

    Matching the human label too meant an unrelated variable that merely shared a label — "Key" is
    an obvious collision — was redacted in the backup, destroying a NON-secret value a restore
    would want, and counted, so the run reported more redactions than it made.
    """
    xml = (
        HEAD
        + '<Config Name="Key" Target="API_KEY" Default="" Mode="" Description="x" '
          'Type="Variable" Display="always" Required="true" Mask="true">s3cret</Config>'
        + "<Environment>"
          "<Variable><Value>s3cret</Value><Name>API_KEY</Name></Variable>"
          "<Variable><Value>public-not-a-secret</Value><Name>Key</Name></Variable>"
          "</Environment>"
        + "</Container>"
    )
    root = ET.fromstring(xml)
    n = sync.redact_secrets(root)

    text = ET.tostring(root, encoding="unicode")
    assert SECRET not in text, "the real secret and its mirror must both go"
    assert "public-not-a-secret" in text, (
        "an unrelated variable was redacted because it shared the Config's human LABEL — that "
        "destroys a value a restore needs")
    assert n == 2, f"reported {n} redactions for 2 real ones — the count follows the same bug"


def test_a_config_with_no_Target_still_matches_its_mirror_by_Name(sync):
    """The fallback arm. Narrowing to Target must not stop matching a Config that has none."""
    xml = (
        HEAD
        + '<Config Name="TOKEN" Default="" Mode="" Description="x" Type="Variable" '
          'Display="always" Required="true" Mask="true">s3cret</Config>'
        + "<Environment><Variable><Value>s3cret</Value><Name>TOKEN</Name></Variable></Environment>"
        + "</Container>"
    )
    root = ET.fromstring(xml)
    assert sync.redact_secrets(root) == 2
    assert SECRET not in ET.tostring(root, encoding="unicode")


def test_a_backup_that_cannot_be_WRITTEN_skips_the_instance_instead_of_crashing(
    sync, inst, tmp_path, monkeypatch, capsys,
):
    """⛔ THE SIBLING PATH. `redact_existing_backups` was hardened so one bad file could not abort
    the sync; `backup()` was left raising, so a full or read-only flash drive gave a traceback out
    of `main()`, a half-finished run, and an orphaned `.tmp` nothing ever cleans up.

    A fix that leaves a sibling is half a fix — this is the sibling.
    """
    p = inst(instance_xml(desc="OLD TEXT"))
    before = p.read_bytes()
    backups = tmp_path / "b"

    def no_space(path, root):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(sync, "atomic_write", no_space)
    with pytest.raises(sync.BackupUnsafe):
        sync.backup(str(p), str(backups))
    assert not list(backups.glob("*.tmp")), "an orphaned .tmp was left behind"

    # ...and through update_instance, the operator sees a SKIP and keeps their file.
    sync.update_instance(str(p), template(desc="NEW TEXT"), str(backups))
    assert p.read_bytes() == before, "the instance was written despite the backup failing"
    assert "SKIP" in capsys.readouterr().out
    assert not list(backups.glob("*.tmp")), "an orphaned .tmp was left behind"


def test_a_masked_field_whose_ONLY_content_is_a_child_still_counts_as_holding_a_secret(sync):
    """The `len(el) > 0` branch, isolated. A mutation returning False there survived the test
    above, because that fixture had text before the child and was caught by the text branch."""
    root = ET.fromstring(instance_xml(extra=NESTED_SECRET_NO_TEXT))
    nested = [c for c in root.findall("Config") if c.get("Target") == "NESTED2"][0]
    assert not (nested.text or "").strip(), "precondition: no text, the value is in the child"
    assert sync._element_holds_a_secret(nested), (
        "a masked field whose only content is a child element was judged empty — its secret "
        "would survive redaction while being reported as cleared")


def test_the_LIVE_instance_keeps_its_secret(sync, inst, tmp_path):
    """⛔ THE REGRESSION REDACTION COULD EASILY SHIP: redacting the file being backed UP.

    `backup()` parses its own tree, so the live instance must be untouched. Redacting it would
    destroy the running container's credentials on the next sync — far worse than the leak.
    """
    p = inst(instance_xml(desc="OLD TEXT"))
    sync.update_instance(str(p), template(desc="NEW TEXT"), str(tmp_path / "b"))
    assert config_of(p).text == SECRET, (
        "the sync REDACTED the live instance — the operator's real value is gone")


def test_an_unmasked_value_is_preserved_in_the_backup(sync, inst, tmp_path):
    """The negative direction. "Nothing sensitive survives" is equally true of a backup that
    blanks EVERYTHING, and such a backup restores nothing."""
    # A real metadata change, so the write path (and therefore the backup) actually runs.
    p = inst(instance_xml(desc="OLD TEXT", extra=SOCKET))
    backups = tmp_path / "b"
    sync.update_instance(str(p), template(desc="NEW TEXT"), str(backups))
    saved = _backups(backups)
    assert saved, "precondition: a backup was taken"
    text = saved[0].read_text(encoding="utf-8")
    assert "/var/run/docker.sock" in text, "an unmasked value must survive into the backup"
    assert "OLD TEXT" in text, "the backup must still hold the PRE-write metadata"
    assert text.count(sync.REDACTED) == 1, "exactly the one masked field should be redacted"


def test_an_EMPTY_masked_field_is_not_marked_as_holding_a_secret(sync, inst, tmp_path):
    """Redacting an empty field would claim it held a value it never had, which misleads an
    operator reading a backup to see what was configured."""
    root = ET.fromstring(instance_xml(extra=EMPTY_SECRET))
    assert sync.redact_secrets(root) == 1, "only the ONE non-empty masked value is redacted"
    spare = [c for c in root.findall("Config") if c.get("Target") == "SPARE_KEY"][0]
    assert not (spare.text or "").strip(), "an empty masked field must stay empty"


def test_a_file_that_cannot_be_backed_up_safely_is_NOT_overwritten(sync, tmp_path, capsys):
    """⛔ NO BACKUP, NO WRITE — and no falling back to a plaintext copy.

    A fallback that did the unsafe thing whenever the safe one failed would reintroduce #27 on
    exactly the malformed files nobody inspects.
    """
    p = tmp_path / "my-widget.xml"
    p.write_text("<Container><Name>x</Name>", encoding="utf-8")  # truncated: unparseable
    backups = tmp_path / "b"
    with pytest.raises(sync.BackupUnsafe):
        sync.backup(str(p), str(backups))
    assert not backups.exists() or not _backups(backups), "an unsafe backup was written anyway"


def test_the_one_time_clear_out_redacts_backups_previous_versions_already_wrote(sync, tmp_path):
    """⭐ Fixing `backup()` stops NEW cleartext copies and does nothing about the pile already on
    the flash drive, which is where every secret this script has ever seen currently sits."""
    backups = tmp_path / "b"
    backups.mkdir()
    old = backups / "my-widget.xml.20260101-000000.bak"
    old.write_text(instance_xml(), encoding="utf-8")
    assert SECRET in old.read_text(encoding="utf-8"), "precondition: plaintext accumulation"

    files, values, unparseable = sync.redact_existing_backups(str(backups))

    assert (files, values, unparseable) == (1, 1, [])
    assert SECRET not in old.read_text(encoding="utf-8"), "the pre-existing plaintext survived"
    assert sync.REDACTED in old.read_text(encoding="utf-8")


def test_the_clear_out_is_idempotent(sync, tmp_path):
    """A second run must find nothing left to do — otherwise every run reports a clear-out it did
    not perform, and the operator cannot tell when the accumulation is actually gone."""
    backups = tmp_path / "b"
    backups.mkdir()
    (backups / "my-widget.xml.20260101-000000.bak").write_text(instance_xml(), encoding="utf-8")
    sync.redact_existing_backups(str(backups))
    assert sync.redact_existing_backups(str(backups)) == (0, 0, [])


def test_an_unparseable_backup_is_REPORTED_and_never_silently_deleted(sync, tmp_path):
    """It may well hold a secret, and only the operator can say whether it is worth keeping.
    Deleting the operator's data to make a report look clean is the wrong trade."""
    backups = tmp_path / "b"
    backups.mkdir()
    broken = backups / "my-widget.xml.20260101-000000.bak"
    broken.write_text("<Container><Name>x</Name>", encoding="utf-8")

    files, values, unreadable = sync.redact_existing_backups(str(backups))

    assert (files, values) == (0, 0)
    assert len(unreadable) == 1 and unreadable[0][0] == broken.name, unreadable
    assert broken.exists(), "an unparseable backup was deleted rather than reported"


def test_the_backup_that_the_run_told_you_to_review_is_not_then_PRUNED(sync, tmp_path):
    """⛔ THE RUN MUST NOT DELETE THE FILE IT JUST NAMED.

    `redact_existing_backups` reports an unparseable `.bak` and promises it is left for the
    operator to review. `prune_backups` ran moments later in `main()` and took it out on age —
    so the run printed "review and delete them by hand" about a file it had already removed. A
    truncated `.bak` is exactly what the old plaintext-copy path left behind, so this is reachable
    in the field, and the file it deletes is the one most likely to still hold a secret.
    """
    backups = tmp_path / "b"
    backups.mkdir()
    broken = backups / "my-widget.xml.20260101-000000.bak"
    broken.write_text("<Container><Name>x</Name>", encoding="utf-8")
    for i in range(sync.KEEP_BACKUPS + 2):
        (backups / f"my-widget.xml.202602{i + 1:02d}-000000.bak").write_text(
            "<Container/>", encoding="utf-8")

    _, _, unreadable = sync.redact_existing_backups(str(backups))
    dropped, _ = sync.prune_backups(str(backups), protected={f for f, _ in unreadable})

    assert broken.name not in dropped, "pruned the very file the run told the operator to review"
    assert broken.exists(), "the unparseable backup was deleted after being reported"
    assert dropped, "precondition: the prune must actually have had work to do"


def test_prune_never_touches_a_file_this_script_did_not_write(sync, tmp_path):
    """⛔ NOT OURS TO DELETE — and it also corrupted the ordering.

    An operator's `my-tape.xml.BEFORE-MIGRATION.bak` landed in the same group as the real backups
    under the old positional `rsplit(".", 2)`, and since letters sort after digits it counted as
    the NEWEST. The three genuine timestamped backups were pruned and the hand-named files became
    immortal — precisely inverted.
    """
    backups = tmp_path / "b"
    backups.mkdir()
    for i in range(sync.KEEP_BACKUPS + 3):
        (backups / f"my-tape.xml.202601{i + 1:02d}-000000.bak").write_text(
            "<Container/>", encoding="utf-8")
    hand_named = ["my-tape.xml.BEFORE-MIGRATION.bak", "my-tape.xml.orig.bak", "notes.bak"]
    for name in hand_named:
        (backups / name).write_text("<Container/>", encoding="utf-8")

    dropped, failed = sync.prune_backups(str(backups))

    assert failed == []
    for name in hand_named:
        assert name not in dropped, f"deleted {name}, which this script never wrote"
        assert (backups / name).exists(), f"{name} was removed"
    assert len(dropped) == 3 and all(d.startswith("my-tape.xml.2026") for d in dropped), dropped
    assert "my-tape.xml.20260101-000000.bak" in dropped, "the OLDEST should go"
    assert "my-tape.xml.20260113-000000.bak" not in dropped, "the NEWEST must stay"


def test_two_backups_in_the_same_second_do_not_overwrite_each_other(sync, inst, tmp_path):
    """⛔ A BACKUP MUST NOT DESTROY A BACKUP. The stamp is one-second resolution, so a second
    write in the same second silently replaced the first — and the backup is the whole recovery
    story now that it is the only copy the operator has of the pre-write state."""
    backups = tmp_path / "b"
    p = inst(instance_xml(desc="ONE"))
    first, _ = sync.backup(str(p), str(backups))
    p.write_text(instance_xml(desc="TWO"), encoding="utf-8")
    second, _ = sync.backup(str(p), str(backups))

    assert first != second, "the second backup overwrote the first"
    assert len(_backups(backups)) == 2
    assert "ONE" in pathlib.Path(first).read_text(encoding="utf-8")
    assert "TWO" in pathlib.Path(second).read_text(encoding="utf-8")
    # The collision name must still be prunable, or these accumulate forever.
    assert sync._BAK_RX.match(pathlib.Path(second).name), (
        f"{pathlib.Path(second).name} does not match the name shape prune_backups recognises")


def test_neither_housekeeping_step_explodes_on_a_first_ever_run(sync, tmp_path):
    """Both are called from `main()` BEFORE anything creates the backup dir, so a missing guard
    here is an uncaught FileNotFoundError that aborts the entire sync on a brand-new install."""
    absent = str(tmp_path / "never-created")
    assert sync.redact_existing_backups(absent) == (0, 0, [])
    assert sync.prune_backups(absent) == ([], [])


def test_an_unwritable_backup_does_not_abort_the_whole_sync(sync, tmp_path, monkeypatch):
    """⛔ ONE BAD FILE MUST NOT STOP EVERY TEMPLATE FROM SYNCING.

    An uncaught OSError here propagated out of `main()` before a single template was processed, so
    a full flash drive in the housekeeping step silently became "the sync does nothing at all".
    """
    backups = tmp_path / "b"
    backups.mkdir()
    (backups / "my-a.xml.20260101-000000.bak").write_text(instance_xml(), encoding="utf-8")
    (backups / "my-b.xml.20260101-000000.bak").write_text(instance_xml(), encoding="utf-8")

    real = sync.atomic_write

    def explode(path, root):
        if "my-a.xml" in path:
            raise OSError(28, "No space left on device")
        return real(path, root)

    monkeypatch.setattr(sync, "atomic_write", explode)
    files, values, unreadable = sync.redact_existing_backups(str(backups))

    assert files == 1 and values == 1, "the healthy file was still redacted"
    assert [f for f, _ in unreadable] == ["my-a.xml.20260101-000000.bak"], unreadable
    assert SECRET not in (backups / "my-b.xml.20260101-000000.bak").read_text(encoding="utf-8")
    assert not list(backups.glob("*.tmp")), "a partial .tmp file was left behind"


def test_update_instance_REFUSES_to_write_when_the_backup_cannot_be_made(sync, inst, tmp_path,
                                                                         monkeypatch, capsys):
    """⛔ NO BACKUP, NO WRITE — pinned at the level that actually decides it.

    The sibling test drives `backup()` directly and asserts only that it raises, so deleting the
    whole `try/except BackupUnsafe` gate from `update_instance` left the suite green. What matters
    to an operator is that the instance is untouched, which is what this asserts.
    """
    p = inst(instance_xml(desc="OLD TEXT"))
    before = p.read_bytes()
    backups = tmp_path / "b"

    def refuse(path, backup_dir):
        raise sync.BackupUnsafe("simulated: cannot redact this file")

    monkeypatch.setattr(sync, "backup", refuse)
    sync.update_instance(str(p), template(desc="NEW TEXT"), str(backups))

    assert p.read_bytes() == before, "the instance was overwritten with no backup taken"
    assert not backups.exists() or not _backups(backups), "a backup appeared anyway"
    assert "SKIP" in capsys.readouterr().out


def test_prune_keeps_the_newest_N_and_never_borrows_from_another_instance(sync, tmp_path):
    """Grouped PER INSTANCE: a container synced often must not evict the only backup another
    container has. The stamp format makes a lexicographic sort chronological."""
    backups = tmp_path / "b"
    backups.mkdir()
    for i in range(sync.KEEP_BACKUPS + 3):
        (backups / f"my-noisy.xml.202601{i + 1:02d}-000000.bak").write_text("<Container/>",
                                                                            encoding="utf-8")
    (backups / "my-quiet.xml.20260101-000000.bak").write_text("<Container/>", encoding="utf-8")

    dropped, failed = sync.prune_backups(str(backups))

    assert failed == []
    assert len(dropped) == 3, f"expected the 3 oldest noisy backups to go, got {dropped}"
    assert all(d.startswith("my-noisy") for d in dropped), dropped
    kept = {p.name for p in _backups(backups)}
    assert "my-quiet.xml.20260101-000000.bak" in kept, "the quiet instance lost its only backup"
    assert len([k for k in kept if k.startswith("my-noisy")]) == sync.KEEP_BACKUPS
    # The OLDEST are the ones dropped, not an arbitrary three.
    assert "my-noisy.xml.20260101-000000.bak" in dropped
    assert "my-noisy.xml.20260113-000000.bak" in kept


def test_dry_run_neither_rewrites_nor_deletes_a_backup(sync, tmp_path):
    """The DRY_RUN contract is 'writes NOTHING'. It covers the backup dir too — and this is the
    run an operator uses to decide whether to trust the clear-out at all."""
    backups = tmp_path / "b"
    backups.mkdir()
    plain = backups / "my-widget.xml.20260101-000000.bak"
    plain.write_text(instance_xml(), encoding="utf-8")
    for i in range(sync.KEEP_BACKUPS + 2):
        (backups / f"my-noisy.xml.202601{i + 1:02d}-000000.bak").write_text("<Container/>",
                                                                            encoding="utf-8")
    before = {p.name: p.read_bytes() for p in _backups(backups)}

    sync.DRY_RUN = True
    try:
        files, values, _ = sync.redact_existing_backups(str(backups))
        dropped, failed = sync.prune_backups(str(backups))
    finally:
        sync.DRY_RUN = False

    assert (files, values) == (1, 1), "dry-run must still REPORT what it would redact"
    assert len(dropped) == 2, "dry-run must still report what it would prune"
    assert failed == []
    assert {p.name: p.read_bytes() for p in _backups(backups)} == before, (
        "DRY_RUN wrote to or deleted from the backup directory")
