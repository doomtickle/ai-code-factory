"""factory secret <save|forget|set|rm|ls> <name>: the broker credentials sbx cannot
proxy-inject, and the GitHub token (CLAUDE.md, "Credentials").

- save: store the value once in this machine's keychain (factory-<name>: macOS Keychain,
  Linux secret-tool, or the Windows Credential Manager on WSL2), so every broker start
  delivers it; the running broker gets it right away.
- forget: delete it from the keychain and from the running broker.
- set / rm / ls: for machines with no keychain, deliver a value to the running broker only
  (until it is recreated), remove it, list what was delivered this way. A name `set`
  delivered is marked, so the next `factory start` leaves it alone.

Port of cmd_secret in bin/factory, with the output unchanged. The value is read from a
hidden prompt or stdin and goes only to a program's stdin (factory/secrets.py).
"""

import os
import sys

from factory import preflight, sbx, secrets
from factory.context import FactoryError

USAGE = "usage: factory secret <save|forget|set|rm|ls> <name>"


def _stdin_fd():
    return sys.stdin.fileno() if sys.stdin is not None else 0


def _read_hidden(ctx, fd, name):
    """A line from the terminal on FD with echo off, without its newline."""
    import termios

    old = termios.tcgetattr(fd)
    new = list(old)
    new[3] &= ~termios.ECHO
    termios.tcsetattr(fd, termios.TCSADRAIN, new)
    chunks = []
    try:
        # The prompt only once echo is off, so nothing typed after it can show.
        ctx.stderr.write("Value for %s (input hidden): " % name)
        ctx.stderr.flush()
        while True:
            byte = os.read(fd, 1)
            if not byte or byte == b"\n":
                break
            chunks.append(byte)
    except KeyboardInterrupt:
        chunks = []
        ctx.stderr.write("\n")
        raise SystemExit(130)
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)
    ctx.stderr.write("\n")
    return b"".join(chunks)


def read_value(ctx, name):
    """The value from a hidden prompt (a terminal) or all of stdin minus exactly one
    trailing newline (so `echo token | factory secret set jira` works). Raises on an
    empty value."""
    fd = _stdin_fd()
    if os.isatty(fd):
        value = _read_hidden(ctx, fd, name)
    else:
        chunks = []
        while True:
            chunk = os.read(fd, 65536)
            if not chunk:
                break
            chunks.append(chunk)
        value = b"".join(chunks)
        if value.endswith(b"\n"):
            value = value[:-1]
    if not value:
        raise FactoryError("empty value for '%s'" % name)
    return value


def _one_name(args, verb):
    if len(args) != 1:
        raise FactoryError("usage: factory secret %s <name>" % verb)
    return args[0]


def _check_secret_name(name):
    if not secrets.secret_name_valid(name):
        raise FactoryError("unknown secret '%s' (one of: %s)" % (name, " ".join(secrets.SECRET_NAMES)))


def _check_keychain_name(name):
    if not secrets.keychain_name_valid(name):
        raise FactoryError("unknown secret '%s' (one of: %s)" % (name, " ".join(secrets.KEYCHAIN_NAMES)))


def _require_broker_running(ctx):
    """bash's require_sbx, then the broker sandbox must be running."""
    if not preflight.which(ctx, "sbx"):
        raise FactoryError("'sbx' not found in PATH (see README.md)")
    got = sbx.installed_version(ctx)
    if got != preflight.SBX_VERSION:
        raise FactoryError("sbx %s required, found '%s' (see README.md)" % (preflight.SBX_VERSION, got or "unknown"))
    if not sbx.sandbox_running(ctx, sbx.BROKER_SANDBOX):
        raise FactoryError("the broker sandbox is not running; run 'factory start' first")


def _broker_up(ctx):
    return bool(preflight.which(ctx, "sbx")) and sbx.sandbox_running(ctx, sbx.BROKER_SANDBOX)


def secret_set(ctx, args):
    name = _one_name(args, "set")
    _check_secret_name(name)
    _require_broker_running(ctx)
    value = read_value(ctx, name)
    if not secrets.deliver_file(ctx, name, value):
        raise FactoryError("could not deliver '%s' to the broker sandbox" % name)
    del value
    if not secrets.mark_manual(ctx.paths, name):
        raise FactoryError("delivered '%s' but could not record it as managed by 'factory secret'; "
                           "the next 'factory start' may remove it" % name)
    ctx.ok("delivered '%s' to the broker sandbox; 'factory start' will no longer touch it from the keychain" % name)
    return 0


def secret_save(ctx, args):
    name = _one_name(args, "save")
    _check_keychain_name(name)
    service = "factory-" + name
    backend = preflight.keychain_backend(ctx)
    if backend == "macos":
        if not os.isatty(_stdin_fd()):
            raise FactoryError("on macOS run 'factory secret save %s' in a terminal: "
                               "Keychain prompts for the value itself" % name)
        ctx.say("Keychain will ask for the value of %s twice." % name)
        # -w last, with no value: security prompts for it on the terminal.
        if ctx.run(["security", "add-generic-password", "-U", "-s", service, "-a", "factory", "-w"]) != 0:
            raise FactoryError("could not save %s in the Keychain" % service)
    elif backend in ("secret-tool", "wincred"):
        value = read_value(ctx, name)
        if not secrets.keychain_store(ctx, service, value):
            raise FactoryError("could not save %s in the keychain (%s)" % (service, backend))
        del value
    else:
        raise FactoryError("this machine has no keychain the factory can use (macOS Keychain, secret-tool, or "
                           "Windows Credential Manager on WSL2); use 'factory secret set %s' after each broker "
                           "start instead" % name)
    if secrets.keychain_has(ctx, service) != secrets.FOUND:
        raise FactoryError("saved %s, but reading it back failed" % service)
    ctx.ok("saved %s in the keychain (%s); every broker start delivers it from now on" % (service, backend))

    if name != "github":
        secrets.unmark_manual(ctx.paths, name)  # the keychain manages it now
    if not _broker_up(ctx):
        ctx.say("The broker isn't running; 'factory start' will deliver it.")
        return 0
    if name == "github":
        secrets.github_sync_from_keychain(ctx)
        return 0
    status = secrets.sync_from_keychain(ctx, name, quiet=True)
    if status == 0:
        ctx.ok("delivered '%s' to the running broker" % name)
    return status


GONE, FAILED, UNTOUCHED = 0, 1, 2


def _forget_broker_copy(ctx, name):
    """Remove the broker's copy of NAME (the file, or the broker-scoped sbx secret): GONE,
    FAILED (it may still be active) or UNTOUCHED (no sbx, or the broker isn't running)."""
    if not _broker_up(ctx):
        return UNTOUCHED
    if name == "github":
        argv = ["sbx", "secret", "rm", "github", "--sandbox", sbx.BROKER_SANDBOX, "-f"]
        if secrets.run_tool(ctx, argv, out=secrets.DROP, err=secrets.DROP)[0] != 0:
            # sbx may refuse because there is nothing to remove; trust only a listing.
            status, listing, _ = secrets.run_tool(ctx, ["sbx", "secret", "ls"], out=secrets.KEEP, err=secrets.DROP)
            if status != 0:
                return FAILED
            for line in listing.decode("utf-8", "replace").split("\n"):
                fields = line.split()
                if len(fields) >= 3 and fields[0] == sbx.BROKER_SANDBOX and fields[2] == "github":
                    return FAILED
        ctx.ok("removed the broker's GitHub token")
    else:
        if not secrets.revoke_file(ctx, name, out=secrets.DROP, err=secrets.DROP):
            return FAILED
        secrets.unmark_manual(ctx.paths, name)
        ctx.ok("removed '%s' from the broker sandbox" % name)
    return GONE


def _forget_failed(name, broker, problem, fix):
    """The error for a keychain entry that could not be deleted, saying truthfully what
    happened to the broker's copy (BROKER: _forget_broker_copy's answer)."""
    service = "factory-" + name
    if broker == GONE:
        head = ("the broker's copy of '%s' is revoked, but %s could not be deleted from the keychain (%s)"
                % (name, service, problem))
    elif broker == UNTOUCHED:
        head = ("the broker isn't running, so its copy of '%s' was NOT touched and may still be active in the "
                "stopped broker; %s could not be deleted from the keychain either (%s)" % (name, service, problem))
    else:
        head = ("could not remove '%s' from the broker sandbox, so it may still be active; %s could not be "
                "deleted from the keychain either (%s)" % (name, service, problem))
    if broker == UNTOUCHED:
        return FactoryError("%s. %s, then run 'factory secret forget %s' again before the next 'factory start'"
                            % (head, fix, name))
    return FactoryError("%s. %s, then run 'factory secret forget %s' again" % (head, fix, name))


def secret_forget(ctx, args):
    """Delete factory-NAME from the keychain, and its copy in the running broker. If the
    entry can't be deleted, the broker's copy is still removed first: forget is how a leaked
    token is killed. What was and wasn't revoked is always stated."""
    name = _one_name(args, "forget")
    _check_keychain_name(name)
    service = "factory-" + name
    secrets.keychain_delete(ctx, service)
    status = secrets.keychain_has(ctx, service)
    if status == secrets.FOUND:
        broker = _forget_broker_copy(ctx, name)
        raise _forget_failed(name, broker, "the delete failed, so the next 'factory start' would deliver it again",
                             "Delete it by hand (e.g. in your keychain app)")
    if status != secrets.NOT_STORED:
        broker = _forget_broker_copy(ctx, name)
        raise _forget_failed(name, broker, "the keychain is locked or unavailable", "Unlock it")
    ctx.ok("deleted %s from the keychain" % service)
    broker = _forget_broker_copy(ctx, name)
    if broker == UNTOUCHED:
        ctx.warn("the broker isn't running, so its copy of '%s' was not touched and may still be active; start the "
                 "broker and run 'factory secret forget %s' again to remove it" % (name, name))
    elif broker == FAILED:
        raise FactoryError("deleted %s from the keychain, but could not remove '%s' from the broker sandbox; "
                           "it may still be active" % (service, name))
    return 0


def secret_rm(ctx, args):
    name = _one_name(args, "rm")
    _check_secret_name(name)
    _require_broker_running(ctx)
    if not secrets.revoke_file(ctx, name):
        raise FactoryError("could not remove '%s' from the broker sandbox; it may still be active" % name)
    secrets.unmark_manual(ctx.paths, name)
    ctx.ok("removed '%s' from the broker sandbox" % name)
    if secrets.keychain_has(ctx, "factory-" + name) == secrets.FOUND:
        ctx.warn("a keychain entry for 'factory-%s' still exists; the next 'factory start' will deliver it again. "
                 "To prevent that: factory secret forget %s" % (name, name))
    return 0


def secret_ls(ctx, args):
    names = secrets.manual_names(ctx.paths)
    for name in names:
        ctx.say("  " + name)
    if not names:
        ctx.say("No secrets are managed with 'factory secret set' (see: factory secret).")
    return 0


SUBCOMMANDS = {"set": secret_set, "rm": secret_rm, "ls": secret_ls, "save": secret_save, "forget": secret_forget}


def run(ctx, args):
    handler = SUBCOMMANDS.get(args[0] if args else "")
    if handler is None:
        raise FactoryError(USAGE)
    try:
        return handler(ctx, args[1:])
    except FactoryError:
        ctx._flush()  # what was said on stdout comes before the error, as in bash
        raise
