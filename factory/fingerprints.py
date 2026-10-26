"""Sandbox fingerprints: a digest of what a sandbox was created from, recorded when it is
created, so `factory start` can tell it is out of date. Port of the broker's case of
sandbox_fingerprint and of record_sandbox_fingerprint, image_build_id and dir_hash
(scripts/lib/roles.sh), and of broker_plugins (scripts/lib/common.sh). The Coordinator's and
the roles' fingerprints, and stale_sandboxes, are still bash (02-launch-factory.sh and `start`
use them; step 7 part C ports them). The digest must stay byte for byte the bash one, which
compares against what this records; factory/test_broker_start.py runs both on the same
fixtures.
"""

import hashlib
import os
import stat

from factory import sbx


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
        return ("factory-%s:%s" % (image, sbx.TEMPLATE_TAG)).encode()


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


def broker_fingerprint(paths):
    """What the broker sandbox is created from, as the hex digest the bash records."""
    text = b"image=%s\nkit=%s\nlocal-kit=%s\nplugins=%s\n" % (
        image_build_id(paths, "broker"), dir_hash(os.path.join(paths.root, "kits", "broker")),
        dir_hash(os.path.join(paths.config, "broker-kit")), broker_plugins(paths).encode())
    return hashlib.sha256(text).hexdigest()


def record_broker_fingerprint(paths):
    """Write the broker's fingerprint to FACTORY_STATE/sandboxes/factory-broker."""
    folder = os.path.join(paths.state, "sandboxes")
    os.makedirs(folder, exist_ok=True)
    with open(os.path.join(folder, sbx.BROKER_SANDBOX), "w") as f:
        f.write(broker_fingerprint(paths) + "\n")
