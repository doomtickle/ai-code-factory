"""Running a program with bytes on its stdin and its output kept as bytes: what the
credential code (factory/keychain.py, factory/secrets.py) needs, where Context.capture
reads text and gives the program no stdin. A value goes only to a program's stdin, and a
lookup's output (the value) is kept, never printed. Every program runs from an argv list,
never through a shell on the host.
"""

import subprocess

# Where a program's output goes: passed on to the command's own stream, dropped, or kept
# (returned, never printed: a lookup's output is the value).
PASS, DROP, KEEP = "pass", "drop", "keep"


def run_tool(ctx, argv, data=None, out=PASS, err=PASS):
    """Run argv with DATA (bytes) on its stdin, or /dev/null, and return (status, stdout,
    stderr) as bytes (empty unless kept). The status is a shell's: 127 not found, 126 not
    runnable, 128+n killed by signal n."""
    pipe = {PASS: subprocess.PIPE, KEEP: subprocess.PIPE, DROP: subprocess.DEVNULL}
    try:
        cp = subprocess.run(list(argv), input=data, stdin=None if data is not None else subprocess.DEVNULL,
                            stdout=pipe[out], stderr=pipe[err], env=dict(ctx.environ))
    except FileNotFoundError:
        status, got_out, got_err = 127, b"", b"%s: command not found\n" % argv[0].encode()
    except OSError as e:
        status, got_out, got_err = 126, b"", ("%s: %s\n" % (argv[0], e.strerror or "cannot run")).encode()
    else:
        status = 128 - cp.returncode if cp.returncode < 0 else cp.returncode
        got_out, got_err = cp.stdout or b"", cp.stderr or b""
    if out == PASS and got_out:
        ctx.stdout.write(got_out.decode("utf-8", "replace"))
    if err == PASS and got_err:
        to_stderr(ctx, got_err.decode("utf-8", "replace"))
    return status, (got_out if out == KEEP else b""), (got_err if err == KEEP else b"")


def to_stderr(ctx, text):
    """Write TEXT on stderr after what is already on stdout, as bash's unbuffered echo does."""
    ctx._flush()
    ctx.stderr.write(text)
