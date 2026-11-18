"""`sync-templates.py` never adds or drops a container mount that the image declares.

The image's own `VOLUME` declarations (for kw-proxy, `/var/spool/squid`) are anonymous volumes
Docker creates at run time. They are not template Configs, so the sync must leave them alone.
The sync reconciles `<Config>` elements from the repo template and nothing else; these tests
fail if it starts reading container or image mount state.
"""

import pathlib
import re

SCRIPT = pathlib.Path(__file__).resolve().parents[1] / "scripts" / "sync-templates.py"


def test_the_sync_script_reads_no_container_or_image_mount_state():
    text = SCRIPT.read_text(encoding="utf-8")
    for needle in ("Mounts", "Volumes", "docker inspect", "subprocess", "/var/lib/docker"):
        assert needle not in text, f"sync-templates.py must not read image or container state: {needle}"


def test_the_sync_script_touches_only_config_elements_for_mounts():
    text = SCRIPT.read_text(encoding="utf-8")
    # Path Configs are the only mount-shaped thing the sync reads, and only from the template XML.
    assert re.search(r'findall\("Config"\)', text)
    assert not re.search(r'findall\("(Mount|Volume|Mounts|Volumes)"\)', text)
