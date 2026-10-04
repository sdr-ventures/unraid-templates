"""The tl;dw template set: the properties that are about THIS set, asserted rather than read.

This set is two templates that must agree with each other — a start order Unraid cannot
enforce, a Redis address the operator has to wire by hand, a session-cookie flag whose value
is a decision — and each of those is one attribute in an XML file that an edit which looks
like tidying silently undoes.

Everything that is true of ANY template here — credential fields shipping blank and masked,
an image reference that cannot move, `Default=` agreeing with the element text, the icon
and README rules, the privilege posture — used to live in this file and now lives in
`test_template_invariants.py`, which runs over every `templates/*.xml` (issue #57). This
file keeps only what is genuinely tl;dw's. None of it is caught by the leak guard (these are
not internal values) or by the XML job (the file still parses).

⚠️ ASSERT ON `Target`, NEVER ON `Name` ALONE — `Target` is the string that becomes the
environment variable; `Name` is only the label Unraid draws beside the field.

WHAT THIS FILE DOES NOT CLAIM:
  * The Overview disclosures are asserted as SUBSTRINGS. An Overview could contain every
    required phrase inside a sentence that negates it. Substring matching makes silent
    deletion impossible; it does not make the prose true. Read the Overview.
  * Nothing here runs a container. Whether the image behaves as the descriptions say was
    established by reading upstream at the pinned commit, and is recorded in the templates'
    own comments.
"""

import pathlib
import re
import xml.etree.ElementTree as ET

import pytest

REPO = pathlib.Path(__file__).resolve().parents[1]
TEMPLATES = REPO / "templates"

# The set, in the order it must be started.
TLDW = ("tldw-redis", "tldw-server", "tldw-webui")

# The exact ordering sentence each Overview must carry. Asserting that both container names
# merely APPEAR would pass an Overview that stated the order backwards, which is worse than
# saying nothing: Unraid has no depends_on, so this prose is the only thing an operator has.
# tldw-webui's Overview carries the 3-step form, "tldw-redis first, then tldw-server, then
# tldw-webui", which contains this exact phrase as a substring, so one constant covers both.
ORDER_PHRASE = "tldw-redis first, then tldw-server"

# Credential variables this set requires the operator to fill in. The repo-wide rule makes
# every credential-shaped field blank and masked; these must additionally be marked
# required, and are re-asserted below BY NAME so a rename out of the shape cannot silently
# drop one from the repo-wide rule.
REQUIRED_SECRETS = {
    "tldw-server": {"SINGLE_USER_API_KEY", "MCP_JWT_SECRET", "MCP_API_KEY_SALT"},
    "tldw-redis": set(),
    "tldw-webui": set(),
}

# The image each template must actually pull. The repo-wide pin rule proves the reference
# cannot move; it says nothing about WHICH image it names, so the repository half is pinned
# here. tldw-server is a third-party image and its tag must be a full release number;
# tldw-webui publishes no such tag at all (confirmed live against the GHCR tags API), so its
# shape is asserted separately below rather than folded into this same release-number regex.
EXPECTED_IMAGE = {
    "tldw-server": "ghcr.io/rmusser01/tldw_server",
    "tldw-redis": "redis",
    "tldw-webui": "ghcr.io/rmusser01/tldw_server-webui",
}

# The minimum each template must actually declare, so "no rule fired" cannot mean "there was
# nothing left to look at". Gutting tldw-redis to a single placeholder Config previously
# stayed green, because every rule that covers it is conditional and its required-secret set
# is legitimately empty. tldw-webui has no Path Config by design - it is stateless.
MUST_DECLARE = {
    "tldw-server": {
        ("Port", "8000"), ("Path", "/app/Databases"),
        ("Path", "/app/tldw_Server_API/Config_Files/config.txt"), ("Path", "/usr/local/bin/deno"),
    },
    "tldw-redis": {("Path", "/data")},
    "tldw-webui": {("Port", "3000")},
}

# Upstream's two compose service names, as they appear in a URL: `redis://redis:6379/0`,
# `http://app:8000`, and the port-less forms of both. The leading lookbehind is what keeps
# an absolute SQLite URL out of it — `sqlite:////app/Databases/users.db` contains "//app"
# and is the value this template ships, so matching that substring would red a correct tree.
COMPOSE_HOST = re.compile(r"(?<!/)//(?:app|redis)(?::\d+)?(?:/|$)")

# The users database, as an ABSOLUTE SQLite URL. Upstream's relative default
# (`sqlite:///./Databases/users.db`) names the same file under WORKDIR /app, but the
# entrypoint's BYOK key-loss guard resolves that URL to /Databases/users.db, which never
# exists, so the guard never fired and an image update could silently regenerate the key
# over stored provider secrets (issue #58). The absolute form resolves correctly on both
# paths, so the guard is live — which is a property worth pinning.
USERS_DB_URL = "sqlite:////app/Databases/users.db"

_CACHE = {}


def load(name):
    path = TEMPLATES / (name + ".xml")
    assert path.is_file(), f"{path} is missing"
    return ET.parse(path).getroot()


def template(name):
    if name not in _CACHE:
        _CACHE[name] = load(name)
    return _CACHE[name]


def configs(root):
    # iter(), not findall(): findall sees direct children only.
    return list(root.iter("Config"))


def var_name(cfg):
    """The environment variable a Config actually sets — Target, else Name, stripped, the
    same fallback `sync-templates.py` uses."""
    return ((cfg.get("Target") or cfg.get("Name") or "")).strip()


def is_variable(cfg):
    return (cfg.get("Type") or "Variable").strip().lower() == "variable"


def by_target(root, target):
    """Every Config wired to this environment variable — by Target, not by label."""
    return [c for c in configs(root) if is_variable(c) and var_name(c) == target]


def one_by_target(root, target):
    found = by_target(root, target)
    assert len(found) == 1, f"expected exactly one Config with Target={target!r}, got {len(found)}"
    return found[0]


def value_of(cfg):
    """The two places a value can hide: the attribute and the element text."""
    return ((cfg.get("Default") or "").strip(), (cfg.text or "").strip())


def environment_pairs(root):
    """The <Environment> block Unraid writes beside <Config>, as (name, value) pairs."""
    pairs = []
    for var in root.iter("Variable"):
        name = (var.findtext("Name") or "").strip()
        value = (var.findtext("Value") or "").strip()
        if name:
            pairs.append((name, value))
    return pairs


@pytest.mark.parametrize("name", TLDW)
def test_the_templates_exist_and_name_themselves(name):
    # The vacuity guard for everything below: if a template were missing, or its root were
    # not a Container, or it had no Config elements, the per-field assertions would have
    # nothing to look at and several would pass by finding nothing wrong.
    root = template(name)
    assert root.tag == "Container"
    assert root.findtext("Name") == name
    assert configs(root), f"{name}: no Config elements at all"
    assert name in MUST_DECLARE and name in EXPECTED_IMAGE, (
        f"{name}: added to TLDW without an entry in MUST_DECLARE / EXPECTED_IMAGE, so "
        f"several rules below would have nothing to check for it"
    )
    declared = {((c.get("Type") or "").strip(), (c.get("Target") or "").strip()) for c in configs(root)}
    missing = MUST_DECLARE[name] - declared
    assert not missing, (
        f"{name}: no longer declares {sorted(missing)} — several rules below are conditional, "
        f"so a template stripped of its port and mount would pass them by having nothing to check"
    )


@pytest.mark.parametrize("name", TLDW)
def test_the_secrets_the_operator_must_supply_are_marked_required_blank_and_masked(name):
    # Required is advisory in Unraid — it does not stop a blank container from starting —
    # but it is what renders the field as one the operator is expected to fill, and it is
    # the only signal the template can give on the page itself. Blank + masked is also the
    # repo-wide rule; it is re-asserted here BY NAME so that a rename of one of these three
    # to a non-credential-shaped spelling cannot walk it out of the shape rule unnoticed.
    for target in sorted(REQUIRED_SECRETS[name]):
        cfg = one_by_target(template(name), target)
        assert cfg.get("Required") == "true", f"{name}: {target} is not marked required"
        assert value_of(cfg) == ("", ""), f"{name}: {target} ships a value"
        assert cfg.get("Mask") == "true", f"{name}: {target} is not masked"


@pytest.mark.parametrize("name", TLDW)
def test_the_image_is_the_expected_one(name):
    repository = (template(name).findtext("Repository") or "").strip()
    image = repository.split("@", 1)[0].rsplit(":", 1)[0]
    assert image == EXPECTED_IMAGE[name], f"{name}: pulls {image!r}, expected {EXPECTED_IMAGE[name]!r}"
    if name == "tldw-server":
        # The repo-wide rule accepts any pinned SHAPE; this image publishes plain release
        # numbers, and the header comment documents the one that was verified.
        tag = repository.rsplit(":", 1)[-1]
        assert re.fullmatch(r"\d+\.\d+\.\d+", tag), f"tldw-server: tag {tag!r} is not a release number"
    if name == "tldw-webui":
        # No release-numbered tag exists for this image (confirmed live against the GHCR tags
        # API) - the repo-wide pin rule accepts a sha-* tag as a pinned shape, and this asserts
        # it is actually that shape rather than a silently-drifted `main`/`latest`.
        tag = repository.rsplit(":", 1)[-1]
        assert re.fullmatch(r"sha-[0-9a-f]{7,}", tag), f"tldw-webui: tag {tag!r} is not a pinned sha-* tag"


def test_the_compose_host_classifier_bites_on_upstreams_service_names():
    # The negative direction for COMPOSE_HOST, which the shipped templates only ever exercise
    # in the clean direction: gutted to `(?!)` the wiring test below would stay green.
    for bad in ("redis://redis:6379/0", "http://app:8000", "redis://redis", "http://app/x"):
        assert COMPOSE_HOST.search(bad), f"COMPOSE_HOST misses {bad!r}"
    for good in (USERS_DB_URL, "redis://198.51.100.5:6380/0", "http://host.example:8000"):
        assert not COMPOSE_HOST.search(good), f"COMPOSE_HOST fires on {good!r}"


@pytest.mark.parametrize("name", TLDW)
def test_the_wiring_variables_ship_blank_so_the_operator_must_fill_them(name):
    # A default that cannot work is worse than no default: it looks configured. Upstream's
    # compose service names resolve only on a user-defined Docker network, which a template
    # cannot create, so anything of that shape must not be shipped as a value.
    root = template(name)
    pairs = [(var_name(c), v) for c in configs(root) if is_variable(c) for v in value_of(c)]
    pairs += environment_pairs(root)
    for target, value in pairs:
        assert not COMPOSE_HOST.search(value), (
            f"{name}: {target} ships {value!r}, a compose service name that "
            f"does not resolve on the bridge network"
        )
    if name == "tldw-server":
        default, text = value_of(one_by_target(template(name), "REDIS_URL"))
        assert default == "" and text == "", "REDIS_URL must ship blank"


def test_the_users_database_url_is_absolute_so_upstreams_byok_guard_can_find_it():
    # Issue #58: with the relative form the entrypoint's refuse-to-regenerate guard looked
    # for /Databases/users.db, found nothing, and regenerated BYOK_ENCRYPTION_KEY over stored
    # provider secrets on every update. The absolute form is the same file and a live guard.
    root = template("tldw-server")
    cfg = one_by_target(root, "DATABASE_URL")
    assert value_of(cfg) == (USERS_DB_URL, USERS_DB_URL), f"DATABASE_URL is {value_of(cfg)}"
    assert cfg.get("Mask") == "true", "DATABASE_URL can carry a Postgres password"
    for env_name, env_value in environment_pairs(root):
        if env_name == "DATABASE_URL":
            assert env_value == USERS_DB_URL, "<Environment> contradicts the Config value"


def test_the_session_cookie_is_secure_with_no_operator_action():
    root = template("tldw-server")
    assert value_of(one_by_target(root, "SESSION_COOKIE_SECURE")) == ("1", "1")
    for env_name, env_value in environment_pairs(root):
        if env_name == "SESSION_COOKIE_SECURE":
            assert env_value == "1", "<Environment> contradicts the Config value"


def test_csrf_ships_off_because_the_secure_cookie_makes_it_unusable_over_plain_http():
    # Not an oversight and not a weakening: with SESSION_COOKIE_SECURE=1 and no HTTPS in
    # front, the double-submit cookie this protection relies on is discarded by the browser,
    # so every write it guards fails with a 403 that names CSRF rather than the real cause.
    # Off is also the application's own behaviour in single-user mode.
    root = template("tldw-server")
    assert value_of(one_by_target(root, "CSRF_ENABLED")) == ("0", "0")
    for env_name, env_value in environment_pairs(root):
        if env_name == "CSRF_ENABLED":
            assert env_value == "0", "<Environment> contradicts the Config value"


@pytest.mark.parametrize("name", TLDW)
def test_each_overview_states_the_startup_order_in_order(name):
    overview = template(name).findtext("Overview") or ""
    assert ORDER_PHRASE in overview, (
        f"{name}: its Overview does not state the start order as {ORDER_PHRASE!r} — "
        f"Unraid has no depends_on, so this sentence is the only place it exists"
    )


def test_the_api_overview_carries_the_disclosures_the_operator_needs_up_front():
    # Each of these is something that changes whether someone installs this at all, and the
    # Overview is the only text Unraid shows before the container is created.
    overview = template("tldw-server").findtext("Overview") or ""
    assert re.search(r"\b\d+\.\d+\.\d+ beta\b", overview), (
        "the API Overview no longer names the upstream version it warns about — assert "
        "the shape, not a literal, so an image bump is not a test edit"
    )
    for phrase in (
        "rough edges",      # upstream's own beta warning, quoted
        "yt-dlp",
        "ffmpeg",
        "faster_whisper",
        "egress",           # blocks media URLs pointing at private networks
        "CPU-bound",        # and therefore slow under load
        "GPU support is unverified",
    ):
        assert phrase in overview, f"the API Overview no longer mentions {phrase!r}"


def test_webui_offers_no_field_for_either_inert_baked_env_var():
    # unraid-templates#55: the API origin is compiled into the published image's build
    # output, so a Variable Config for it would be decorative - fill it in, Apply, and every
    # API call silently proxies to a name the field never actually controls. Any
    # NEXT_PUBLIC_* Next.js variable is build-time-inlined the same way, for an unrelated
    # reason (Next.js's own design, not this specific baked-URL bug). Neither belongs as a
    # field; this pins that decision so a well-meaning future edit cannot quietly reintroduce
    # either one, believing it will work now.
    root = template("tldw-webui")
    offered = {var_name(c) for c in configs(root) if is_variable(c)}
    for inert in ("TLDW_INTERNAL_API_ORIGIN", "NEXT_PUBLIC_TLDW_DEPLOYMENT_MODE"):
        assert inert not in offered, (
            f"tldw-webui: {inert} is offered as a Variable Config, but it is build-time-baked "
            f"into the published image and inert at runtime - see the template's own header"
        )


def test_webui_documents_the_alias_requirement_with_a_working_issue_link():
    # The single most important fact on this template: skip the manual network-alias setup
    # and the container starts, renders, and silently talks to nothing. Both the field the
    # operator actually sees before Apply (Overview) and the one place prose survives an
    # existing container's template update (the HTTP Port Config's own Description - Overview
    # edits never reach an already-created container, per this repo's own sync-templates.py
    # behaviour) must carry it, not just the header comment a reader of the raw XML sees.
    root = template("tldw-webui")
    overview = root.findtext("Overview") or ""
    # one_by_target/by_target only look at Type="Variable" Configs; the port is Type="Port",
    # so it is found directly here rather than through that variable-only helper.
    port_configs = [c for c in configs(root) if var_name(c) == "3000"]
    assert len(port_configs) == 1, f"expected exactly one Config with Target='3000', got {len(port_configs)}"
    port_description = port_configs[0].get("Description") or ""
    for phrase in ("network-alias=app", "unraid-templates#55"):
        assert phrase in overview, f"tldw-webui Overview no longer mentions {phrase!r}"
    for phrase in ("network-alias=app",):
        assert phrase in port_description, (
            f"tldw-webui's HTTP Port Config Description no longer mentions {phrase!r} - this is "
            f"the one place prose reaches an ALREADY-CREATED container on a template update, "
            f"per this repo's own sync-templates.py behaviour, so it cannot rely on the "
            f"Overview alone"
        )


def test_tldw_redis_publishes_no_host_port(name="tldw-redis"):
    # UT-SYNC-SAFE: a published Redis with no password is reachable from every network a host
    # port exposes it to, for nothing beyond this one container's own sibling. CI invariant: a
    # Port Config reappearing here is the exact regression this guards.
    root = template(name)
    kinds = {(c.get("Type") or "").strip() for c in configs(root)}
    assert "Port" not in kinds, (
        "tldw-redis: a Port Config was added back - this template deliberately publishes no "
        "host port for Redis (UT-SYNC-SAFE); tldw-server reaches it over a Docker network instead"
    )


def test_no_template_text_points_the_operator_at_a_redis_host_port():
    # Done-when: "No template text tells the operator to use a Redis host port the template
    # no longer defines (including the REDIS_URL guidance in the tldw server template)."
    for name in ("tldw-redis", "tldw-server"):
        root = template(name)
        haystack = " ".join(filter(None, [
            root.findtext("Overview"),
            (one_by_target(root, "REDIS_URL").get("Description") if name == "tldw-server" else None),
        ])).lower()
        for phrase in ("host port you gave tldw-redis", "host port you published",
                       "the port published below", "this is the interface"):
            assert phrase not in haystack, f"{name}: still points the operator at a Redis host port ({phrase!r})"


def test_webui_ships_no_path_config_because_it_is_stateless():
    # Nothing in the published image's config or any layer references a persistent path
    # (checked directly against the image, not assumed) - a Path Config appearing later
    # would be a real behaviour change worth a second look, not a silent addition.
    root = template("tldw-webui")
    kinds = {(c.get("Type") or "").strip() for c in configs(root)}
    assert "Path" not in kinds, "tldw-webui: a Path Config was added - is this still stateless?"


@pytest.mark.parametrize("name", TLDW)
def test_no_gpu_passthrough_fields_were_added_speculatively(name):
    # Deliberately absent: whether ingest always runs Whisper is unverified, and a wrong
    # GPU field is a confusing thing to ship where a later added one is not.
    root = template(name)
    fields = [
        text
        for cfg in configs(root)
        for text in (cfg.get("Name"), cfg.get("Target"))
        if text
    ]
    fields.append(root.findtext("ExtraParams") or "")
    fields.append(root.findtext("PostArgs") or "")
    haystack = " ".join(fields).lower()
    for token in ("nvidia", "cuda", "--gpus", "gpu_uuid"):
        assert token not in haystack, f"{name}: a GPU passthrough field ({token}) was added"
