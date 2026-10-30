"""The write gate's diff inspector: a held push's patch as data for a page.

Passdesk (passdesk-broker) stores the patch of each push it holds for a
person under its state folder (approvals/patches/<sha256>.patch), made with

    git log -p --stat --cc --no-ext-diff --no-textconv --no-color --reverse
        --format='commit %H%nAuthor: %an <%ae>%nDate:   %aI%n%n%w(0,4,4)%B%n'

so a commit message line never starts at column 0, capped (cut at a line
end, then one trailer line "[passdesk: patch truncated at N of M bytes]").
The dashboard's control unit (dashboard_controls.py) reads it, checks it
against the card's digest and calls view() here; the page only ever draws
what this returns, with textContent.

parse() reads the text as git writes it, never by guessing from what a line
looks like where git's structure says what it is:

  - a commit starts with "commit <hex>" at column 0; its header lines run to
    the first blank line; its message is the 4-space-indented lines after
    that; everything else up to the first "diff --git" / "diff --cc" (the
    "---" line, --stat lines, blank lines) is skipped;
  - a file section's extended headers ("rename from", "new file mode",
    "Binary files ... differ", "---", "+++" ...) run to its first hunk
    header or the next section;
  - a hunk is consumed BY THE COUNTS in its "@@ -a,b +c,d @@" header
    ("@@@ -a,b -c,d +e,f @@@" and so on for a --cc merge, one column per
    parent): content that looks like a header, a commit line or a trailer
    is content, because every content line is still owed to the counts. A
    hunk cut short by the cap ends at the end of the text. A line that can't
    be content (wrong prefix, or more of one side than the header says)
    ends the hunk and marks the file "malformed", as does a hunk the end
    of the text cuts short when there is no trailer (git never stops
    mid-hunk, so lines were lost or merged on the way).

Every string that came from the patch is sanitised here, so no page can
forget to: bidi controls, zero-width and other invisible format characters,
the BOM, C0 (but tab), DEL and C1 controls, and the line and paragraph
separators become a visible token like "⟨U+202E⟩", and the file (or commit)
is flagged "bidi", "invisible" or "control"; a line longer than LINE_MAX
characters is cut, with "⟨… N more chars⟩", and flagged "longline"; a CRLF
line end is dropped and flagged "crlf". The text is split on "\\n" only
(never str.splitlines(), which also splits on \\x0b, \\x1c-\\x1e, \\x85 and
U+2028/2029, all of which are content here).

Standard library only; runs on the macOS system Python (3.9).
"""

from __future__ import annotations

import json
import re
import unicodedata

LINE_MAX = 2000                    # characters of one line shown before it is cut
MAX_RESPONSE = 3 * 1024 * 1024     # bytes of JSON view() returns at most (about)

COMMIT = re.compile(r"commit ([0-9a-f]{40}|[0-9a-f]{64})(?: \(from [0-9a-f]{40,64}\))?")
TRAILER = re.compile(r"\[passdesk: patch truncated at (\d+) of (\d+) bytes\]")
HUNK = re.compile(r"@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@(.*)", re.S)
CC_HUNK = re.compile(r"(@{3,})((?: -\d+(?:,\d+)?)+) \+(\d+)(?:,(\d+))? \1(.*)", re.S)
CC_RANGE = re.compile(r"-\d+(?:,(\d+))?")

# Characters never shown as themselves, and the flag each raises.
BIDI = set(map(chr, [0x061C, 0x200E, 0x200F, *range(0x202A, 0x202F), *range(0x2066, 0x206A)]))
INVISIBLE_EXTRA = set(map(chr, [0x034F, 0x115F, 0x1160, 0x17B4, 0x17B5, 0x180B, 0x180C, 0x180D, 0x180E, 0x180F,
                                0x3164, 0xFFA0, *range(0xFE00, 0xFE10)]))
OK_CHARS = {"\t"}


def char_flag(ch: str) -> str | None:
    """The flag ch raises, or None if it is shown as itself."""
    o = ord(ch)
    if o < 0x20:
        return None if ch in OK_CHARS else "control"
    if o < 0x7F:
        return None
    if o <= 0x9F or o in (0x2028, 0x2029):
        return "control"
    if ch in BIDI:
        return "bidi"
    if ch in INVISIBLE_EXTRA or 0xE0100 <= o <= 0xE01EF or unicodedata.category(ch) == "Cf":
        return "invisible"  # zero-width, BOM, tag characters, variation selectors...
    return None


def clean(text: str, flags: set, limit: int = LINE_MAX) -> str:
    """One line of patch text as it may be shown: cut at limit characters,
    then every character that isn't shown as itself made a visible token.
    Adds what it found to flags."""
    more = len(text) - limit
    if more > 0:
        text = text[:limit]
        flags.add("longline")
    out = []
    for ch in text:
        f = char_flag(ch)
        if f is None:
            out.append(ch)
        else:
            flags.add(f)
            out.append("⟨U+{:04X}⟩".format(ord(ch)))
    if more > 0:
        out.append("⟨… {} more chars⟩".format(more))
    return "".join(out)


_ESC = {"a": 7, "b": 8, "t": 9, "n": 10, "v": 11, "f": 12, "r": 13, '"': 34, "\\": 92}


def unquote(s: str) -> tuple:
    """(path, rest) for a path git may have C-quoted ("a/f\\303\\251") at the
    start of s; an unquoted path runs to the end of s."""
    if not s.startswith('"'):
        return s, ""
    out, i = bytearray(), 1
    while i < len(s):
        ch = s[i]
        if ch == '"':
            return out.decode("utf-8", "replace"), s[i + 1:]
        if ch == "\\" and i + 1 < len(s):
            nxt = s[i + 1]
            if re.fullmatch(r"[0-7]{3}", s[i + 1:i + 4]):
                out.append(int(s[i + 1:i + 4], 8) & 0xFF)
                i += 4
                continue
            if nxt in _ESC:
                out.append(_ESC[nxt])
                i += 2
                continue
        out.extend(ch.encode("utf-8", "replace"))
        i += 1
    return out.decode("utf-8", "replace"), ""  # no closing quote: take it as it is


def strip_prefix(path: str) -> str:
    return path[2:] if path[:2] in ("a/", "b/") else path


def git_paths(rest: str) -> tuple:
    """(old, new) from what follows "diff --git ", or (None, None)."""
    if rest.startswith('"'):
        a, tail = unquote(rest)
        tail = tail[1:] if tail.startswith(" ") else tail
        b, _ = unquote(tail)
        return strip_prefix(a), strip_prefix(b)
    if '"' in rest:  # "a/x" unquoted, "b/y" quoted
        a, _, b = rest.partition(' "')
        return strip_prefix(a), strip_prefix(unquote('"' + b)[0])
    # Both unquoted: "a/P b/P" splits in the middle when the two names are the
    # same (a rename's real names come from its "rename from/to" lines).
    n = len(rest)
    if n % 2 == 1:
        half = (n - 1) // 2
        a, b = rest[:half], rest[half + 1:]
        if rest[half] == " " and a[2:] == b[2:]:
            return strip_prefix(a), strip_prefix(b)
    a, _, b = rest.partition(" b/")
    return strip_prefix(a), b if b else None


def header_path(rest: str) -> str | None:
    """The path of a "--- " / "+++ " line (None for /dev/null). Git ends a
    name with a space in it with a tab."""
    if rest.startswith('"'):
        p, _ = unquote(rest)
    else:
        p = rest[:-1] if rest.endswith("\t") else rest
    return None if p == "/dev/null" else strip_prefix(p)


class _Lines:
    def __init__(self, lines: list, truncated: bool = False):
        self.lines, self.i, self.truncated = lines, 0, truncated

    def peek(self) -> str | None:
        return self.lines[self.i] if self.i < len(self.lines) else None

    def take(self) -> str:
        self.i += 1
        return self.lines[self.i - 1]


def is_section(line: str) -> bool:
    return line.startswith(("diff --git ", "diff --cc ", "diff --combined ")) or COMMIT.fullmatch(line) is not None


def parse(text: str, truncated: bool = False) -> dict:
    """{"truncated", "commits": [...]} for a patch as described above (the
    response's other fields are added by view()). truncated says the text was
    cut by whoever stored it without a trailer line (the dispatch bridge's
    job diffs keep that in their JSON), so a hunk the text ends in is cut short."""
    lines = text.split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    # The trailer counts only as the very last line: anywhere else, a line
    # like it is content (indented in a message, prefixed in a hunk).
    if lines and TRAILER.fullmatch(lines[-1]):
        lines.pop()
        truncated = True
    src = _Lines(lines, truncated)
    commits: list = []
    commit = None
    while src.peek() is not None:
        line = src.take()
        m = COMMIT.fullmatch(line)
        if m:
            commit = _commit(src, m.group(1))
            commits.append(commit)
        elif line.startswith(("diff --git ", "diff --cc ", "diff --combined ")):
            if commit is None:  # a bare diff, without a commit header
                commit = {"sha": "", "author": "", "date": "", "message": "", "merge": False, "flags": [],
                          "files": []}
                commits.append(commit)
            commit["files"].append(_file(src, line))
            if commit["files"][-1].pop("_cc"):
                commit["merge"] = True
        # Anything else between sections ("---", --stat lines, blank lines) is skipped.
    return {"truncated": truncated, "commits": commits}


def _commit(src: _Lines, sha: str) -> dict:
    flags: set = set()
    author = date = ""
    while src.peek() not in (None, "") and not is_section(src.peek()):
        line = src.take()
        if line.startswith("Author: "):
            author = clean(line[8:], flags)
        elif line.startswith("Date:"):
            date = clean(line[5:].strip(), flags)
    if src.peek() == "":
        src.take()
    message = []
    while src.peek() is not None and src.peek().startswith("    "):
        message.append(clean(src.take()[4:], flags))
    while message and not message[-1].strip():
        message.pop()
    return {"sha": sha, "author": author, "date": date, "message": "\n".join(message), "merge": False,
            "flags": sorted(flags), "files": []}


def _file(src: _Lines, first: str) -> dict:
    flags: set = set()
    cc = not first.startswith("diff --git ")
    old = new = None
    status = "M"
    binary = False
    if cc:
        new = unquote(first.split(" ", 2)[2])[0]
        old = new
    else:
        old, new = git_paths(first[len("diff --git "):])
    renamed_from = renamed_to = None
    copied = False
    # Extended headers, up to the first hunk or the next section.
    while True:
        line = src.peek()
        if line is None or is_section(line) or line.startswith("@@"):
            break
        src.take()
        if line.startswith("rename from "):
            renamed_from = unquote(line[12:])[0]
        elif line.startswith("rename to "):
            renamed_to = unquote(line[10:])[0]
        elif line.startswith("copy from "):
            renamed_from, copied = unquote(line[10:])[0], True
        elif line.startswith("copy to "):
            renamed_to, copied = unquote(line[8:])[0], True
        elif line.startswith("new file mode"):
            status = "A"
        elif line.startswith("deleted file mode"):
            status = "D"
        elif line.startswith("--- "):
            p = header_path(line[4:])
            if p is None:
                status = "A"
            else:
                old = p
        elif line.startswith("+++ "):
            p = header_path(line[4:])
            if p is None:
                status = "D"
            else:
                new = p
        elif line.startswith("Binary files ") or line == "GIT binary patch":
            binary = True
    if renamed_from is not None:
        old = renamed_from
        status = "A" if copied else "R"
    if renamed_to is not None:
        new = renamed_to
    if binary:
        status = "B"
    path = new if new is not None else old
    hunks, adds, dels = [], 0, 0
    while src.peek() is not None and src.peek().startswith("@@"):
        hunk, a, d = _hunk(src, flags)
        if hunk is None:
            break
        hunks.append(hunk)
        adds, dels = adds + a, dels + d
    # After its hunks, git writes the next section (or nothing): anything else
    # is a line the counts didn't account for.
    if src.peek() is not None and not is_section(src.peek()):
        flags.add("malformed")
    shown_old = None
    if old is not None and old != path:
        shown_old = clean(old, flags)
    return {"path": clean(path or "?", flags), "old_path": shown_old, "status": status, "adds": adds, "dels": dels,
            "flags": sorted(flags), "hunks": hunks, "_cc": cc}


def _hunk(src: _Lines, flags: set) -> tuple:
    """(hunk, adds, dels) consumed by its header's counts, or (None, 0, 0)
    (and the file marked malformed) when the header can't be read."""
    head = src.take()
    m = HUNK.fullmatch(head)
    if m:
        width = 1
        need = [1 if m.group(2) is None else int(m.group(2))]  # the old side's count
        result = 1 if m.group(4) is None else int(m.group(4))
    else:
        m = CC_HUNK.fullmatch(head)
        if not m:
            flags.add("malformed")
            return None, 0, 0
        width = len(m.group(1)) - 1
        ranges = m.group(2).split()
        if len(ranges) != width:
            flags.add("malformed")
            return None, 0, 0
        need = [1 if r.group(1) is None else int(r.group(1)) for r in (CC_RANGE.fullmatch(x) for x in ranges)]
        result = 1 if m.group(4) is None else int(m.group(4))
    hunk = {"header": clean(head, flags), "lines": []}
    out = hunk["lines"]
    adds = dels = 0
    crlf = False

    def owed() -> bool:
        return result > 0 or any(n > 0 for n in need)

    while owed():
        line = src.peek()
        if line is None:
            # Cut by the cap: the hunk ends with the text. Git itself never
            # ends mid-hunk, so without the trailer lines went missing.
            if not src.truncated:
                flags.add("malformed")
            break
        if line.startswith("\\"):
            src.take()
            out.append(["\\", clean(line[1:].lstrip(" "), flags)])
            continue
        cols = line[:width]
        if len(cols) < width or any(c not in " +-" for c in cols):
            flags.add("malformed")
            break
        if "-" in cols:
            # In each parent with "-", gone from the result.
            if width > 1 and "+" in cols:
                flags.add("malformed")
                break
            hit = [k for k, c in enumerate(cols) if c == "-"]
            if any(need[k] <= 0 for k in hit):
                flags.add("malformed")
                break
            for k in hit:
                need[k] -= 1
            kind = "-"
            dels += 1
        else:
            # In the result, and in each parent whose column is " ".
            hit = [k for k, c in enumerate(cols) if c == " "]
            if result <= 0 or any(need[k] <= 0 for k in hit):
                flags.add("malformed")
                break
            result -= 1
            for k in hit:
                need[k] -= 1
            kind = "+" if "+" in cols else " "
            if kind == "+":
                adds += 1
        src.take()
        body = line[width:]
        if body.endswith("\r"):
            body, crlf = body[:-1], True
        item = [kind, clean(body, flags)]
        if width > 1:
            item.append(cols)
        out.append(item)
    # A "\ No newline at end of file" after the hunk's last line belongs to it.
    while src.peek() is not None and src.peek().startswith("\\"):
        out.append(["\\", clean(src.take()[1:].lstrip(" "), flags)])
    if crlf:
        flags.add("crlf")
    return hunk, adds, dels


def _size(obj) -> int:
    return len(json.dumps(obj, ensure_ascii=False).encode("utf-8"))


CUT = ["✂", "CUT HERE · the rest of this push is not shown"]  # the last line shown of a truncated patch
CARD_FILES = 50   # names from the card's files list compared with what is shown


def view(text: str, *, patch_bytes: int, files_total, card_files=None, card_truncated: bool = False,
         max_response: int = MAX_RESPONSE) -> dict:
    """The /cu/api/patch response for a patch's text: parse() plus the
    counts, cut to about max_response bytes of JSON: commits, files and
    hunks in order until the budget runs out, the last hunk cut to the lines
    that fit, a file none of whose lines fit left out, and the response then
    says truncated.

    When truncated, the last file shown ends with the CUT line, files_missing
    names (sanitised) the card's files (the first CARD_FILES of its list)
    that nothing shown names, and files_missing_more counts the rest of
    file_count that is neither shown nor named. files_shown counts the
    card's own files that are shown when its list is complete, else the
    distinct paths shown, never more than files_total."""
    parsed = parse(text)
    truncated = parsed["truncated"] or bool(card_truncated)
    budget = max_response - 4096
    commits, paths, seen, full = [], set(), set(), False
    for c in parsed["commits"]:
        files = c.pop("files")
        budget -= _size(c)
        if budget < 0:
            full = True
            break
        c["files"] = []
        commits.append(c)
        for f in files:
            hunks = f.pop("hunks")
            budget -= _size(f)
            if budget < 0:
                full = True
                break
            kept = []
            for h in hunks:
                n = _size(h)
                if n > budget:
                    full = True
                    # The lines of it that fit, if any: a huge hunk still shows its start.
                    part = {"header": h["header"], "lines": []}
                    budget -= _size(part)
                    for ln in h["lines"]:
                        budget -= _size(ln) + 1
                        if budget < 0:
                            break
                        part["lines"].append(ln)
                    if part["lines"]:
                        kept.append(part)
                    break
                budget -= n
                kept.append(h)
            if full and not kept and hunks:
                break  # none of its changes fit: leave the file out
            f["hunks"] = kept
            c["files"].append(f)
            paths.add(f["path"])
            if f["old_path"] is not None:
                seen.add(f["old_path"])
            if full:
                break
        if full:
            break
    truncated = truncated or full
    if isinstance(files_total, bool) or not isinstance(files_total, int) or files_total < 0:
        files_total = None
    seen |= paths
    names = []
    for f in (card_files if isinstance(card_files, list) else [])[:CARD_FILES]:
        if isinstance(f, str):
            name = clean(f, set())
            if name not in names:
                names.append(name)
    if files_total is not None and names and len(names) == files_total:
        shown = sum(1 for n in names if n in seen)  # the card's net files: the true count
    else:
        shown = len(paths) if files_total is None else min(len(paths), files_total)
    missing, more = [], 0
    if truncated:
        missing = [n for n in names if n not in seen]
        if files_total is not None:
            more = max(0, files_total - shown - len(missing))
        last = commits[-1]["files"][-1] if commits and commits[-1]["files"] else None
        if last is not None:
            if not last["hunks"]:
                last["hunks"].append({"header": "", "lines": []})
            last["hunks"][-1]["lines"].append(list(CUT))
    return {"truncated": truncated, "patch_bytes": int(patch_bytes), "files_total": files_total,
            "files_shown": shown, "files_missing": missing, "files_missing_more": more, "commits": commits}
