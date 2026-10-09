#!/usr/bin/env python3
"""
sync-templates — reconcile Unraid dockerMan container templates against the
published `unraid-templates` repo WITHOUT losing your applied values.

NO PARAMETERS, NO WRAPPER. Running it does create / update / drop-deprecated
for the ONE repo template that TEMPLATE (below) names:

  TEMPLATE = None     NOT SET: refuses before anything is read or written. This is the
                      repo copy; there is no run over every template (unraid-templates#102).
  TEMPLATE = "tape"   ONLY the `tape` repo template and its live instances. Nothing
                      that maps to another template is created, updated, deleted,
                      or has its backups redacted or pruned — `tape-db` included,
                      since instances are mapped against the FULL template list
                      (see MAPPING below). Refused before anything is written: a
                      name the repo does not have; a my-tape.xml that is really
                      another template's instance or a foreign container (case
                      variants of the name, e.g. my-Tape.xml, included); and a
                      templates dir that cannot be listed. A backup whose
                      instance is gone, unmapped or unreadable belongs to no
                      run (delete it by hand) — except my-tape.xml's, which are
                      tape's (unless that file maps to another template, when
                      tape refuses).

  ONE USER SCRIPT PER TEMPLATE: each installed copy is identical to this file
  apart from its TEMPLATE line, so syncing one template can never ship another
  template's merged-but-not-intended changes.

  CREATE  — seed my-<name>.xml for the template if it has no my- file yet,
            so it is ready to pick in Add Container.
  UPDATE  — for EVERY live instance of a template (tape: my-tape.xml AND
            my-tape-dev.xml, ...; my-tape-db-dev.xml is tape-db's): keep each
            instance's applied values, refresh each variable's metadata from the
            template, and ADD new template vars.
  DROP    — a variable the instance has and the repo template lacks is deprecated and is
            dropped, whatever value it holds; each drop is named in the run output
            ("DROPPED, not in repo template (deprecated): NAME (held a value)").

THE RULE: the repo template is the schema (names + neutral defaults); the live instance
is the values. A sync copies each applied value into the matching repo variable and adopts
the repo's metadata. Not in the repo template = deprecated = dropped. To retire a variable,
remove it from the repo template. To add one, add it to the repo template FIRST, then sync,
then set its value live. Never add a variable to a live instance only: the next sync drops it.

  Container-level settings you set per instance — image tag, network/IP, WebUI,
  Extra Params, ports, the container Name — are ALWAYS preserved. Only <Config>
  elements are reconciled.
  PATH MODE IS OPERATOR-OWNED, once seeded — a Path's Read/Write-vs-Read-only Mode you
  set on an instance survives every later sync, even when the template's own Mode
  changes; a template-side Mode change reaches only a newly-seeded my-<name>.xml, never
  an existing instance. Every other attribute of every Config — including a Port's
  tcp/udp Mode — keeps refreshing from the template on every run, same as always.

------------------------------------------------------------------------------
DRY-RUN vs LIVE  —  the DRY_RUN constant below is the switch.
  * DRY_RUN = True   prints exactly what it WOULD create/update/drop, writes
                     nothing. This is the version you validate.
  * DRY_RUN = False  performs the changes (each overwritten file is backed up
                     first, timestamped, under templates-user/.template-sync-backups/;
                     writes are atomic; a result is validated before it replaces
                     the original).

BACKUPS AND SECRETS  (unraid-templates#27)
  `Mask="true"` is a UI setting only -- it makes the Unraid web form render a
  password box. The XML on the flash drive holds the value in PLAINTEXT either
  way. So a backup is written with every masked value REDACTED (masked in the
  instance file OR in the repo template, #104), and each run also redacts the
  backups previous versions already wrote. Backups are
  pruned to the newest KEEP_BACKUPS per instance.

  What that means for a restore: the structure and every non-secret value come
  back in full (except a mirror entry with no Config, redacted as unknown); a
  masked value must be re-entered. It is not a real loss --
  merge() copies applied values across verbatim, so a merge cannot damage a
  secret, and the backup is there for a bad merge.
  Workflow: run the dry-run version -> review -> when it is correct, install the
  version with DRY_RUN = False. Never a parameter; a copy differs from this file
  only in its TEMPLATE line (and DRY_RUN while you rehearse).
------------------------------------------------------------------------------

ONE COMMIT PER RUN  (unraid-templates#103)
  BRANCH is resolved to its commit once; the listing and the template body are read at
  that commit, and the first output line names it. 2 api.github.com requests per run
  (unauthenticated limit: 60/hour per IP); a run that hits the limit stops before any
  write and says when to retry. GitHub caches the commit lookup up to 60 s, so a
  run right after a merge may use the commit before it; the first line shows which.

INSTANCE -> TEMPLATE MAPPING
  Primary: the instance's <TemplateURL> basename (my-tape-dev.xml -> tape.xml).
  Fallback: longest dash-prefix of the filename (my-tape-db-dev.xml -> tape-db,
  never tape; my-tapeworm.xml -> nothing). Foreign containers are never touched.

REQUIRES  python3 >= 3.9 (stdlib only). Run on the Unraid host via User Scripts.
"""

# =============================================================================
DRY_RUN = False    # LIVE — creates/updates/drops with timestamped backups. (dev phase used True)
TEMPLATE = None    # "<name>" = only templates/<name>.xml and its instances. None = unset: refuses.
# =============================================================================

import copy
import json
import os
import re
import sys
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime

# ----------------------------------------------------------------------------- config
REPO            = "sdr-ventures/unraid-templates"
BRANCH          = "main"
TEMPLATE_SUBDIR = "templates"
TEMPLATES_USER  = "/boot/config/plugins/dockerMan/templates-user"
BACKUP_SUBDIR   = ".template-sync-backups"
UA              = "sync-templates (unraid)"
TIMEOUT         = 30

# ----------------------------------------------------------------------------- secrets
# ⭐ WHAT REPLACES A MASKED VALUE IN A BACKUP (unraid-templates#27).
#
# `Mask="true"` is a UI affordance ONLY — it tells the Unraid web form to render the field as a
# password box. The XML on the flash drive stores the value in PLAINTEXT regardless. So the old
# `shutil.copy2` backup wrote a byte-for-byte cleartext copy of every API token, PAT and password
# in the instance, and did it on EVERY write, and never pruned. A flash drive that anyone with
# physical access can read then accumulated one more cleartext copy of each secret per run.
#
# The backup exists to recover from a bad merge, and the value of a secret is not what it
# recovers: `merge()` copies every applied value across VERBATIM (`new_c.text = op_c.text`),
# so a merge cannot corrupt a secret in the first place. (A variable the template lacks is
# dropped on purpose, so the backup is also the only record of one.) What a restore actually needs is the STRUCTURE and the non-secret values, and those
# are preserved in full. A masked value is re-entered from wherever it came from.
REDACTED = "***REDACTED***"

# How many backups to keep per instance file. The old code kept every backup forever, which is
# half of what #27 is about: even redacted, an unbounded pile of timestamped XML on a flash drive
# is just accumulation. The newest are the ones with any recovery value.
KEEP_BACKUPS = 10


# ----------------------------------------------------------------------------- http
def _get(url, accept=None):
    headers = {"User-Agent": UA}
    if accept:
        headers["Accept"] = accept
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            return r.read()
    except urllib.error.HTTPError as e:
        # Unauthenticated api.github.com allows 60 requests/hour per IP and a run makes 2 (the
        # commit and the listing; raw.githubusercontent.com is not counted). Say so, with the retry
        # time when GitHub gives one, rather than a bare "HTTP Error 403" that reads like a firewall.
        # A secondary limit may say so only in the body, so that is checked too.
        h = e.headers or {}
        if e.code in (403, 429):
            try:
                body = e.read()[:2000].lower()
            except Exception:
                body = b""
            if h.get("X-RateLimit-Remaining") == "0" or h.get("Retry-After") or b"rate limit" in body:
                when = "later"
                reset, after = h.get("X-RateLimit-Reset") or "", h.get("Retry-After") or ""
                if reset.isdigit():
                    try:
                        when = "after " + datetime.fromtimestamp(int(reset)).strftime(
                            "%Y-%m-%d %H:%M:%S") + " host time"
                    except (OverflowError, OSError, ValueError):
                        pass
                elif after:
                    when = f"in {after} s" if after.isdigit() else f"after {after}"
                raise RuntimeError(f"GitHub rate limit reached (api.github.com allows 60 "
                                   f"unauthenticated requests/hour per IP; a run makes 2); "
                                   f"retry {when}") from e
        raise


def resolve_sha():
    """The commit BRANCH points at, resolved ONCE per run (unraid-templates#103).

    The listing and every template body are then read at this commit. Reading bodies by BRANCH
    from raw.githubusercontent.com (cached up to 300 s) could merge a run against a template
    from before the commit its own listing came from; a commit URL is immutable.
    """
    url = f"https://api.github.com/repos/{REPO}/commits/{BRANCH}"
    sha = _get(url, accept="application/vnd.github.sha").decode("utf-8", "replace").strip()
    if not re.fullmatch(r"[0-9a-f]{40}", sha):
        raise ValueError(f"resolving {BRANCH} did not return a commit sha: {sha[:60]!r}")
    return sha


def list_repo_templates(sha):
    """Enumerate <name> for every .xml under templates/ at commit `sha` (via the GitHub API)."""
    url = f"https://api.github.com/repos/{REPO}/contents/{TEMPLATE_SUBDIR}?ref={sha}"
    data = json.loads(_get(url).decode("utf-8"))
    return sorted(
        e["name"][:-4] for e in data
        if e.get("type") == "file" and e.get("name", "").endswith(".xml")
    )


def fetch_template(name, sha):
    url = f"https://raw.githubusercontent.com/{REPO}/{sha}/{TEMPLATE_SUBDIR}/{name}.xml"
    return _get(url)


# ----------------------------------------------------------------------------- xml helpers
def config_key(el):
    """Stable identity of a <Config>: (Type, Target) — fall back to Name if no Target."""
    typ = (el.get("Type") or "").strip()
    tgt = (el.get("Target") or "").strip()
    if not tgt:
        tgt = "name:" + (el.get("Name") or "").strip()
    return (typ, tgt)


def validate(root):
    def nonempty(tag):
        e = root.find(tag)
        return e is not None and (e.text or "").strip() != ""
    if root.tag != "Container":
        return f"root element is <{root.tag}>, expected <Container>"
    if not nonempty("Name"):
        return "missing/empty <Name>"
    if not nonempty("Repository"):
        return "missing/empty <Repository>"
    return None


def map_instance(path, fname, repo_names):
    """Return (repo template name or None, read/parse error or None).

    ⚠️ Only (ET.ParseError, OSError) count as an error. The old `except Exception: tu = ""`
    swallowed a real read/parse failure on one of OUR OWN instances the same way it swallowed a
    merely-empty <TemplateURL>, so a corrupt my-*.xml that also missed the filename fallback was
    printed under "foreign / not from these templates" — wrong: it is ours, and broken. The
    caller reports an error distinctly from a genuine naming miss.
    """
    error = None
    tu = ""
    try:
        tu = (ET.parse(path).getroot().findtext("TemplateURL") or "").strip()
    except (ET.ParseError, OSError) as e:
        # basename, not str(e) alone: an OSError's message embeds the full path it was raised
        # against, and every other error string in this script (backup()'s BackupUnsafe) is
        # basename-scoped — this keeps the convention consistent wherever a path could leak in.
        detail = getattr(e, "strerror", None) or str(e)
        error = f"{os.path.basename(path)}: {detail}"
    base = os.path.basename(tu)
    if base.endswith(".xml") and base[:-4] in repo_names:
        return base[:-4], None                              # primary: TemplateURL
    stem = fname[3:-4]                                       # strip 'my-' and '.xml'
    cands = [n for n in repo_names if stem == n or stem.startswith(n + "-")]
    if cands:
        return max(cands, key=len), None                    # longest dash-prefix wins
    return None, error


def discover_instances(directory, repo_names):
    """Map every my-*.xml in the dir to its template. Returns (by_template, unmapped, broken).

    `broken` is a my-*.xml this script could not even read/parse to find out — kept separate
    from `unmapped` (a file that read fine and genuinely matched no template), which is the
    only bucket "foreign / not from these templates" is true of.
    """
    by_template, unmapped, broken = {}, [], []
    try:
        names = sorted(os.listdir(directory))
    except OSError as e:
        # Same class as #61/#60's other sites: a directory that exists but cannot be LISTED
        # (permission revoked mid-run, a flaky mount) must not crash main() before a single
        # template is processed.
        broken.append((os.path.basename(directory.rstrip("/\\")), f"could not list the directory: {e}"))
        return by_template, unmapped, broken
    for fname in names:
        if not (fname.startswith("my-") and fname.endswith(".xml")):
            continue
        path = os.path.join(directory, fname)
        t, error = map_instance(path, fname, repo_names)
        if t:
            by_template.setdefault(t, set()).add(path)
        elif error:
            broken.append((fname, error))
        else:
            unmapped.append(fname)
    return by_template, unmapped, broken


def merge(operator_root, template_root):
    """Return (merged_root, stats). Base = operator tree; reconcile <Config> only.
    A Config the operator has but the template lacks is deprecated and always dropped,
    whatever it holds; stats["dropped"] lists (label, held_a_value) for each, and
    stats["mirror_dropped"] each legacy <Environment> mirror entry with no template Target."""
    op_by_key, dup_keys = {}, []
    for c in operator_root.findall("Config"):
        k = config_key(c)
        if k in op_by_key:
            dup_keys.append(k)
        else:
            op_by_key[k] = c

    merged = copy.deepcopy(operator_root)
    for c in merged.findall("Config"):          # strip Configs; container tags stay
        merged.remove(c)

    stats = {"added": [], "retained": 0, "dropped": [], "dupes": dup_keys, "mode_kept": [],
             "mirror_dropped": []}
    seen = set()

    for tc in template_root.findall("Config"):  # template order; refresh metadata
        k = config_key(tc)
        seen.add(k)
        new_c = copy.deepcopy(tc)               # template metadata + default value
        if k in op_by_key:
            op_c = op_by_key[k]
            new_c.text = op_c.text              # keep the applied value verbatim
            # Path Mode (Read/Write vs Read-only) is OPERATOR-owned once an instance has
            # been seeded — it is how an operator expresses something the template cannot
            # know (a slave/secondary mount, say). Every other attribute, including Port
            # Mode (tcp/udp), keeps refreshing from the template every run, same as before.
            # A blank/absent operator Mode is not an operator decision to preserve — it
            # takes the template's, same as a Config new to this instance always does.
            if (tc.get("Type") or "").strip() == "Path":
                op_mode = (op_c.get("Mode") or "").strip()
                tpl_mode = (tc.get("Mode") or "").strip()
                if op_mode:
                    new_c.set("Mode", op_mode)
                    if op_mode != tpl_mode:
                        # A Mode-only difference is otherwise invisible: `new_c` ends up
                        # identical to the operator's own Config (the one attribute the
                        # template changed is the one just overwritten back), so the
                        # content-comparison gate below sees NO change at all and the run
                        # would silently say "nothing to change" while quietly keeping the
                        # operator's Mode against a template that now disagrees with it.
                        stats["mode_kept"].append((tc.get("Name") or k[1], op_mode, tpl_mode))
            stats["retained"] += 1
        else:
            stats["added"].append(tc.get("Name") or k[1])
        merged.append(new_c)

    for k, c in op_by_key.items():              # not in the template = deprecated = dropped
        if k not in seen:
            stats["dropped"].append((c.get("Name") or k[1], bool((c.text or "").strip())))

    # dockerMan's legacy <Environment><Variable> mirror repeats each value. Dropping a Config left
    # its mirror, value and all, in the live file, where no Config marks it secret any more, so
    # every later backup copied it in clear. A mirror with no template Target is dropped with it.
    targets = {_config_name(tc) for tc in template_root.findall("Config")}
    for env in merged.findall("Environment"):
        for var in env.findall("Variable"):
            name = (var.findtext("Name") or "").strip()
            if name not in targets:
                env.remove(var)
                stats["mirror_dropped"].append(name)

    return merged, stats


def canonical(root):
    """Serialise a template tree the way atomic_write would, for content comparison."""
    r = copy.deepcopy(root)
    ET.indent(r, space="  ")
    return ET.tostring(r, encoding="utf-8")


def atomic_write(path, root):
    ET.indent(root, space="  ")
    tmp = path + ".tmp"
    ET.ElementTree(root).write(tmp, encoding="utf-8", xml_declaration=False)
    os.replace(tmp, path)


class BackupUnsafe(Exception):
    """The instance could not be backed up in REDACTED form, so it is not backed up at all.

    Raised rather than falling back to a plaintext copy. A fallback that quietly did the unsafe
    thing when the safe one failed would reintroduce #27 on exactly the malformed files nobody
    looks at, and "it only leaks sometimes" is not a fix.
    """


def _is_masked(c):
    return (c.get("Mask") or "").strip().lower() == "true"


def _config_name(c):
    """The variable a <Config> sets: its Target, or its Name when it has no Target."""
    return (c.get("Target") or "").strip() or (c.get("Name") or "").strip()


def _masked_configs(root, secret_targets=()):
    """Every secret <Config> anywhere under `root`: masked in this file, OR named in
    `secret_targets` (the variables the REPO template masks — unraid-templates#104).

    ⚠️ THE FILE'S OWN MASK IS NOT ENOUGH. A live Config can hold a secret with `Mask="false"` or
    no Mask at all while the repo template masks that variable; trusting the file alone wrote that
    value into the backup in clear text. Either side saying "secret" makes it one.

    `iter` and NOT `findall`: `findall("Config")` is direct children only, so a masked <Config>
    nested one level down was skipped entirely. Over-reaching costs nothing here — this only ever
    runs against a BACKUP copy, where redacting one element too many is harmless and missing one
    is the bug.
    """
    return [c for c in root.iter("Config") if _is_masked(c) or _config_name(c) in secret_targets]


def _masked_names(root, secret_targets=()):
    """The environment-variable KEYS the masked <Config> elements correspond to.

    ⚠️ `Target` IS THE VARIABLE NAME; `Name` is the human label the UI shows ("Key", "API token").
    The <Environment> mirror keys on the VARIABLE, so matching the human label as well made an
    unrelated variable that merely shared a label get redacted — destroying a non-secret value a
    restore would want, and counting it, so the run reported more redactions than it performed.
    `Name` is used only as a fallback for a <Config> that has no Target.
    """
    return {_config_name(c) for c in _masked_configs(root, secret_targets)} - {""}


def _mirrored_secrets(root, secret_targets=()):
    """The <Environment><Variable><Value> elements that mirror a masked <Config>.

    ⭐ dockerMan WRITES EACH VARIABLE TWICE. Alongside the <Config> elements this script
    reconciles, `my-*.xml` carries a legacy

        <Environment><Variable><Value>s3cret</Value><Name>TOKEN</Name></Variable></Environment>

    block holding the SAME value. `merge()` deepcopies the operator tree and only reconciles
    <Config>, so that block is preserved verbatim — which meant redacting the <Config> half left
    the secret sitting in the <Environment> half, in cleartext, in every backup, while the run
    reported the value redacted. Both agents that reviewed this reproduced it independently.

    Matched by NAME against the secret <Config> set rather than by any flag of its own: the
    <Variable> element carries no `Mask` attribute, so the <Config> is the only place that says
    whether the value is a secret. ⛔ So a mirror with NO <Config> of its name in the file (its
    Config was dropped by an earlier sync, which leaves the mirror behind) cannot be shown to be
    safe, and is redacted: fail closed, never print a value nothing vouches for.
    """
    secret_names = _masked_names(root, secret_targets)
    configured = {_config_name(c) for c in root.iter("Config")}
    found = []
    for var in root.iter("Variable"):
        name = (var.findtext("Name") or "").strip()
        if name in secret_names or name not in configured:
            value = var.find("Value")
            if value is not None:
                found.append(value)
    return found


def _element_holds_a_secret(el):
    """Does this element still carry an unredacted value?

    ⚠️ CHILD ELEMENTS COUNT, and missing that was a silent leak. `el.text` is only the text BEFORE
    the first child, so on `<Config Mask="true">pre<b>SECRET</b></Config>` setting `el.text` alone
    left the secret sitting in the child — while the run COUNTED the field as redacted and
    reported the file clean. Worse, the idempotence check then saw a redacted `text` and never
    looked at that file again, so the value was permanently classified as cleared. A false "this
    is now safe" is worse than no clear-out at all.
    """
    if (el.text or "").strip() not in ("", REDACTED):
        return True
    return len(el) > 0


def redact_secrets(root, secret_targets=()):
    """Blank every masked value on `root`, IN PLACE. Returns how many were redacted.
    `secret_targets`: the variables the repo template masks (see `_masked_configs`).

    Covers BOTH places dockerMan stores a variable: the <Config> element and its <Environment>
    mirror. Only a field that actually HOLDS something is touched — marking an empty one would
    claim it held a secret when it did not, and an operator reading a backup to see what was
    configured would be misled.
    """
    n = 0
    for el in _masked_configs(root, secret_targets) + _mirrored_secrets(root, secret_targets):
        if not _element_holds_a_secret(el):
            continue
        for child in list(el):      # the child elements are content too
            el.remove(child)
        el.text = REDACTED
        n += 1
    return n


def count_secrets(root, secret_targets=()):
    """How many masked values `root` still holds. The exact converse of `redact_secrets`, so the
    reported count and the work actually done cannot drift apart."""
    return sum(1 for el in _masked_configs(root, secret_targets)
               + _mirrored_secrets(root, secret_targets) if _element_holds_a_secret(el))


def backup(path, backup_dir, secret_targets=()):
    """Copy `path` aside before it is overwritten, with every masked value REDACTED — masked in
    the file OR in the repo template (`secret_targets`, unraid-templates#104).

    Returns (dest, n_redacted). Raises BackupUnsafe if the file cannot be parsed — see the class.

    ⚠️ RE-SERIALISED, NOT COPIED. The backup is written through the same indent+serialise path as
    the live file, so it is not byte-identical to the original: it loses any XML declaration and
    is re-indented. That costs nothing to a restore — it is exactly the form `atomic_write` gives
    the live file anyway — and it is what makes redaction possible at all.
    """
    try:
        tree = ET.parse(path)
    except (ET.ParseError, OSError) as e:
        raise BackupUnsafe(f"{os.path.basename(path)}: {e}") from e
    n = redact_secrets(tree.getroot(), secret_targets)
    # ⛔ A WRITE FAILURE HERE IS ALSO "could not back it up safely". `redact_existing_backups` was
    # hardened so one bad file could not abort the sync, and this sibling path was left raising —
    # so a full or read-only flash drive gave a traceback out of main(), a half-finished run, and
    # an orphaned `.tmp` nothing ever cleans up (`_backup_files` only matches `.bak`). Same class,
    # same handling: the caller's "no backup, no write" branch takes it from here.
    dest = ""
    try:
        os.makedirs(backup_dir, exist_ok=True)
        dest = _unused_backup_path(backup_dir, os.path.basename(path))
        atomic_write(dest, tree.getroot())
    except OSError as e:
        if dest:
            _discard(dest + ".tmp")
        raise BackupUnsafe(f"{os.path.basename(path)}: could not be written: {e}") from e
    return dest, n


def _unused_backup_path(backup_dir, base):
    """A backup path that does not already exist.

    ⚠️ THE STAMP IS ONE-SECOND RESOLUTION, so two backups of the same instance in the same second
    collided and the second SILENTLY REPLACED the first. That is a backup destroying a backup —
    the one thing this file must not do, now that the backup is the whole recovery story.

    The counter goes INSIDE the stamp segment (`...-2`) rather than after `.bak`, so the name
    still matches `_BAK_RX` and stays prunable. Ordering between two backups taken in the same
    second is arbitrary, which is fine: they are simultaneous. Ordering against OTHER seconds is
    preserved, which is what "keep the newest N" actually depends on.
    """
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    dest = os.path.join(backup_dir, f"{base}.{stamp}.bak")
    n = 1
    while os.path.exists(dest):
        n += 1
        dest = os.path.join(backup_dir, f"{base}.{stamp}-{n}.bak")
    return dest


# ----------------------------------------------------------------------------- backup hygiene
# `<instance>.<YYYYmmdd-HHMMSS>[-<n>].bak` — the shape THIS script writes, and the only shape it
# will ever delete. `rsplit(".", 2)` was doing this job by position and got it wrong for anything
# else in the directory: an operator's `my-tape.xml.BEFORE-MIGRATION.bak` landed in the same group
# as the real backups, and since letters sort after digits it counted as the NEWEST — so the three
# genuine timestamped backups were pruned and the hand-named file became immortal.
_BAK_RX = re.compile(r"^(?P<inst>.+)\.(?P<stamp>\d{8}-\d{6}(?:-\d+)?)\.bak$")


def _backup_files(backup_dir):
    """Every `.bak` in the backup dir, oldest first, and any error LISTING the directory.

    Returns (files, error). Absent dir is a legitimate answer, not an error (a first run happens
    before anything creates it) — `error` is only set when the dir EXISTS but `os.listdir` itself
    raised (permission revoked, a flaky network mount): that used to propagate straight out of
    `main()` before either caller (`redact_existing_backups`, `prune_backups`) processed a single
    file, the same crash-mid-loop shape unraid-templates#61 fixed at every other call site.
    """
    if not os.path.isdir(backup_dir):
        return [], None
    try:
        return sorted(f for f in os.listdir(backup_dir) if f.endswith(".bak")), None
    except OSError as e:
        return [], str(e)


def _backup_owner(fname, instances):
    """The instance file that backup `fname` was taken of, or None (an orphan).

    The LONGEST of `instances` that `fname` extends with a dot: `my-tape-db.xml.*` is never a
    `my-tape.xml` backup, and `my-tape.xml.old.xml.*` belongs to that instance, not `my-tape.xml`.
    """
    owners = [i for i in instances if fname.startswith(i + ".")]
    return max(owners, key=len) if owners else None


def _same_file(a, b):
    """os.path.samefile, and False when either cannot be stat'd (absent, unreadable)."""
    try:
        return os.path.samefile(a, b)
    except OSError:
        return False


def redact_existing_backups(backup_dir, scope=None, secret_targets=()):
    """ONE-TIME CLEAR-OUT of the plaintext accumulation already on the flash drive.

    Fixing `backup()` stops NEW cleartext copies; it does nothing about the pile already written,
    which is where every secret this script has ever seen is currently sitting. Each existing
    `.bak` is rewritten in redacted form, ATOMICALLY (tmp + `os.replace`) so an interrupted run
    cannot truncate one.

    Rewritten rather than deleted: a backup is the operator's data and the recoverable part of it
    — the structure and every non-secret value — is exactly what deleting would throw away.

    Returns (n_redacted_files, n_values, unparseable). An unparseable `.bak` is REPORTED, never
    silently deleted and never assumed safe: it may well hold a secret, and only the operator can
    say whether it is worth keeping.

    Idempotent — a second run finds nothing left to redact, because a redacted value no longer
    differs from the marker.

    `secret_targets` (the repo template's masked variables) also redacts a backup whose own Config
    was not masked when it was taken (unraid-templates#104) — the scope must then be one
    template's backups, since the set is that template's.
    """
    redacted_files, redacted_values, unreadable = 0, 0, []
    files, list_error = _backup_files(backup_dir)
    if list_error:
        unreadable.append((os.path.basename(backup_dir.rstrip("/\\")),
                            f"could not list the backup dir: {list_error}"))
        return redacted_files, redacted_values, unreadable
    for fname in files:
        if scope is not None and not scope(fname):
            continue
        full = os.path.join(backup_dir, fname)
        try:
            tree = ET.parse(full)
        except ET.ParseError as e:
            unreadable.append((fname, str(e)))
            continue
        except OSError as e:
            unreadable.append((fname, f"could not be read: {e}"))
            continue
        root = tree.getroot()
        n = count_secrets(root, secret_targets)
        if not n:
            continue
        if not DRY_RUN:
            try:
                redact_secrets(root, secret_targets)
                atomic_write(full, root)
            except OSError as e:
                # ⛔ ONE BAD FILE MUST NOT ABORT THE SYNC. An uncaught OSError here (a full flash
                # drive, a read-only mount) propagated out of main() BEFORE a single template was
                # processed, so a disk problem in the housekeeping step silently became "the sync
                # does nothing". Report it and carry on with the rest.
                unreadable.append((fname, f"could not be rewritten: {e}"))
                _discard(full + ".tmp")   # atomic_write's partial file, if it got that far
                continue
        redacted_files += 1
        redacted_values += n
    return redacted_files, redacted_values, unreadable


def _discard(path):
    """Remove a leftover temp file, best-effort. It is redacted content, not a leak — but nothing
    else ever cleans it up, since `_backup_files` only matches `.bak`."""
    try:
        os.remove(path)
    except OSError:
        pass


def prune_backups(backup_dir, protected=(), scope=None):
    """Keep the newest KEEP_BACKUPS per instance file; drop the rest.

    Returns (dropped, failed) — `dropped` is what was actually removed; `failed` is
    [(filename, reason), ...] for a file this function chose to prune but an OSError (a
    read-only mount, a concurrent delete) stopped it from removing. A failed remove is neither
    dropped (it is still on disk) nor silently ignored — the caller reports and counts it,
    the same policy as every other write/remove site in this script (unraid-templates#61).

    Grouped per instance rather than over the directory as a whole, so a container synced often
    cannot evict the only backup another container has. The stamp is `%Y%m%d-%H%M%S`, so a plain
    lexicographic sort IS chronological.

    ⛔ TWO THINGS THIS WILL NOT DELETE, both of which it used to:
      * anything whose name is not the `<instance>.<stamp>.bak` shape THIS script writes. A file
        an operator dropped in here by hand is not ours to remove, and treating it as a backup
        also corrupted the ordering — see `_BAK_RX`.
      * anything in `protected`. `redact_existing_backups` reports an unparseable `.bak` and
        promises the operator it is left for them to review; pruning it three lines later in
        `main()` deleted the very file the same run told them to go and look at, and printed its
        name while doing so.
    """
    groups = {}
    files, list_error = _backup_files(backup_dir)
    dropped, failed = [], []
    if list_error:
        failed.append((os.path.basename(backup_dir.rstrip("/\\")),
                        f"could not list the backup dir: {list_error}"))
        return dropped, failed
    for fname in files:
        if fname in protected or (scope is not None and not scope(fname)):
            continue
        m = _BAK_RX.match(fname)
        if not m:
            continue
        groups.setdefault(m.group("inst"), []).append(fname)
    for _, files in sorted(groups.items()):
        for fname in sorted(files)[:-KEEP_BACKUPS]:
            if not DRY_RUN:
                try:
                    os.remove(os.path.join(backup_dir, fname))
                except OSError as e:
                    failed.append((fname, str(e)))
                    continue
            dropped.append(fname)
    return dropped, failed


# ----------------------------------------------------------------------------- per-instance
def update_instance(inst_path, tpl_root, backup_dir):
    """Reconcile one my-*.xml instance against its template. Returns True on success (including
    "nothing to do"), False on any SKIP/failure — the caller counts False as a failure
    (unraid-templates#61/#62)."""
    fname = os.path.basename(inst_path)
    try:
        op_root = ET.parse(inst_path).getroot()
    except (ET.ParseError, OSError) as e:
        print(f"    ! {fname:<22} SKIP — XML parse error: {e}")
        return False
    merged, st = merge(op_root, tpl_root)
    err = validate(merged)
    if err:
        print(f"    ! {fname:<22} SKIP — merged result invalid ({err}); left untouched")
        return False
    # Compare the SEMANTIC result, not the variable set. Gating on added/dropped alone meant a
    # template edit that changed only a field's Description, Default, Display or Required was
    # computed correctly by merge() and then thrown away — and a Description is where every
    # operator-facing instruction in this repo lives, so the one edit the templates are
    # written to deliver was the one edit that never arrived. Both sides go through the same
    # indent+serialise path so this compares content, not the file's original formatting.
    # mode_kept is the ONE stat this comparison cannot see: a preserved operator Path Mode
    # makes merged equal the operator's tree, so it must still be reported. dupes rides along
    # for safety, though for an operator-side duplicate it is redundant: merge() keeps only
    # the first of a duplicate-keyed Config, which changes the merged tree.
    unchanged = canonical(merged) == canonical(op_root)
    if unchanged and not (st["dupes"] or st["mode_kept"]):
        print(f"    = {fname:<22} up to date  ({st['retained']} values, nothing to change)")
        return True
    # "metadata refreshed ... no variables added or removed" must be true of the run that
    # prints it. dupes belongs in here because merge() DROPS all but the first duplicate,
    # which IS a variable removed.
    meta_only = not (st["added"] or st["dropped"] or st["dupes"] or st["mode_kept"]
                     or st["mirror_dropped"])
    if unchanged:
        # nothing to write, but there IS something to say - fall through to the warnings
        print(f"    = {fname:<22} no write needed, but see below")
    elif DRY_RUN:
        print(f"    * {fname:<22} would UPDATE")
    else:
        # ⛔ NO BACKUP, NO WRITE. If the pre-write copy cannot be made SAFELY, the instance is
        # left exactly as it was: the alternative is either overwriting with no way back, or
        # falling back to the plaintext copy that #27 exists to remove.
        #
        # ⚠️ Belt-and-braces, and worth knowing it: `update_instance` already parsed this file
        # successfully above, so `backup()`'s own parse of the same unmodified file only fails on
        # a concurrent modification between the two. It is kept because `backup()`'s contract is
        # "raise rather than write a plaintext copy" and a caller that assumed otherwise is
        # exactly how this bug returns — not because this branch is expected to fire.
        try:
            b, masked = backup(inst_path, backup_dir, _masked_names(tpl_root))
        except BackupUnsafe as e:
            print(f"    ! {fname:<22} SKIP — could not back it up safely ({e}); left untouched")
            return False
        try:
            atomic_write(inst_path, merged)
        except OSError as e:
            # A backup was already taken above, so the operator's applied values are not at
            # risk — only this run's write failed. Same policy as every other write site: no
            # orphaned .tmp, report by name, count as a failure, move on.
            _discard(inst_path + ".tmp")
            print(f"    ! {fname:<22} SKIP — could not write it ({e}); left untouched")
            return False
        note = f", {masked} masked value(s) REDACTED" if masked else ""
        print(f"    * {fname:<22} UPDATED   (backup: {os.path.basename(b)}{note})")
    print(f"        values kept   : {st['retained']}")
    if meta_only:
        print("        metadata refreshed (descriptions/defaults/visibility); no variables added or removed")
    if st["added"]:
        print(f"        added         : {', '.join(st['added'])}")
    for label, held in st["dropped"]:
        print(f"        DROPPED, not in repo template (deprecated): {label}"
              f"{' (held a value)' if held else ''}")
    for name in st["mirror_dropped"]:
        print(f"        DROPPED legacy <Environment> mirror, not in repo template: {name or '(no name)'}")
    if st["dupes"]:
        print(f"        ! duplicate keys (first kept): {st['dupes']}")
    if st["mode_kept"]:
        for label, op_mode, tpl_mode in st["mode_kept"]:
            print(f"        Path Mode PRESERVED (operator-owned): {label} stays {op_mode!r} "
                  f"— the template now ships Mode={tpl_mode!r} for it, reaching only a "
                  f"freshly-seeded my-<name>.xml, not this instance")
    return True


def load_template(name, sha):
    """Fetch (at commit `sha`), parse and validate repo template `name`. Returns (root, None)
    or (None, why)."""
    try:
        tpl_root = ET.fromstring(fetch_template(name, sha))
    except Exception as e:
        return None, f"could not fetch/parse repo template: {e}"
    err = validate(tpl_root)
    if err:
        return None, f"repo template invalid ({err}); skipping"
    return tpl_root, None


def process_template(name, tpl_root, instances_by_tpl, backup_dir):
    """Process one loaded repo template: CREATE the stub if absent, UPDATE every live instance.
    Returns the number of failures (unraid-templates#61/#62) — 0 means clean."""
    print(f"[{name}]")
    failures = 0
    base_path = os.path.join(TEMPLATES_USER, f"my-{name}.xml")

    # unraid-templates#60: os.path.exists() answers False on ANY OSError (EACCES, EIO, a
    # transient mount hiccup), not only on genuine absence. Deciding CREATE-vs-UPDATE on that
    # answer meant a stat failure on a POPULATED instance looked identical to "not created
    # yet" - the CREATE branch would then replace the operator's applied values with the bare
    # repo template, with no backup and no merge. os.stat + explicit FileNotFoundError is the
    # only case that means "absent"; any other OSError refuses to touch the file at all.
    try:
        os.stat(base_path)
        base_exists = True
    except FileNotFoundError:
        base_exists = False
    except OSError as e:
        print(f"    ! my-{name}.xml         could not be checked ({e}); left untouched")
        failures += 1
        base_exists = None

    if base_exists is False:                     # CREATE the base stub if absent
        if DRY_RUN:
            print(f"    + my-{name}.xml         would CREATE  (does not exist yet)")
        else:
            try:
                atomic_write(base_path, copy.deepcopy(tpl_root))
                print(f"    + my-{name}.xml         CREATED  (ready for Add Container)")
                base_exists = True
            except OSError as e:
                _discard(base_path + ".tmp")
                print(f"    ! my-{name}.xml         FAILED to create ({e})")
                failures += 1

    targets = set(instances_by_tpl.get(name, set()))   # UPDATE every live instance
    if base_exists:
        targets.add(base_path)
    for inst_path in sorted(targets):
        if not update_instance(inst_path, tpl_root, backup_dir):
            failures += 1
    if not targets:
        print("    (no live instances yet)")
    return failures


# ----------------------------------------------------------------------------- main
def main():
    # unraid-templates#102: the run over every template is retired, not repaired. It claimed
    # my-<name>.xml by NAME for template <name> even when the instance map gave that file to
    # another template, so one file was reconciled against two templates and each dropped the
    # other's variables. Only a scoped run checks that, so only a scoped run exists.
    if TEMPLATE is None:
        sys.exit('error: TEMPLATE is not set. Set TEMPLATE = "<name>" in this copy (one User '
                 'Script per repo template). Nothing changed.')
    if not os.path.isdir(TEMPLATES_USER):
        sys.exit(f"error: templates dir not found: {TEMPLATES_USER}")

    # Report the REASON. This is step 0 of the runner conversion, and an unauthenticated
    # api.github.com is rate-limited to 60 requests/hour per IP — "network/API" alone sends
    # the operator looking at their firewall instead of at a 403 that clears by itself.
    # ONE commit for the whole run (#103): the listing and the template body are both read at it.
    sha = ""
    try:
        sha = resolve_sha()
        all_repo = list_repo_templates(sha)
        why = ""
    except Exception as e:
        all_repo, why = [], f" ({e})"
    if not all_repo:
        sys.exit(f"error: could not list repo templates from {REPO}@{BRANCH}"
                 f"{why or ' (empty listing)'}. Nothing changed.")
    # ⛔ REFUSE BEFORE THE FIRST WRITE (the backup redaction below). A misspelt TEMPLATE must
    # never quietly become a clean-looking no-op.
    if TEMPLATE not in all_repo:
        sys.exit(f"error: TEMPLATE={TEMPLATE!r} is not a template in {REPO}@{BRANCH} ({sha}) "
                 f"(it has: {', '.join(all_repo)}). Nothing changed.")

    banner = "DRY-RUN — writes NOTHING (validate me, then install the DRY_RUN=False version)" \
        if DRY_RUN else "LIVE — will create/update/drop with backups"
    print(f"sync-templates  repo={REPO}@{BRANCH}  commit={sha}  dir={TEMPLATES_USER}")
    print(f"mode: {banner}")
    print(f"scope: TEMPLATE={TEMPLATE!r} — only this template and its live instances; "
          f"the other {len(all_repo) - 1} repo template(s) are left alone")
    print(f"templates: {TEMPLATE}\n")

    # ⚠️ Map against the FULL list, then pick this run's templates out of the result. Mapping
    # against [TEMPLATE] alone leaves it the only candidate prefix, so a `tape` run would claim
    # `my-tape-db-dev.xml` — the exact trap the longest-dash-prefix rule exists to avoid.
    instances_by_tpl, unmapped, broken = discover_instances(TEMPLATES_USER, all_repo)
    # ⛔ The stub my-<TEMPLATE>.xml is always processed as TEMPLATE's (process_template), so a
    # file there that is really another template's instance, or a foreign container, would be
    # rewritten with the wrong template's variables. SAME FILE, not same name: /boot is FAT32,
    # where my-Tape.xml IS my-tape.xml. Refuse rather than guess.
    # Without a listing there is nothing to tell this template's instances from anyone else's.
    # The listing the scope is built from, not a second one: discover_instances reports its
    # failure as a `broken` entry named for the dir itself (no instance name looks like that).
    unlisted = [why for f, why in broken if f == os.path.basename(TEMPLATES_USER.rstrip("/\\"))]
    if unlisted:
        sys.exit(f"error: {unlisted[0]}, so a TEMPLATE={TEMPLATE!r} run cannot tell its own "
                 f"instances apart. Nothing changed.")
    base = os.path.join(TEMPLATES_USER, f"my-{TEMPLATE}.xml")
    claimed = [(p, f"maps to {t!r}")
               for t, paths in instances_by_tpl.items() if t != TEMPLATE for p in paths]
    claimed += [(os.path.join(TEMPLATES_USER, f), "is not from these templates") for f in unmapped]
    for p, why in sorted(claimed):
        if _same_file(p, base):
            sys.exit(f"error: {os.path.basename(p)} (the file at my-{TEMPLATE}.xml) {why}, so a "
                     f"TEMPLATE={TEMPLATE!r} run would rewrite it with the wrong template. Fix its "
                     f"<TemplateURL> or rename that container. Nothing changed.")
    # A backup is this run's only if its owner — judged against EVERY instance name, not just
    # this template's — is one of this template's instances. A backup whose instance is gone
    # (other than the stub's) belongs to no run; the operator deletes those by hand (README).
    own = {f"my-{TEMPLATE}.xml"} | {os.path.basename(p) for p in instances_by_tpl.get(TEMPLATE, ())}
    known = ({os.path.basename(p) for paths in instances_by_tpl.values() for p in paths}
             | set(unmapped) | {f for f, _ in broken} | {f"my-{t}.xml" for t in all_repo})

    def scope(fname):
        return _backup_owner(fname, known) in own

    backup_dir = os.path.join(TEMPLATES_USER, BACKUP_SUBDIR)
    failures = 0                                # unraid-templates#62: a run must be able to say so

    # BEFORE anything else touches the backup dir. Every `.bak` written before this release is a
    # cleartext copy of whatever secrets that instance held, and this is the run that clears
    # them; doing it first means a crash later still leaves the flash drive better than it was.
    # The template is loaded first, once: what counts as secret is its call as well as each
    # file's (unraid-templates#104). If it cannot load, the files' own masks still apply.
    tpl_root, tpl_error = load_template(TEMPLATE, sha)
    secret_targets = _masked_names(tpl_root) if tpl_root is not None else set()
    files, values, unreadable = redact_existing_backups(backup_dir, scope, secret_targets)
    if files:
        print(f"{'would redact' if DRY_RUN else 'REDACTED'} {values} masked value(s) across "
              f"{files} pre-existing backup(s) in {BACKUP_SUBDIR}/ (unraid-templates#27: "
              f"`Mask` is a UI setting, so these were stored in PLAINTEXT)")
    if unreadable:
        failures += len(unreadable)
        print(f"! {len(unreadable)} backup(s) could not be read, so could NOT be redacted - "
              f"they may still hold secrets in plaintext. These are left in place for you to "
              f"review and delete by hand:")
        for fname, why in unreadable:
            print(f"    {fname} ({why})")
    if files or unreadable:
        print()

    if broken:
        failures += len(broken)
        print(f"! {len(broken)} of your OWN instance(s) could not be read, so could not be "
              f"matched to a template and were left untouched — fix or remove by hand:")
        for fname, why in broken:
            print(f"    {fname} ({why})")
        print()

    if tpl_error:
        print(f"[{TEMPLATE}]\n    ! {tpl_error}")
        failures += 1
    else:
        failures += process_template(TEMPLATE, tpl_root, instances_by_tpl, backup_dir)
    print()

    # ⭐ PRUNE LAST, once this run's own backups exist. Pruning first left KEEP_BACKUPS + 1 on
    # disk afterwards, so the run ended one over the number it reported keeping. It also spent
    # flash writes redacting files it was about to delete.
    dropped, prune_failed = prune_backups(backup_dir, protected={f for f, _ in unreadable}, scope=scope)
    if prune_failed:
        failures += len(prune_failed)
        print(f"! {len(prune_failed)} backup(s) could not be pruned:")
        for fname, why in prune_failed:
            print(f"    {fname} ({why})")
    if dropped:
        # ⚠️ THE DRY-RUN FIGURE IS A LOWER BOUND, and says so. The prune runs after the templates,
        # so a live run has also written this run's own backups by now and a rehearsal has not —
        # the rehearsal is short by up to one per updated instance. Stated rather than silently
        # off by one: the whole point of DRY_RUN is that it predicts the live run.
        if DRY_RUN:
            print(f"would prune at least {len(dropped)} backup(s) beyond the newest "
                  f"{KEEP_BACKUPS} per instance (a live run also prunes the backups it takes "
                  f"itself, which this rehearsal has not written)\n")
        else:
            print(f"pruned {len(dropped)} backup(s) beyond the newest {KEEP_BACKUPS} "
                  f"per instance\n")

    if unmapped:
        print("left untouched (foreign / not from these templates): " + ", ".join(sorted(unmapped)))

    # unraid-templates#62: every SKIP/failure branch above used to reach this same "done." with
    # an implicit exit 0 — a run that skipped every template looked identical to a clean one to
    # anything that reads the exit status (a wrapper, a schedule, a person skimming the tail).
    if failures:
        print(f"done. {failures} failure(s) — see above"
              + ("  (dry-run — nothing changed)" if DRY_RUN else "  (LIVE — changes written)"))
        sys.exit(1)
    print("done." + ("  (dry-run — nothing changed)" if DRY_RUN else "  (LIVE — changes written)"))


if __name__ == "__main__":
    main()
