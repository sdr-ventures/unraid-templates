#!/usr/bin/env python3
"""
sync-templates — reconcile Unraid dockerMan container templates against the
published `unraid-templates` repo WITHOUT losing your applied values.

NO PARAMETERS, NO WRAPPER. Running it does create / update / delete-as-necessary
across the managed container templates that TEMPLATE (below) selects:

  TEMPLATE = None     every repo template and all their instances (the full pass).
  TEMPLATE = "tape"   ONLY the `tape` repo template and its live instances. Nothing
                      that maps to another template is created, updated, deleted,
                      or has its backups redacted or pruned — `tape-db` included,
                      since instances are mapped against the FULL template list
                      (see MAPPING below). Refused before anything is written: a
                      name the repo does not have; a my-tape.xml that is really
                      another template's instance or a foreign container (case
                      variants of the name, e.g. my-Tape.xml, included); and a
                      templates dir that cannot be listed. A backup whose
                      instance is gone, unmapped or unreadable is left to the
                      full pass — except my-tape.xml's, which are tape's (unless
                      that file maps to another template, when tape refuses).

  ONE USER SCRIPT PER TEMPLATE: each installed copy is identical to this file
  apart from its TEMPLATE line, so syncing one template can never ship another
  template's merged-but-not-intended changes. The repo copy is the full pass.

  CREATE  — seed my-<name>.xml for any repo template that has no my- file yet,
            so it is ready to pick in Add Container.
  UPDATE  — for EVERY live instance of a template (tape: my-tape.xml AND
            my-tape-dev.xml, ...; my-tape-db-dev.xml is tape-db's): keep each
            instance's applied values, refresh each variable's metadata from the
            template, and ADD new template vars.
  DELETE  — drop a variable the template removed ONLY when it is genuinely unused
            (blank or still at its default). A removed variable that still holds a
            real, non-default value is KEPT and loudly flagged — that almost always
            means the TEMPLATE is missing it (drift), not that you wanted it gone.
  Container-level settings you set per instance — image tag, network/IP, WebUI,
  Extra Params, ports, the container Name — are ALWAYS preserved. Only <Config>
  elements are reconciled.

------------------------------------------------------------------------------
DRY-RUN vs LIVE  —  the DRY_RUN constant below is the switch.
  * DRY_RUN = True   prints exactly what it WOULD create/update/delete, writes
                     nothing. This is the version you validate.
  * DRY_RUN = False  performs the changes (each overwritten file is backed up
                     first, timestamped, under templates-user/.template-sync-backups/;
                     writes are atomic; a result is validated before it replaces
                     the original).

BACKUPS AND SECRETS  (unraid-templates#27)
  `Mask="true"` is a UI setting only -- it makes the Unraid web form render a
  password box. The XML on the flash drive holds the value in PLAINTEXT either
  way. So a backup is written with every masked value REDACTED, and the first
  run also redacts the backups previous versions already wrote. Backups are
  pruned to the newest KEEP_BACKUPS per instance.

  What that means for a restore: the structure and every non-secret value come
  back in full; a masked value must be re-entered. It is not a real loss --
  merge() copies applied values across verbatim, so a merge cannot damage a
  secret, and the backup is there for a bad merge.
  Workflow: run the dry-run version -> review -> when it is correct, install the
  version with DRY_RUN = False. Never a parameter; a copy differs from this file
  only in its TEMPLATE line (and DRY_RUN while you rehearse).
------------------------------------------------------------------------------

INSTANCE -> TEMPLATE MAPPING
  Primary: the instance's <TemplateURL> basename (my-tape-dev.xml -> tape.xml).
  Fallback: longest dash-prefix of the filename (my-tape-db-dev.xml -> tape-db,
  never tape; my-tapeworm.xml -> nothing). Foreign containers are never touched.

REQUIRES  python3 >= 3.9 (stdlib only). Run on the Unraid host via User Scripts.
"""

# =============================================================================
DRY_RUN = False    # LIVE — creates/updates/deletes with timestamped backups. (dev phase used True)
TEMPLATE = None    # None = every template. "<name>" = only templates/<name>.xml and its instances.
# =============================================================================

import copy
import json
import os
import re
import sys
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
# recovers: `merge()` copies every applied value across VERBATIM (`new_c.text = op_by_key[k].text`)
# and keeps a template-removed variable verbatim too, so a merge cannot corrupt a secret in the
# first place. What a restore actually needs is the STRUCTURE and the non-secret values, and those
# are preserved in full. A masked value is re-entered from wherever it came from.
REDACTED = "***REDACTED***"

# How many backups to keep per instance file. The old code kept every backup forever, which is
# half of what #27 is about: even redacted, an unbounded pile of timestamped XML on a flash drive
# is just accumulation. The newest are the ones with any recovery value.
KEEP_BACKUPS = 10


# ----------------------------------------------------------------------------- http
def _get(url):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
        return r.read()


def list_repo_templates():
    """Enumerate <name> for every .xml under templates/ (via the GitHub API)."""
    url = f"https://api.github.com/repos/{REPO}/contents/{TEMPLATE_SUBDIR}?ref={BRANCH}"
    data = json.loads(_get(url).decode("utf-8"))
    return sorted(
        e["name"][:-4] for e in data
        if e.get("type") == "file" and e.get("name", "").endswith(".xml")
    )


def fetch_template(name):
    url = f"https://raw.githubusercontent.com/{REPO}/{BRANCH}/{TEMPLATE_SUBDIR}/{name}.xml"
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
    Delete-as-necessary: a removed var is dropped only if unused (blank/default);
    a removed var with a real value is kept + flagged (template drift)."""
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

    stats = {"added": [], "retained": 0, "deleted": [], "kept_flag": [], "dupes": dup_keys}
    seen = set()

    for tc in template_root.findall("Config"):  # template order; refresh metadata
        k = config_key(tc)
        seen.add(k)
        new_c = copy.deepcopy(tc)               # template metadata + default value
        if k in op_by_key:
            new_c.text = op_by_key[k].text      # keep the applied value verbatim
            stats["retained"] += 1
        else:
            stats["added"].append(tc.get("Name") or k[1])
        merged.append(new_c)

    for k, c in op_by_key.items():              # vars the template no longer defines
        if k in seen:
            continue
        val = (c.text or "").strip()
        default = (c.get("Default") or "").strip()
        label = c.get("Name") or k[1]
        if val == "" or val == default:
            stats["deleted"].append(label)      # unused -> delete as necessary
        else:
            merged.append(copy.deepcopy(c))     # real value -> keep + flag drift
            stats["kept_flag"].append(label)

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


def _masked_configs(root):
    """Every masked <Config> anywhere under `root`.

    `iter` and NOT `findall`: `findall("Config")` is direct children only, so a masked <Config>
    nested one level down was skipped entirely. Over-reaching costs nothing here — this only ever
    runs against a BACKUP copy, where redacting one element too many is harmless and missing one
    is the bug.
    """
    return [c for c in root.iter("Config") if _is_masked(c)]


def _masked_names(root):
    """The environment-variable KEYS the masked <Config> elements correspond to.

    ⚠️ `Target` IS THE VARIABLE NAME; `Name` is the human label the UI shows ("Key", "API token").
    The <Environment> mirror keys on the VARIABLE, so matching the human label as well made an
    unrelated variable that merely shared a label get redacted — destroying a non-secret value a
    restore would want, and counting it, so the run reported more redactions than it performed.
    `Name` is used only as a fallback for a <Config> that has no Target.
    """
    names = set()
    for c in _masked_configs(root):
        key = (c.get("Target") or "").strip() or (c.get("Name") or "").strip()
        if key:
            names.add(key)
    return names


def _mirrored_secrets(root):
    """The <Environment><Variable><Value> elements that mirror a masked <Config>.

    ⭐ dockerMan WRITES EACH VARIABLE TWICE. Alongside the <Config> elements this script
    reconciles, `my-*.xml` carries a legacy

        <Environment><Variable><Value>s3cret</Value><Name>TOKEN</Name></Variable></Environment>

    block holding the SAME value. `merge()` deepcopies the operator tree and only reconciles
    <Config>, so that block is preserved verbatim — which meant redacting the <Config> half left
    the secret sitting in the <Environment> half, in cleartext, in every backup, while the run
    reported the value redacted. Both agents that reviewed this reproduced it independently.

    Matched by NAME against the masked <Config> set rather than by any flag of its own: the
    <Variable> element carries no `Mask` attribute, so the <Config> is the only place that says
    whether the value is a secret.
    """
    secret_names = _masked_names(root)
    found = []
    for var in root.iter("Variable"):
        name = (var.findtext("Name") or "").strip()
        if name and name in secret_names:
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


def redact_secrets(root):
    """Blank every masked value on `root`, IN PLACE. Returns how many were redacted.

    Covers BOTH places dockerMan stores a variable: the <Config> element and its <Environment>
    mirror. Only a field that actually HOLDS something is touched — marking an empty one would
    claim it held a secret when it did not, and an operator reading a backup to see what was
    configured would be misled.
    """
    n = 0
    for el in _masked_configs(root) + _mirrored_secrets(root):
        if not _element_holds_a_secret(el):
            continue
        for child in list(el):      # the child elements are content too
            el.remove(child)
        el.text = REDACTED
        n += 1
    return n


def count_secrets(root):
    """How many masked values `root` still holds. The exact converse of `redact_secrets`, so the
    reported count and the work actually done cannot drift apart."""
    return sum(1 for el in _masked_configs(root) + _mirrored_secrets(root)
               if _element_holds_a_secret(el))


def backup(path, backup_dir):
    """Copy `path` aside before it is overwritten, with every masked value REDACTED.

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
    n = redact_secrets(tree.getroot())
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


def redact_existing_backups(backup_dir, scope=None):
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
        n = count_secrets(root)
        if not n:
            continue
        if not DRY_RUN:
            try:
                redact_secrets(root)
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
    # Compare the SEMANTIC result, not the variable set. Gating on added/deleted/kept meant a
    # template edit that changed only a field's Description, Default, Display or Required was
    # computed correctly by merge() and then thrown away — and a Description is where every
    # operator-facing instruction in this repo lives, so the one edit the templates are
    # written to deliver was the one edit that never arrived. Both sides go through the same
    # indent+serialise path so this compares content, not the file's original formatting.
    # kept_flag is the ONE stat this comparison cannot see. A variable the template no longer
    # defines but which still holds a real value is copied VERBATIM into merged (see merge()),
    # so merged can equal the operator's tree while drift is sitting right there. Returning on
    # content alone therefore silenced the loudest warning this script has - and silenced it on
    # every run after the first, which is exactly when an operator re-runs to check their work.
    # Drift is reported whether or not there is anything to write. dupes rides along in the
    # condition for safety, though for an operator-side duplicate it is redundant: merge()
    # keeps only the first of a duplicate-keyed Config, which changes the merged tree, so
    # `unchanged` is already False.
    unchanged = canonical(merged) == canonical(op_root)
    if unchanged and not (st["kept_flag"] or st["dupes"]):
        print(f"    = {fname:<22} up to date  ({st['retained']} values, nothing to change)")
        return True
    # "metadata refreshed ... no variables added or removed" must be true of the run that
    # prints it. dupes belongs in here because merge() DROPS all but the first duplicate,
    # which IS a variable removed. `unchanged` needs no term of its own: when unchanged is
    # true the early return above only falls through if kept_flag or dupes is set, and either
    # of those already zeroes this.
    meta_only = not (st["added"] or st["deleted"] or st["kept_flag"] or st["dupes"])
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
            b, masked = backup(inst_path, backup_dir)
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
    if st["deleted"]:
        print(f"        deleted (unused): {', '.join(st['deleted'])}")
    if st["kept_flag"]:
        print(f"        !! KEPT (removed from template but still holds a value — FIX THE "
              f"TEMPLATE, it is probably missing these): {', '.join(st['kept_flag'])}")
    if st["dupes"]:
        print(f"        ! duplicate keys (first kept): {st['dupes']}")
    return True


def process_template(name, instances_by_tpl, backup_dir):
    """Process one repo template: CREATE the stub if absent, UPDATE every live instance.
    Returns the number of failures (unraid-templates#61/#62) — 0 means clean."""
    print(f"[{name}]")
    try:
        tpl_root = ET.fromstring(fetch_template(name))
    except Exception as e:
        print(f"    ! could not fetch/parse repo template: {e}")
        return 1
    err = validate(tpl_root)
    if err:
        print(f"    ! repo template invalid ({err}); skipping")
        return 1

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
    if not os.path.isdir(TEMPLATES_USER):
        sys.exit(f"error: templates dir not found: {TEMPLATES_USER}")

    # Report the REASON. This is step 0 of the runner conversion, and an unauthenticated
    # api.github.com is rate-limited to 60 requests/hour per IP — "network/API" alone sends
    # the operator looking at their firewall instead of at a 403 that clears by itself.
    try:
        all_repo = list_repo_templates()
        why = ""
    except Exception as e:
        all_repo, why = [], f" ({e})"
    if not all_repo:
        sys.exit(f"error: could not list repo templates from {REPO}@{BRANCH}"
                 f"{why or ' (empty listing)'}. Nothing changed.")
    # ⛔ REFUSE BEFORE THE FIRST WRITE (the backup redaction below). A misspelt TEMPLATE must
    # never quietly become a clean-looking no-op, nor fall back to the full pass it exists to avoid.
    if TEMPLATE is not None and TEMPLATE not in all_repo:
        sys.exit(f"error: TEMPLATE={TEMPLATE!r} is not a template in {REPO}@{BRANCH} "
                 f"(it has: {', '.join(all_repo)}). Nothing changed.")
    names = all_repo if TEMPLATE is None else [TEMPLATE]

    banner = "DRY-RUN — writes NOTHING (validate me, then install the DRY_RUN=False version)" \
        if DRY_RUN else "LIVE — will create/update/delete with backups"
    print(f"sync-templates  repo={REPO}@{BRANCH}  dir={TEMPLATES_USER}")
    print(f"mode: {banner}")
    if TEMPLATE is not None:
        print(f"scope: TEMPLATE={TEMPLATE!r} — only this template and its live instances; "
              f"the other {len(all_repo) - 1} repo template(s) are left alone")
    print(f"templates: {', '.join(names)}\n")

    # ⚠️ Map against the FULL list, then pick this run's templates out of the result. Mapping
    # against [TEMPLATE] alone leaves it the only candidate prefix, so a `tape` run would claim
    # `my-tape-db-dev.xml` — the exact trap the longest-dash-prefix rule exists to avoid.
    instances_by_tpl, unmapped, broken = discover_instances(TEMPLATES_USER, all_repo)
    scope = None                                # the full pass may touch every backup
    if TEMPLATE is not None:
        # ⛔ The stub my-<TEMPLATE>.xml is always processed as TEMPLATE's (process_template), so a
        # file there that is really another template's instance, or a foreign container, would be
        # rewritten with the wrong template's variables. SAME FILE, not same name: /boot is FAT32,
        # where my-Tape.xml IS my-tape.xml. Refuse rather than guess; the full pass is unchanged.
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
        # (other than the stub's) belongs to no scoped run; only the full pass touches those.
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
    files, values, unreadable = redact_existing_backups(backup_dir, scope)
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

    for name in names:
        failures += process_template(name, instances_by_tpl, backup_dir)
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
