"""Guard for UT-N: a template must not carry a Variable the consuming app never reads.

test_template_invariants.py says plainly what it does NOT do: "Nothing here runs a
container, reads an image or checks that an application actually READS a variable. Whether
a field is inert is established against the application's source, by hand, when a template
is written or audited." UT-N did exactly that by hand, once, for every template with a
consumer under `C:\\dev`. This file is what stops the finding from rotting the moment this
package returns.

WHY THIS CANNOT BE A LIVE GREP AGAINST THE CONSUMING REPO, IN THIS REPO'S CI: this repo is
PUBLIC and its CI runs on GitHub-hosted runners only (a public repo never uses the fleet's
self-hosted runners — see _worker-common "A PUBLIC REPO NEVER USES THE SELF-HOSTED RUNNER
FLEET"). Every consuming app (keystone, tape, cef-tracker, reauth-bot, the-desk) is a
SEPARATE, PRIVATE repo — its source is not, and must not be, available to a public repo's
CI job. So the live "does the app actually read this" check cannot run here; it can only
run by hand, by whoever edits a template's Variables, against a checkout of the consumer.

What CAN run here, and does: `tests/consumption.json` is the durable RECORD of that
hand audit — one citation per declared Variable, naming the file:line in the consumer repo
that actually uses it. This file enforces that the record is COMPLETE and never stale:
  * every `Type="Variable"` Config in every template has a citation (a Variable added
    without one fails CI immediately, instead of accreting silently);
  * no citation survives for a Variable a template no longer declares (a stale record is
    exactly the "changelog of dead settings" this package exists to stop, moved one file
    over instead of eliminated);
  * a template flagged `no_local_consumer` carries no per-var citations to go stale, and is
    itself checked against the actual absence of a same-named repo under `C:\\dev` at audit
    time was recorded correctly by a human — this file cannot re-verify that part.
  * every citation starts `reads <VARIABLE>`: the exact env-var name the code looks up, not
    just the feature it serves. Find that name before writing the line. For os.environ /
    getenv / a shell `${VAR}` it is the literal string. For a pydantic-settings field it is
    env_prefix + the field name, compared case-insensitively unless case_sensitive=True; an
    alias / validation_alias replaces it. A citation that says pydantic names a
    `field <name>`, and every `field <name>` in the citation must spell the Variable, as
    must every lookup written as `${NAME}`, `os.environ.get("NAME")`, `os.environ["NAME"]`,
    `getenv("NAME")` or `process.env.NAME`. Other shapes, such as a project's own
    `_env("NAME")` wrapper, are not checked. The check does not model env_prefix, aliases
    or case_sensitive=True, since no consumer uses them; extend it when one does. Any
    identifier in the citation made of the Variable's words in another order
    (`backup_db_user` under `DB_BACKUP_USER`) fails too: that is a Variable nothing reads.
  * every Type="Path" Target gets the same exact check under `paths`, with citations that
    open `reads <PATH>` (the path itself, not a file under it).
  * a Variable or Path a release has not wired yet goes under `declared_for`, never under
    `vars` or `paths`: its value names the package or branch that will read it and does not
    claim a read today. A name is cited or declared_for, never both.
This is a structural gate, not a semantic one: it cannot itself prove a citation is true.
That proof is the one-time job the accompanying RETURN records; ⚠️ re-run the grep behind a
citation before trusting it if the cited consumer file has moved on since.
"""

import json
import pathlib
import re
import xml.etree.ElementTree as ET

import pytest

REPO = pathlib.Path(__file__).resolve().parents[1]
TEMPLATES = REPO / "templates"
MANIFEST_PATH = REPO / "tests" / "consumption.json"

NAMES = sorted(p.stem for p in TEMPLATES.glob("*.xml"))


def _manifest():
    return json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))


def _template_vars(name):
    root = ET.parse(TEMPLATES / f"{name}.xml").getroot()
    return {
        (c.get("Target") or c.get("Name") or "").strip()
        for c in root.iter("Config")
        if (c.get("Type") or "Variable").strip().lower() == "variable"
    }


def _template_paths(name):
    root = ET.parse(TEMPLATES / f"{name}.xml").getroot()
    return {
        (c.get("Target") or "").strip()
        for c in root.iter("Config")
        if (c.get("Type") or "").strip().lower() == "path"
    }


def _declared_for(manifest):
    return manifest.get("declared_for") or {}


def test_manifest_exists_and_parses():
    assert MANIFEST_PATH.is_file(), f"{MANIFEST_PATH} is missing"
    manifest = _manifest()
    assert isinstance(manifest, dict) and manifest, "consumption.json is empty"


def test_every_template_has_a_manifest_entry():
    manifest = _manifest()
    entries = {k for k in manifest if not k.startswith("_")}
    missing = set(NAMES) - entries
    assert not missing, (
        f"templates/ has templates with no consumption.json entry: {sorted(missing)} — "
        f"a new template ships with either a per-var audit or no_local_consumer recorded"
    )
    stale = entries - set(NAMES)
    assert not stale, f"consumption.json names templates that no longer exist: {sorted(stale)}"


@pytest.mark.parametrize("name", NAMES)
def test_declared_variables_match_the_audited_variables_exactly(name):
    manifest = _manifest()[name]
    if manifest.get("no_local_consumer"):
        assert not manifest.get("vars") and not _declared_for(manifest), (
            f"{name}: flagged no_local_consumer but still carries per-variable citations — "
            f"either the consumer showed up under C:\\dev (audit it and clear the flag) or "
            f"these citations are stale (delete them)"
        )
        return
    declared = _template_vars(name)
    audited = set(manifest.get("vars") or {}) | (set(_declared_for(manifest)) & declared)
    missing = declared - audited
    assert not missing, (
        f"{name}: template declares Variable(s) with no consumption.json citation: "
        f"{sorted(missing)} — grep the consuming repo and either cite where it's read, or "
        f"delete the Config block if nothing reads it (per UT-N: a Variable earns its place "
        f"only if the consuming app actually reads it)"
    )
    stale = audited - declared
    assert not stale, (
        f"{name}: consumption.json cites Variable(s) the template no longer declares: "
        f"{sorted(stale)} — remove the stale citation"
    )


@pytest.mark.parametrize("name", NAMES)
def test_every_citation_is_non_trivial(name):
    # A one-word or blank citation ("yes", "-") records nothing a reviewer can re-check —
    # the whole point of the citation is that it names a file/line to go re-grep.
    manifest = _manifest()[name]
    for var, citation in (manifest.get("vars") or {}).items():
        assert isinstance(citation, str) and len(citation.strip()) >= 8, (
            f"{name}: {var}'s citation {citation!r} is too thin to mean anything — "
            f"name the file (and ideally line) that reads it"
        )


@pytest.mark.parametrize("name", NAMES)
def test_declared_paths_match_the_audited_paths_exactly(name):
    manifest = _manifest()[name]
    declared = _template_paths(name)
    cited = manifest.get("paths") or {}
    if manifest.get("no_local_consumer"):
        assert not cited, f"{name}: flagged no_local_consumer but still carries path citations"
        return
    audited = set(cited) | (set(_declared_for(manifest)) & declared)
    missing = declared - audited
    assert not missing, (
        f"{name}: template declares Path(s) with no consumption.json citation: {sorted(missing)} "
        f"— cite where the consumer reads each one under `paths`, or record it under "
        f"`declared_for` if no release reads it yet"
    )
    stale = set(cited) - declared
    assert not stale, f"{name}: consumption.json cites Path(s) the template no longer declares: {sorted(stale)}"


@pytest.mark.parametrize("name", NAMES)
def test_every_declared_for_names_a_declared_variable_or_path(name):
    manifest = _manifest()[name]
    declared = _template_vars(name) | _template_paths(name)
    stale = set(_declared_for(manifest)) - declared
    assert not stale, f"{name}: declared_for names {sorted(stale)}, which the template does not declare"


@pytest.mark.parametrize("name", NAMES)
def test_a_name_is_cited_or_declared_for_never_both(name):
    manifest = _manifest()[name]
    both = (set(manifest.get("vars") or {}) | set(manifest.get("paths") or {})) & set(_declared_for(manifest))
    assert not both, f"{name}: {sorted(both)} is both cited and declared_for — keep one"


def _declared_for_problems(value):
    """What is wrong with a declared_for value ([] = nothing): it must name a package, not claim a read."""
    if not isinstance(value, str) or len(value.strip()) < 8:
        return ["is too thin to name the package or branch that will read it"]
    if re.match(r"reads\b", value.strip()):
        return ["opens `reads`, a read claim: it belongs under vars or paths"]
    return []


@pytest.mark.parametrize("name", NAMES)
def test_every_declared_for_says_what_will_read_it(name):
    for target, value in _declared_for(_manifest()[name]).items():
        problems = _declared_for_problems(value)
        assert not problems, f"{name}: declared_for {target} {'; '.join(problems)} ({value!r})"


@pytest.mark.parametrize("name", NAMES)
def test_every_path_citation_is_non_trivial_and_opens_reads_its_path(name):
    for path, citation in (_manifest()[name].get("paths") or {}).items():
        assert isinstance(citation, str) and len(citation.strip()) >= 8, (
            f"{name}: {path}'s citation {citation!r} is too thin to mean anything — name the file "
            f"(and ideally line) that reads it"
        )
        problems = _path_problems(path, citation)
        assert not problems, (
            f"{name}: {path}'s citation {'; '.join(problems)}. A citation for a directory names "
            f"that directory, not a file under it: a file under it is a different Path entry."
        )


def _path_problems(path, citation):
    """What is wrong with `citation` as the record of where `path` is read ([] = nothing)."""
    if re.match(rf"reads {re.escape(path)}(?![\w/.-])", citation):
        return []
    return [f"does not start `reads {path}`"]


@pytest.mark.parametrize("path, citation, caught", [
    ("/data", "reads /data via DB_PATH, the default /data/cef_tracker.db (config.py:92)", False),
    ("/data", "reads /data/backups via scheduler.py:1835", True),
    ("/data", "the container reads /data via config.py:92", True),
    ("/kw_shared", "reads /kw_shared via alerting_config.py:45", False),
    ("/etc/squid/kw/destinations", "reads /etc/squid/kw/destinations/keystone.allow via squid.conf:50", True),
])
def test_the_path_rule_catches_a_citation_for_a_different_path(path, citation, caught):
    assert bool(_path_problems(path, citation)) is caught


@pytest.mark.parametrize("value, caught", [
    ("kb-service branch library-slice-1 (unreleased): kb_service/config.py:35 reads it", False),
    ("reads KB_DATABASE_URL at kb_service/config.py:35 on a later release", True),
    ("later", True),
])
def test_the_declared_for_rule_catches_a_read_claim_or_an_empty_reason(value, caught):
    assert bool(_declared_for_problems(value)) is caught


def _citation_problems(var, citation):
    """What is wrong with `citation` as the record of where `var` is read ([] = nothing)."""
    problems = []
    if not re.match(rf"reads {re.escape(var)}\b", citation):
        problems.append(f"does not start `reads {var}`")
    pydantic = re.search("pydantic", citation, re.IGNORECASE)
    fields = re.findall(r"\bfield `?(\w+)", citation) if pydantic else []
    if pydantic and not fields:
        problems.append("says pydantic but names no `field <name>`")
    quoted = ["".join(m) for m in re.findall(
        r"\$\{(\w+)|environ(?:\.get\(|\[)\s*[\"'](\w+)|getenv\(\s*[\"'](\w+)|process\.env\.(\w+)", citation)]
    misread = ([f"field {f} -> {f.upper()}" for f in fields if f.upper() != var]
               + [n for n in quoted if n != var])
    if misread:
        problems.append(f"cites a lookup of another env var ({', '.join(misread)})")
    words = sorted(var.lower().split("_"))
    swapped = sorted({t for t in re.findall(r"[A-Za-z][A-Za-z0-9]*(?:_[A-Za-z0-9]+)+", citation)
                      if t.lower() != var.lower() and sorted(t.lower().split("_")) == words})
    if swapped:
        problems.append(f"names {swapped}, which is {var}'s words in another order")
    return problems


@pytest.mark.parametrize("name", NAMES)
def test_every_citation_names_the_env_var_the_code_reads(name):
    for var, citation in (_manifest()[name].get("vars") or {}).items():
        problems = _citation_problems(var, citation)
        assert not problems, (
            f"{name}: {var}'s citation {'; '.join(problems)}. Find the env-var name the "
            f"consumer actually looks up (a pydantic field reads env_prefix + its name; this "
            f"check does not model env_prefix or aliases yet, extend it if a consumer uses one) and "
            f"start the citation `reads {var}` only if that is it; if it is not, the template "
            f"declares a Variable nothing reads"
        )


@pytest.mark.parametrize("var, citation, caught", [
    # The citation that let DB_BACKUP_USER ship while the app read BACKUP_DB_USER.
    ("DB_BACKUP_USER", "app/config.py:63,372-373 assembles the backup pg_dump SQLAlchemy URL", True),
    # Naming the Variable by rote while citing the field that actually reads another name.
    ("DB_BACKUP_USER", "reads DB_BACKUP_USER via settings field backup_db_user, app/config.py:63", True),
    ("APP_DB_USER", "reads APP_DB_USER via pydantic-settings field db_user in app/config.py", True),
    ("APP_DB_USER", "reads APP_DB_USER via pydantic-settings field app_db_user, like field db_user", True),
    ("DB_BACKUP_USER", "reads DB_BACKUP_USER via pydantic-settings field `backup_user` in app/config.py", True),
    ("DB_BACKUP_USER", "reads DB_BACKUP_USER via pydantic-settings field `db_backup_user` in app/config.py", False),
    ("DB_BACKUP_USER", "reads DB_BACKUP_USER via pydantic-settings in app/config.py:63", True),
    ("DB_BACKUP_USER", "reads DB_BACKUP_USER via Pydantic settings in app/config.py:63", True),
    ("DB_BACKUP_USER", "reads DB_BACKUP_USER via settings.backup_db_user in app/config.py:63", True),
    ("S3_BUCKET", "reads S3_BUCKET via pydantic-settings field s3_bucket in app/config.py", False),
    # A quoted lookup names the Variable itself.
    ("DB_BACKUP_USER", 'reads DB_BACKUP_USER via os.environ.get("BACKUP_USER") in app/backup.py', True),
    ("DB_BACKUP_USER", "reads DB_BACKUP_USER via os.environ['BACKUP_USER'] in app/backup.py", True),
    ("DB_BACKUP_USER", "reads DB_BACKUP_USER via os.getenv('BACKUP_USER') in app/backup.py", True),
    ("DB_BACKUP_USER", "reads DB_BACKUP_USER via os.getenv('db_backup_user') in app/backup.py", True),
    ("DB_BACKUP_USER", "reads DB_BACKUP_USER via os.getenv('DB_BACKUP_USER') in app/backup.py", False),
    ("WEB_CONCURRENCY", "reads WEB_CONCURRENCY via the shell: ${GUNICORN_WORKERS:-2}", True),
    ("WEB_CONCURRENCY", "reads WEB_CONCURRENCY via the shell: ${WEB_CONCURRENCY:-2}", False),
    ("KEYSTONE_API_URL", "reads KEYSTONE_API_URL via process.env.KEYSTONE_URL in lib/keystone.ts", True),
    ("KEYSTONE_API_URL", "reads KEYSTONE_API_URL via process.env.KEYSTONE_API_URL in lib/keystone.ts", False),
    # Prose that says "field" is not a pydantic field unless the citation is about pydantic.
    ("SCHWAB_PASS", "reads SCHWAB_PASS via _env() in reauth.py; filled into the password field of the form", False),
    # `reads <VAR>` is the citation's opening claim, spelled exactly.
    ("DB_BACKUP_USER", "reads DB_BACKUP_USERS at app/config.py:63", True),
    ("DB_BACKUP_USER", "reads BACKUP_DB_USER via settings field backup_db_user, app/config.py:63", True),
    ("DB_BACKUP_USER", "nothing reads DB_BACKUP_USER at app/config.py:63", True),
    ("DB_BACKUP_USER", "reads the DB_BACKUP_USER setting from app/config.py:63", True),
    ("DB_USER", "reads APP_DB_USER via os.environ.get in app/config.py", True),
    ("DB_BACKUP_USER", "threads DB_BACKUP_USER through app/config.py:63", True),
    ("DB_BACKUP_USER", "read DB_BACKUP_USER via os.environ.get at app/backup.py:12", True),
    ("DB_BACKUP_USER", "reads db_backup_user via os.environ.get at app/backup.py:12", True),
    # Another name built from the Variable's words, two-word and with digits.
    ("DB_USER", "reads DB_USER via the shell; entrypoint.sh exports USER_DB", True),
    ("S3_BUCKET", "reads S3_BUCKET via os.environ.get; app.py passes BUCKET_S3 on", True),
    ("DB_BACKUP_USER", "reads DB_BACKUP_USER via os.environ.get at app/backup.py:12", False),
    ("BACKUP_DB_USER", ("reads BACKUP_DB_USER via pydantic-settings field backup_db_user in app/config.py "
                        "(no env_prefix, case-insensitive)"), False),
])
def test_the_citation_rule_catches_a_citation_that_never_names_what_is_read(var, citation, caught):
    assert bool(_citation_problems(var, citation)) is caught


def test_the_citation_rule_names_the_env_var_a_wrong_field_reads():
    problems = _citation_problems("APP_DB_USER", "reads APP_DB_USER via pydantic-settings field db_user")
    assert "field db_user -> DB_USER" in "; ".join(problems)
