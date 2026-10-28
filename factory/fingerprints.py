"""Sandbox fingerprints: a digest of what a sandbox was created from, recorded when it is
created, so `factory start` can tell it is out of date. Port of sandbox_fingerprint (the broker,
Coordinator and role cases), record_sandbox_fingerprint, stale_sandboxes, image_build_id, dir_hash
and yes_no (scripts/lib/roles.sh), and of broker_plugins (scripts/lib/common.sh). The bash
record_sandbox_fingerprint is gone; the others stay while `factory update` uses them (step 8).
The digest must stay byte for byte the bash one, which compares against what this records, and
stale_sandboxes must name the same sandboxes in the same order; factory/test_fingerprints.py and
factory/test_broker_start.py run both on the same fixtures.
"""

import hashlib
import os
import stat

from factory import roles, sbx
from factory.context import COORDINATOR_SANDBOX

# Keep in sync with RW_LAYOUT in scripts/lib/roles.sh (a test compares them).
RW_LAYOUT = "lanes-v1"


def broker_plugins(paths):
    """The module names of the broker plugins in FACTORY_CONFIG/plugins/<module>/, joined by
    commas ("" if none): every folder (or link to one) not hidden and not __pycache__."""
    folder = os.path.join(paths.config, "plugins")
    try:
        names = sorted(os.listdir(folder))
    except OSError:
        return ""
    return ",".join(n for n in names
                    if not n.startswith(".") and n != "__pycache__" and os.path.isdir(os.path.join(folder, n)))


def image_build_id(paths, image):
    """The id of the last build of factory-IMAGE:TEMPLATE_TAG (00-build-templates.sh records
    it), or that name if none was recorded."""
    try:
        with open(os.path.join(paths.state, "images", "factory-%s-%s.id" % (image, sbx.TEMPLATE_TAG)), "rb") as f:
            return f.read().rstrip(b"\n")
    except OSError:
        return ("factory-%s:%s" % (image, sbx.TEMPLATE_TAG)).encode("utf-8", "surrogateescape")


def _sha256_file(path):
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 16), b""):
            digest.update(block)
    return digest.hexdigest()


def dir_hash(folder):
    """A digest of the names and contents of the regular files under FOLDER (links are not
    followed, broker.conf is left out), "none" if FOLDER is not a folder. As `find -type f`
    does, a FOLDER that is itself a link is not descended."""
    if not os.path.isdir(folder):
        return b"none"
    lines = []
    if not os.path.islink(folder):
        for top, _, files in os.walk(folder):
            for name in files:
                path = os.path.join(top, name)
                try:
                    regular = stat.S_ISREG(os.lstat(path).st_mode)
                except OSError:
                    continue
                if regular and name != "broker.conf":
                    lines.append((os.fsencode(os.path.relpath(path, folder)), path))
    listing = b"".join(b"%s %s\n" % (rel, _sha256_file(path).encode()) for rel, path in sorted(lines))
    return hashlib.sha256(listing).hexdigest().encode()


def _yes_no(value):
    return b"yes" if value else b"no"


def _text(value):
    return value.encode("utf-8", "surrogateescape")


def fingerprint_text(paths, name):
    """What the sandbox NAME is created from, as the key=value lines the digest is taken of. A
    name that is neither the broker nor the Coordinator is a role's sandbox, factory-<role>.
    Grants in broker.conf and LANES are left out on purpose (see scripts/lib/roles.sh)."""
    if name == sbx.BROKER_SANDBOX:
        text = b"image=%s\nkit=%s\nlocal-kit=%s\nplugins=%s\n" % (
            image_build_id(paths, "broker"), dir_hash(os.path.join(paths.root, "kits", "broker")),
            dir_hash(os.path.join(paths.config, "broker-kit")), broker_plugins(paths).encode())
    elif name == COORDINATOR_SANDBOX:
        text = b"image=%s\nkit=%s\nbroker=%s\n" % (
            image_build_id(paths, "developer"), dir_hash(os.path.join(paths.root, "kits", "coordinator")),
            _yes_no(roles.coordinator_broker_tools(paths)))
    else:
        role = name[len("factory-"):] if name.startswith("factory-") else name
        workspace = roles.role_get(paths, role, "WORKSPACE")
        text = b"image=%s\nkit=%s\nworkspace=%s\nbroker=%s\n" % (
            image_build_id(paths, roles.role_get(paths, role, "TEMPLATE")),
            dir_hash(os.path.join(paths.root, "roles", role, "kit")), _text(workspace),
            _yes_no(roles.role_get(paths, role, "BROKER_TOOLS")))
        if workspace == "rw":
            text += b"layout=%s\n" % RW_LAYOUT.encode()
    return text


def sandbox_fingerprint(paths, name):
    """The hex digest of fingerprint_text, as the bash records it."""
    return hashlib.sha256(fingerprint_text(paths, name)).hexdigest()


def broker_fingerprint(paths):
    return sandbox_fingerprint(paths, sbx.BROKER_SANDBOX)


def fingerprint_file(paths, name):
    return os.path.join(paths.state, "sandboxes", name)


def parts_file(paths, name):
    return os.path.join(paths.state, "sandbox-parts", name)


def record_sandbox_fingerprint(paths, name):
    """Write NAME's fingerprint to FACTORY_STATE/sandboxes/NAME, and the lines it is the digest of
    to FACTORY_STATE/sandbox-parts/NAME (a separate folder: the bash reads only the first), so
    stale_reason can say which input changed."""
    text = fingerprint_text(paths, name)
    os.makedirs(os.path.join(paths.state, "sandbox-parts"), exist_ok=True)
    with open(parts_file(paths, name), "wb") as f:
        f.write(text)
    os.makedirs(os.path.join(paths.state, "sandboxes"), exist_ok=True)
    with open(fingerprint_file(paths, name), "w") as f:
        f.write(hashlib.sha256(text).hexdigest() + "\n")


def checked_sandboxes(paths):
    """The sandboxes that can go stale, in the order they are listed: the broker, the Coordinator
    and one per role this machine runs."""
    return [sbx.BROKER_SANDBOX, COORDINATOR_SANDBOX] + [roles.role_sandbox(r) for r in roles.list_roles(paths)]


def recorded_sandboxes(paths):
    """The checked sandboxes with a recorded fingerprint, i.e. created by a start that records
    them. For callers that must not ask sbx which sandboxes exist (the dashboard): a sandbox that
    predates fingerprints, or was never created, is not named."""
    return [n for n in checked_sandboxes(paths) if os.path.exists(fingerprint_file(paths, n))]


def is_stale(paths, name):
    """True if NAME's inputs changed since it was created, or no fingerprint of it is readable."""
    try:
        with open(fingerprint_file(paths, name), "rb") as f:
            recorded = f.read().rstrip(b"\n")
    except OSError:
        recorded = None
    return recorded != sandbox_fingerprint(paths, name).encode()


def stale_sandboxes(ctx):
    """The existing broker, Coordinator and role sandboxes whose inputs changed since they were
    created (or that predate fingerprints), in that order. Like bash, an `sbx ls -q` that fails
    reads as no sandboxes (so nothing is stale), and a missing or unreadable fingerprint file
    is a mismatch."""
    existing = set(sbx.sandbox_names(ctx))
    return [name for name in checked_sandboxes(ctx.paths) if name in existing and is_stale(ctx.paths, name)]


def _lines(text):
    """The key=value lines of a fingerprint text as {key: value}."""
    out = {}
    for line in text.split(b"\n"):
        key, eq, value = line.partition(b"=")
        if eq:
            out[key.decode("ascii", "replace")] = value
    return out


_WHY = {
    "kit": "kit changed",
    "local-kit": "broker-kit/ changed",
    "plugins": "broker plugins changed",
    "broker": "access to broker tools changed",
    "workspace": "workspace mode changed",
    "layout": "lane layout changed",
}


def stale_reason(paths, name):
    """One line on why NAME is stale: which inputs differ from the lines recorded when it was
    created. A sandbox with no recorded lines (made before they were kept) gets a general one."""
    try:
        with open(parts_file(paths, name), "rb") as f:
            old = f.read()
    except OSError:
        return "made from an older image, kit or setting"
    new = fingerprint_text(paths, name)
    was, now = _lines(old), _lines(new)
    why = []
    for key in sorted(set(was) | set(now)):
        if was.get(key) == now.get(key):
            continue
        if key == "image":
            why.append("image rebuilt (policy.toml or broker code)" if name == sbx.BROKER_SANDBOX
                       else "image rebuilt (template)")
        else:
            why.append(_WHY.get(key, key + " changed"))
    return ", ".join(why) or "made from an older image, kit or setting"
