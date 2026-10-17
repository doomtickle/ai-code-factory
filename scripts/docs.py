"""Factory docs: renders the factory's markdown docs into one themed,
self-contained HTML page for `factory docs` (bin/factory).

Sources, read-only, from the factory checkout (--root):
  docs/guides/*.md   task-oriented guides, in file-name order (optional)
  README.md          the reference, split into pages at its `## ` headings (required)
  docs/setup-wsl.md  Windows setup (optional)

The page is generated from these files every time, so it can't drift from
them. It needs no network: no CDNs, no web fonts, all CSS and JS inline, and it
works from file://.

The markdown renderer handles the subset the docs use (ATX headings,
paragraphs, nested lists, fenced code, GFM pipe tables, blockquotes, rules,
inline code, emphasis, links and autolinks, backslash escapes). Raw HTML in the
markdown is escaped, never passed through. Links to the rendered markdown files
become in-page links; other relative links become file:// links into the
checkout, and only if they stay inside it; any other scheme renders as text.

  docs.py --root <repo> [--out <file>]

With --out the page is written atomically and nothing is printed; without it
the page goes to stdout. Exit 1 with a one-line message on stderr on error.

Standard library only; compatible with the macOS system Python (3.9).
"""

from __future__ import annotations

import argparse
import html
import os
import re
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from urllib.parse import quote, unquote


class DocsError(Exception):
    """A problem that stops the page being built (reported as one line)."""


def esc(text: str) -> str:
    return html.escape(text, quote=True)


# --------------------------------------------------------------------------
# Slugs

def slugify(plain: str) -> str:
    """GitHub's heading anchor: lowercase, drop everything but word characters,
    spaces and hyphens, then spaces become hyphens."""
    s = re.sub(r"[^\w\- ]", "", plain.strip().lower())
    return s.replace(" ", "-")


def plain_text(fragment_html: str) -> str:
    return html.unescape(re.sub(r"<[^>]+>", "", fragment_html))


# --------------------------------------------------------------------------
# Inline markdown

_ESCAPABLE = set("!\"#$%&'()*+,-./:;<=>?@[\\]^_`{|}~")
_PH_RE = re.compile("\x00(\\d+)\x00")
_AUTOLINK_RE = re.compile(r"<((?:https?://|mailto:)[^\s<>]*)>", re.IGNORECASE)
_BARE_URL_RE = re.compile(r"https?://[^\s<>\x00`]+")
_STRONG_STAR = re.compile(r"\*\*(?=[^\s*])(.+?)(?<=[^\s*])\*\*", re.S)
_STRONG_UNDER = re.compile(r"(?<![\w\\])__(?=[^\s_])(.+?)(?<=[^\s_])__(?!\w)", re.S)
_EM_STAR = re.compile(r"(?<!\*)\*(?=[^\s*])(.+?)(?<=[^\s*])\*(?!\*)", re.S)
_EM_UNDER = re.compile(r"(?<![\w\\])_(?=[^\s_])(.+?)(?<=[^\s_])_(?!\w)", re.S)
_SCHEME_RE = re.compile(r"^[a-z][a-z0-9+.\-]*:", re.IGNORECASE)


def _backtick_run(s: str, i: int) -> int:
    j = i
    while j < len(s) and s[j] == "`":
        j += 1
    return j - i


def _find_code_close(s: str, start: int, run: int) -> int:
    """Index of a backtick run of exactly `run` length at or after start, or -1."""
    j = start
    while j < len(s):
        if s[j] == "`":
            r = _backtick_run(s, j)
            if r == run:
                return j
            j += r
        else:
            j += 1
    return -1


def _find_bracket_close(s: str, i: int) -> int:
    depth = 0
    j = i
    while j < len(s):
        c = s[j]
        if c == "\\":
            j += 2
            continue
        if c == "`":
            run = _backtick_run(s, j)
            close = _find_code_close(s, j + run, run)
            j = close + run if close >= 0 else j + run
            continue
        if c == "[":
            depth += 1
        elif c == "]":
            depth -= 1
            if depth == 0:
                return j
        j += 1
    return -1


def _parse_destination(s: str, k: int):
    """Parse `(url "title")` starting at s[k] == '('. Returns (url, end) or None."""
    n = len(s)
    j = k + 1
    while j < n and s[j] in " \t\n":
        j += 1
    url = []
    if j < n and s[j] == "<":
        j += 1
        while j < n and s[j] not in ">\n":
            url.append(s[j])
            j += 1
        if j >= n or s[j] != ">":
            return None
        j += 1
    else:
        depth = 0
        while j < n:
            c = s[j]
            if c == "\\" and j + 1 < n and s[j + 1] in _ESCAPABLE:
                url.append(s[j + 1])
                j += 2
                continue
            if c in " \t\n":
                break
            if c == "(":
                depth += 1
            elif c == ")":
                if depth == 0:
                    break
                depth -= 1
            url.append(c)
            j += 1
    while j < n and s[j] in " \t\n":
        j += 1
    if j < n and s[j] in "\"'(":
        close = {"\"": "\"", "'": "'", "(": ")"}[s[j]]
        end = s.find(close, j + 1)
        if end < 0:
            return None
        j = end + 1
        while j < n and s[j] in " \t\n":
            j += 1
    if j >= n or s[j] != ")":
        return None
    return "".join(url), j + 1


def _trim_bare_url(url: str) -> str:
    while url:
        if url[-1] in ".,:;!?'\"*_~":
            url = url[:-1]
        elif url[-1] == ")" and url.count(")") > url.count("("):
            url = url[:-1]
        else:
            break
    return url


class InlineRenderer:
    """Renders one paragraph's worth of inline markdown to safe HTML.

    `resolve(url)` maps a link destination to (href, external) or None, in
    which case the link renders as plain text."""

    def __init__(self, resolve):
        self.resolve = resolve

    def render(self, text: str, links: bool = True) -> str:
        text = text.replace("\x00", "�")
        stash: list[str] = []

        def put(fragment: str) -> str:
            stash.append(fragment)
            return f"\x00{len(stash) - 1}\x00"

        out: list[str] = []
        i, n = 0, len(text)
        while i < n:
            c = text[i]
            if c == "\\" and i + 1 < n:
                nxt = text[i + 1]
                if nxt in _ESCAPABLE:
                    out.append(put(esc(nxt)))
                    i += 2
                    continue
                if nxt == "\n":
                    out.append(put("<br>") + "\n")
                    i += 2
                    continue
            if c == "`":
                run = _backtick_run(text, i)
                close = _find_code_close(text, i + run, run)
                if close < 0:
                    out.append(text[i:i + run])
                    i += run
                    continue
                code = text[i + run:close].replace("\n", " ")
                if len(code) > 2 and code[0] == " " and code[-1] == " " and code.strip():
                    code = code[1:-1]
                out.append(put(f"<code>{esc(code)}</code>"))
                i = close + run
                continue
            if c == "<":
                m = _AUTOLINK_RE.match(text, i)
                if m and links:
                    out.append(put(self._link(m.group(1), esc(m.group(1)))))
                    i = m.end()
                    continue
            if c == "!" and text.startswith("![", i):
                close = _find_bracket_close(text, i + 1)
                if close > 0 and close + 1 < n and text[close + 1] == "(":
                    dest = _parse_destination(text, close + 1)
                    if dest:
                        # No images: the page loads nothing. Show the alt text.
                        alt = self.render(text[i + 2:close], links=False)
                        out.append(put(f'<span class="img-alt">{alt}</span>'))
                        i = dest[1]
                        continue
            if c == "[" and links:
                close = _find_bracket_close(text, i)
                if close > 0 and close + 1 < n and text[close + 1] == "(":
                    dest = _parse_destination(text, close + 1)
                    if dest:
                        label = self.render(text[i + 1:close], links=False)
                        out.append(put(self._link(dest[0], label)))
                        i = dest[1]
                        continue
            if c in "hH" and links and (i == 0 or not text[i - 1].isalnum()):
                m = _BARE_URL_RE.match(text, i)
                if m:
                    url = _trim_bare_url(m.group(0))
                    if len(url) > len("https://"):
                        out.append(put(self._link(url, esc(url))))
                        i += len(url)
                        continue
            out.append(c)
            i += 1

        s = esc("".join(out))
        s = _STRONG_STAR.sub(r"<strong>\1</strong>", s)
        s = _STRONG_UNDER.sub(r"<strong>\1</strong>", s)
        s = _EM_STAR.sub(r"<em>\1</em>", s)
        s = _EM_UNDER.sub(r"<em>\1</em>", s)
        s = re.sub(r" {2,}\n", "<br>\n", s)
        return _PH_RE.sub(lambda m: stash[int(m.group(1))], s)

    def _link(self, url: str, label_html: str) -> str:
        target = self.resolve(url.strip())
        if target is None:
            return f'<span class="dead-link">{label_html}</span>'
        href, external = target
        if external:
            return (f'<a href="{esc(href)}" target="_blank" rel="noopener noreferrer"'
                    f' class="ext">{label_html}</a>')
        return f'<a href="{esc(href)}">{label_html}</a>'


# --------------------------------------------------------------------------
# Block markdown

_FENCE_RE = re.compile(r"^( {0,3})(`{3,}|~{3,})(.*)$")
_ATX_RE = re.compile(r"^ {0,3}(#{1,6})(?:[ \t]+(.*?))?(?:[ \t]+#+)?[ \t]*$")
_HR_RE = re.compile(r"^ {0,3}([-*_])(?:[ \t]*\1){2,}[ \t]*$")
_BQ_RE = re.compile(r"^ {0,3}> ?(.*)$")
_BULLET_RE = re.compile(r"^( {0,3})([-*+])( +|$)(.*)$")
_ORDERED_RE = re.compile(r"^( {0,3})(\d{1,9})([.)])( +|$)(.*)$")
_DELIM_CELL_RE = re.compile(r"^\s*:?-+:?\s*$")
_CALLOUT_RE = re.compile(r"^\*\*(Note|Tip|Warning|Important)(?::\*\*|\*\*:)", re.IGNORECASE)

COMMAND_LANGS = {"bash", "sh", "shell", "zsh", "powershell", "ps1", "pwsh"}
OUTPUT_LANGS = {"", "text", "console", "output", "plain", "plaintext"}
LANG_LABELS = {"powershell": "PowerShell", "ps1": "PowerShell", "pwsh": "PowerShell",
               "console": "Output", "output": "Output", "conf": "Config", "ini": "INI",
               "toml": "TOML", "yaml": "YAML", "yml": "YAML", "json": "JSON",
               "markdown": "Markdown", "md": "Markdown", "python": "Python", "py": "Python",
               "diff": "Diff", "typescript": "TypeScript", "ts": "TypeScript",
               "javascript": "JavaScript", "js": "JavaScript"}


@dataclass
class ListMarker:
    ordered: bool
    char: str          # bullet char, or the ordered delimiter
    start: int
    indent: int        # columns before the marker
    content: int       # column where the item's content starts
    first: str         # the item's first line, minus marker


def match_list_item(line: str):
    m = _BULLET_RE.match(line)
    ordered = False
    if not m:
        m = _ORDERED_RE.match(line)
        ordered = True
        if not m:
            return None
        indent, num, char, spaces, rest = m.group(1), m.group(2), m.group(3), m.group(4), m.group(5)
        marker_len = len(num) + 1
    else:
        indent, char, spaces, rest = m.group(1), m.group(2), m.group(3), m.group(4)
        num = "1"
        marker_len = 1
    if not rest.strip():
        spaces, rest = " ", ""
    elif len(spaces) > 4:  # an indented code block in the item: content starts one column in
        rest = spaces[1:] + rest
        spaces = " "
    return ListMarker(ordered, char, int(num), len(indent), len(indent) + marker_len + len(spaces), rest)


def split_row(line: str) -> list[str]:
    """Split a table row on unescaped pipes; `\\|` becomes a literal pipe in
    the cell (code spans included, as in GFM)."""
    s = line.strip()
    if s.startswith("|"):
        s = s[1:]
    cells, cur, i = [], [], 0
    while i < len(s):
        c = s[i]
        if c == "\\" and i + 1 < len(s) and s[i + 1] == "|":
            cur.append("|")
            i += 2
            continue
        if c == "|":
            cells.append("".join(cur))
            cur = []
        else:
            cur.append(c)
        i += 1
    tail = "".join(cur)
    if tail.strip() or not s.rstrip().endswith("|"):
        cells.append(tail)
    return [c.strip() for c in cells]


def is_delimiter_row(line: str) -> bool:
    if "|" not in line or "-" not in line:
        return False
    cells = split_row(line)
    return bool(cells) and all(_DELIM_CELL_RE.match(c) for c in cells)


def _shell_line_html(line: str) -> str:
    """One line of a command block: dim `$ ` prompts and `#` comments."""
    prefix = ""
    m = re.match(r"^(\s*)\$ (.*)$", line)
    if m:
        prefix = f'{esc(m.group(1))}<span class="prompt" aria-hidden="true">$ </span>'
        line = m.group(2)
    if line.lstrip().startswith("#"):
        return f'{prefix}<span class="c">{esc(line)}</span>'
    quote = None
    for idx, ch in enumerate(line):
        if quote:
            if ch == quote:
                quote = None
        elif ch in "\"'":
            quote = ch
        elif ch == "#" and idx > 0 and line[idx - 1] in " \t":
            return f'{prefix}{esc(line[:idx])}<span class="c">{esc(line[idx:])}</span>'
    return prefix + esc(line)


_COPY_ICON = ('<svg aria-hidden="true" viewBox="0 0 16 16" width="14" height="14"><rect x="5" y="5" width="9" '
              'height="9" rx="2" fill="none" stroke="currentColor" stroke-width="1.4"/><path d="M11 5V3.5A1.5 1.5 '
              '0 0 0 9.5 2h-6A1.5 1.5 0 0 0 2 3.5v6A1.5 1.5 0 0 0 3.5 11H5" fill="none" stroke="currentColor" '
              'stroke-width="1.4"/></svg>')


def copy_button(what: str) -> str:
    return (f'<button type="button" class="copy" aria-label="Copy {what}">{_COPY_ICON}'
            f'<span class="copy-label">Copy</span></button>')


def render_code(lang: str, body: list[str]) -> str:
    text = "\n".join(body)
    if lang == "prompt":
        return ('<figure class="prompt"><figcaption><span class="who">You</span>'
                '<span class="arrow" aria-hidden="true">→</span><span class="to">Coordinator</span>'
                f'{copy_button("prompt")}</figcaption>'
                f'<div class="prompt-text" data-copy-src>{esc(text)}</div></figure>')
    if lang in COMMAND_LANGS:
        label = LANG_LABELS.get(lang, "Terminal")
        code = "\n".join(_shell_line_html(line) for line in body)
        return (f'<div class="code cmd"><div class="code-head"><span class="lang">{esc(label)}</span>'
                f'{copy_button("commands")}</div><pre tabindex="0"><code data-copy-src>{code}</code></pre></div>')
    if lang in OUTPUT_LANGS:
        label = LANG_LABELS.get(lang, "")
        kind = "out"
    else:
        label = LANG_LABELS.get(lang, lang)
        kind = "file"
    head = (f'<div class="code-head"><span class="lang">{esc(label)}</span>{copy_button("text")}</div>'
            if label else copy_button("text"))
    cls = f"code {kind}" + ("" if label else " bare")
    return f'<div class="{cls}">{head}<pre tabindex="0"><code data-copy-src>{esc(text)}</code></pre></div>'


@dataclass
class Block:
    html: str
    level: int = 0          # heading level in the source, 0 for other blocks
    id: str = ""
    plain: str = ""
    inner: str = ""         # a heading's rendered inline content


class DocRenderer:
    """Renders one markdown document. Heading ids are `<key>:<slug>`, so each
    document keeps its own GitHub-style anchors on the shared page."""

    def __init__(self, site: "Site", key: str, path: Path, level_shift: int = 0):
        self.site = site
        self.key = key
        self.path = path
        self.level_shift = level_shift
        self.slugs: dict[str, int] = {}
        self.used: set[str] = set()
        self.ids: list[str] = []
        self.inline = InlineRenderer(self.resolve_link)

    # -- links

    def resolve_link(self, url: str):
        if not url or any(ch.isspace() or ord(ch) < 32 or ord(ch) == 127 for ch in url):
            return None
        low = url.lower()
        if low.startswith(("http://", "https://", "mailto:")):
            return url, True
        if url.startswith("#"):
            return self.site.anchor_href(self.key, url[1:], self), False
        if _SCHEME_RE.match(url) or url.startswith(("//", "\\")):
            return None  # javascript:, data:, file:, protocol-relative, ...
        path_part, _, frag = url.partition("#")
        path_part = unquote(path_part.split("?", 1)[0])
        if "\x00" in path_part:
            return None
        root = self.site.root
        base = root if path_part.startswith("/") else self.path.parent
        target = (base / path_part.lstrip("/")).resolve() if path_part else self.path.resolve()
        try:
            target.relative_to(root)
        except ValueError:
            return None
        key = self.site.doc_keys.get(target)
        if key is not None:
            if frag:
                return self.site.anchor_href(key, frag, self), False
            return "#" + self.site.doc_routes[key], False
        if not target.exists():
            return None
        if self.site.link_base:
            return self.site.link_base + quote(target.relative_to(root).as_posix()), True
        return target.as_uri(), False

    # -- blocks

    def heading_id(self, plain: str) -> str:
        base = slugify(plain) or "section"
        slug, n = base, self.slugs.get(base, 0)
        while slug in self.used:
            n += 1
            slug = f"{base}-{n}"
        self.slugs[base] = n
        self.used.add(slug)
        hid = f"{self.key}:{slug}"
        self.ids.append(hid)
        return hid

    def heading_html(self, level: int, hid: str, inner: str, plain: str) -> str:
        h = max(1, min(6, level + self.level_shift))
        return (f'<h{h} id="{esc(hid)}" tabindex="-1">{inner}'
                f'<a class="anchor" href="#{esc(hid)}" aria-label="Link to {esc(plain)}"></a></h{h}>')

    def render(self, text: str) -> list[Block]:
        text = text.lstrip("﻿").replace("\r\n", "\n").replace("\r", "\n")
        lines = [line.expandtabs(4).rstrip("\n") for line in text.split("\n")]
        return self.blocks(lines)

    def render_html(self, lines: list[str]) -> str:
        return "\n".join(b.html for b in self.blocks(lines))

    def starts_block(self, line: str) -> bool:
        """Does this line interrupt a paragraph?"""
        if _FENCE_RE.match(line) and not (line.lstrip().startswith("`") and "`" in _FENCE_RE.match(line).group(3)):
            return True
        if _ATX_RE.match(line) or _HR_RE.match(line) or _BQ_RE.match(line):
            return True
        mk = match_list_item(line)
        return bool(mk and mk.first.strip() and (not mk.ordered or mk.start == 1))

    def blocks(self, lines: list[str]) -> list[Block]:
        out: list[Block] = []
        i, n = 0, len(lines)
        while i < n:
            line = lines[i]
            if not line.strip():
                i += 1
                continue

            m = _FENCE_RE.match(line)
            if m and not (m.group(2)[0] == "`" and "`" in m.group(3)):
                indent, fence = len(m.group(1)), m.group(2)
                info = m.group(3).strip().split()
                lang = info[0].lower() if info else ""
                close_re = re.compile(r"^ {0,3}" + re.escape(fence[0]) + "{" + str(len(fence)) + r",}\s*$")
                body, j = [], i + 1
                while j < n and not close_re.match(lines[j]):
                    raw = lines[j]
                    strip = min(indent, len(raw) - len(raw.lstrip(" ")))
                    body.append(raw[strip:])
                    j += 1
                out.append(Block(render_code(lang, body)))
                i = j + 1
                continue

            m = _ATX_RE.match(line)
            if m:
                level = len(m.group(1))
                inner = self.inline.render((m.group(2) or "").strip())
                plain = plain_text(inner)
                hid = self.heading_id(plain)
                out.append(Block(self.heading_html(level, hid, inner, plain), level, hid, plain, inner))
                i += 1
                continue

            if _HR_RE.match(line):
                out.append(Block("<hr>"))
                i += 1
                continue

            if _BQ_RE.match(line):
                quoted, j = [], i
                while j < n:
                    bm = _BQ_RE.match(lines[j])
                    if bm:
                        quoted.append(bm.group(1))
                    elif (lines[j].strip() and quoted and quoted[-1].strip()
                          and not self.starts_block(lines[j])):
                        quoted.append(lines[j])  # lazy continuation
                    else:
                        break
                    j += 1
                first = next((q.strip() for q in quoted if q.strip()), "")
                cm = _CALLOUT_RE.match(first)
                kind = cm.group(1).lower() if cm else "plain"
                inner = self.render_html(quoted)
                role = ' role="note"' if cm else ""
                out.append(Block(f'<div class="callout callout-{kind}"{role}>{inner}</div>'))
                i = j
                continue

            mk = match_list_item(line)
            if mk:
                html_, i = self.list_block(lines, i, mk)
                out.append(Block(html_))
                continue

            if "|" in line and i + 1 < n and is_delimiter_row(lines[i + 1]):
                header = split_row(line)
                delims = split_row(lines[i + 1])
                if len(header) == len(delims):
                    html_, i = self.table_block(lines, i, header, delims)
                    out.append(Block(html_))
                    continue

            para, j = [line.strip()], i + 1
            while j < n and lines[j].strip() and not self.starts_block(lines[j]):
                if "|" in lines[j] and j + 1 < n and is_delimiter_row(lines[j + 1]):
                    break
                para.append(lines[j].lstrip())
                j += 1
            text = "\n".join(para).rstrip()
            out.append(Block(f"<p>{self.inline.render(text)}</p>"))
            i = j
        return out

    def list_block(self, lines: list[str], i: int, first: ListMarker):
        n = len(lines)
        items: list[list[str]] = []
        loose = False
        mk = first
        while True:
            ci = mk.content
            item = [mk.first]
            fence = None

            def track(content: str, fence):
                fm = _FENCE_RE.match(content)
                if fence is None:
                    if fm and not (fm.group(2)[0] == "`" and "`" in fm.group(3)):
                        return (fm.group(2)[0], len(fm.group(2))), False
                    stripped = content.strip()
                    para = bool(stripped) and not stripped.startswith(("|", "#")) and not _HR_RE.match(content)
                    return None, para
                if re.match(r"^ {0,3}" + re.escape(fence[0]) + "{" + str(fence[1]) + r",}\s*$", content):
                    return None, False
                return fence, False

            fence, para = track(mk.first, None)
            j = i + 1
            while j < n:
                line = lines[j]
                if not line.strip():
                    item.append("")
                    para = False
                    j += 1
                    continue
                ind = len(line) - len(line.lstrip(" "))
                if ind >= ci:
                    content = line[ci:]
                    item.append(content)
                    fence, para = track(content, fence)
                    j += 1
                    continue
                if fence is None and para and not self.starts_block(line) and not match_list_item(line):
                    item.append(line.strip())  # lazy continuation of the item's paragraph
                    j += 1
                    continue
                break
            trailing = 0
            while item and not item[-1].strip():
                item.pop()
                trailing += 1
            # A blank line between two direct children of the item makes the list loose.
            in_fence = None
            for k, content in enumerate(item):
                if in_fence is None and not content.strip() and k + 1 < len(item):
                    nxt = item[k + 1]
                    if nxt.strip() and len(nxt) - len(nxt.lstrip(" ")) == 0:
                        loose = True
                fm = _FENCE_RE.match(content)
                if fm and in_fence is None:
                    in_fence = fm.group(2)
                elif in_fence and content.strip().startswith(in_fence):
                    in_fence = None
            items.append(item)
            i = j
            nxt = match_list_item(lines[i]) if i < n else None
            if not nxt or nxt.ordered != first.ordered or nxt.char != first.char:
                break
            if trailing:
                loose = True
            mk = nxt
        tag = "ol" if first.ordered else "ul"
        attrs = f' start="{first.start}"' if first.ordered and first.start != 1 else ""
        cls = "" if loose else ' class="tight"'
        body = "".join(f"<li>{self.render_html(item)}</li>" for item in items)
        return f"<{tag}{attrs}{cls}>{body}</{tag}>", i

    def table_block(self, lines: list[str], i: int, header: list[str], delims: list[str]):
        aligns = []
        for d in delims:
            d = d.strip()
            if d.startswith(":") and d.endswith(":"):
                aligns.append("center")
            elif d.endswith(":"):
                aligns.append("right")
            elif d.startswith(":"):
                aligns.append("left")
            else:
                aligns.append("")
        width = len(header)

        def cell(tag: str, text: str, k: int) -> str:
            a = f' class="al-{aligns[k]}"' if aligns[k] else ""
            return f"<{tag}{a}>{self.inline.render(text)}</{tag}>"

        head = "".join(cell("th", h, k) for k, h in enumerate(header))
        rows = []
        j, n = i + 2, len(lines)
        while j < n and lines[j].strip() and "|" in lines[j]:
            if _FENCE_RE.match(lines[j]) or _ATX_RE.match(lines[j]) or _BQ_RE.match(lines[j]):
                break
            cells = (split_row(lines[j]) + [""] * width)[:width]
            rows.append("<tr>" + "".join(cell("td", c, k) for k, c in enumerate(cells)) + "</tr>")
            j += 1
        label = esc(plain_text(self.inline.render(header[0])) if header else "")
        return (f'<div class="table-wrap" role="region" tabindex="0" aria-label="Table: {label}">'
                f'<table><thead><tr>{head}</tr></thead><tbody>{"".join(rows)}</tbody></table></div>'), j


# --------------------------------------------------------------------------
# Site: documents -> pages

@dataclass
class Page:
    route: str               # element id of the page section, e.g. "/guide/first-session"
    doc: str                 # document key (anchor prefix)
    group: str
    title_html: str
    title_plain: str
    nav_label: str
    body: str = ""           # rendered blocks after the title
    title_id: str = ""
    subheads: list = field(default_factory=list)   # (id, plain, depth) below the title
    summary: str = ""


@dataclass
class Doc:
    key: str
    path: Path
    kind: str                # guide | readme | wsl
    route: str               # route of the doc's first page
    slug: str = ""


class Site:
    def __init__(self, root: Path, link_base: str | None = None):
        self.root = root.resolve()
        # Published site (e.g. GitHub Pages): links to repo files go to
        # link_base + their repo path instead of file:// into the checkout.
        self.link_base = link_base
        self.docs: list[Doc] = []
        self.doc_keys: dict[Path, str] = {}
        self.doc_routes: dict[str, str] = {}
        self.known_ids: dict[str, set] | None = None
        self.warnings: list[str] = []
        self.pages: list[Page] = []

    def anchor_href(self, key: str, anchor: str, renderer: DocRenderer) -> str:
        anchor = unquote(anchor)
        if self.known_ids is None:
            return f"#{key}:{anchor}"
        ids = self.known_ids.get(key, set())
        for cand in (anchor, anchor.lower(), slugify(anchor)):
            if f"{key}:{cand}" in ids:
                return f"#{key}:{cand}"
        self.warnings.append(f"{renderer.path.relative_to(self.root)}: no heading for #{anchor} in {key}")
        return "#" + self.doc_routes.get(key, "/")

    def discover(self) -> None:
        readme = self.root / "README.md"
        if not readme.is_file():
            raise DocsError(f"README.md not found in {self.root}")
        guides_dir = self.root / "docs" / "guides"
        used = set()
        if guides_dir.is_dir():
            for path in sorted(guides_dir.glob("*.md")):
                if not path.is_file():
                    continue
                path = path.resolve()
                try:
                    path.relative_to(self.root)
                except ValueError:
                    continue
                slug = slugify(re.sub(r"^\d+[-_ .]*", "", path.stem).replace("_", "-")) or "guide"
                base, k = slug, 1
                while slug in used:
                    slug = f"{base}-{k}"
                    k += 1
                used.add(slug)
                self._add(Doc(f"guide-{slug}", path, "guide", f"/guide/{slug}", slug))
        self._add(Doc("readme", readme.resolve(), "readme", "/ref/overview"))
        wsl = self.root / "docs" / "setup-wsl.md"
        if wsl.is_file():
            self._add(Doc("wsl", wsl.resolve(), "wsl", "/setup/wsl"))

    def _add(self, doc: Doc) -> None:
        self.docs.append(doc)
        self.doc_keys[doc.path] = doc.key
        self.doc_routes[doc.key] = doc.route

    def _read(self, path: Path) -> str:
        try:
            return path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as e:
            raise DocsError(f"cannot read {path}: {e}")

    def _render_doc(self, doc: Doc) -> list[Block]:
        shift = -1 if doc.kind == "readme" else 0
        r = DocRenderer(self, doc.key, doc.path, shift)
        blocks = r.render(self._read(doc.path))
        return blocks, r

    def build(self) -> None:
        self.discover()
        # Pass 1 collects every document's heading ids so pass 2 can check anchors.
        known = {}
        for doc in self.docs:
            _, r = self._render_doc(doc)
            known[doc.key] = set(r.ids)
        self.known_ids = known
        guides, refs, setup = [], [], []
        for doc in self.docs:
            blocks, _ = self._render_doc(doc)
            if doc.kind == "readme":
                refs.extend(self._split_readme(doc, blocks))
            else:
                page = self._single_page(doc, blocks, "Guides" if doc.kind == "guide" else "Platform setup")
                (guides if doc.kind == "guide" else setup).append(page)
        self.pages = guides + refs + setup

    def _single_page(self, doc: Doc, blocks: list[Block], group: str) -> Page:
        title_block = blocks[0] if blocks and blocks[0].level == 1 else None
        rest = blocks[1:] if title_block else blocks
        if title_block:
            title_html, title_plain, title_id = title_block.inner, title_block.plain, title_block.id
        else:
            title_plain = doc.path.stem.replace("-", " ").strip().capitalize()
            title_html, title_id = esc(title_plain), ""
        summary = ""
        if rest and rest[0].html.startswith("<p>") and doc.kind == "guide":
            summary = plain_text(rest[0].html)
            rest = [Block(rest[0].html.replace("<p>", '<p class="lede">', 1))] + rest[1:]
        page = Page(doc.route, doc.key, group, title_html, title_plain, title_plain,
                    title_id=title_id, summary=summary)
        page.body = "\n".join(b.html for b in rest)
        page.subheads = [(b.id, b.plain, b.level - 2) for b in rest if b.level in (2, 3)]
        return page

    def _split_readme(self, doc: Doc, blocks: list[Block]) -> list[Page]:
        pages: list[Page] = []
        intro: list[Block] = []
        title_id = ""
        k = 0
        while k < len(blocks) and blocks[k].level != 2:
            if blocks[k].level == 1 and not title_id:
                title_id = blocks[k].id
            else:
                intro.append(blocks[k])
            k += 1
        if intro or title_id:
            p = Page("/ref/overview", doc.key, "Reference", "Overview", "Overview", "Overview",
                     title_id=title_id)
            p.body = "\n".join(b.html for b in intro)
            p.subheads = [(b.id, b.plain, 0) for b in intro if b.level == 3]
            pages.append(p)
        used = {"overview"}
        while k < len(blocks):
            head = blocks[k]
            k += 1
            body = []
            while k < len(blocks) and blocks[k].level != 2:
                body.append(blocks[k])
                k += 1
            slug = head.id.split(":", 1)[1]
            base, n = slug, 1
            while slug in used:
                slug = f"{base}-{n}"
                n += 1
            used.add(slug)
            p = Page(f"/ref/{slug}", doc.key, "Reference", head.inner, head.plain, head.plain,
                     title_id=head.id)
            p.body = "\n".join(b.html for b in body)
            p.subheads = [(b.id, b.plain, 0) for b in body if b.level == 3]
            pages.append(p)
        if pages:
            self.doc_routes[doc.key] = pages[0].route
        return pages


# --------------------------------------------------------------------------
# Page assembly

CSS = r"""
:root {
  --bg: #f5f7f8; --surface: #ffffff; --surface-2: #eef2f4; --fg: #17212b; --muted: #556472;
  --line: #d5dde3; --line-soft: #e4e9ed; --accent: #0d6e7a; --accent-strong: #0a5761; --accent-soft: #e0f0f1;
  --chip: #e8edf0; --code-bg: #142028; --code-fg: #d9e6ec; --code-dim: #7f95a1; --code-line: #26353f;
  --warn: #8a5a00; --warn-soft: #fbf1dc; --ok: #1d7a45; --ok-soft: #e2f3e8; --imp: #a03a2c; --imp-soft: #f9e7e3;
  --shadow: 0 1px 2px rgba(16, 32, 40, .06), 0 10px 28px rgba(16, 32, 40, .10);
  --font-ui: system-ui, -apple-system, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif;
  --font-mono: ui-monospace, "SF Mono", Menlo, Consolas, monospace;
  --top: 56px; --side: 272px;
  color-scheme: light;
}
@media (prefers-color-scheme: dark) {
  :root:not([data-theme="light"]) {
    --bg: #0f161b; --surface: #16212a; --surface-2: #1a2731; --fg: #e3eaef; --muted: #98a8b4;
    --line: #2a3843; --line-soft: #22303a; --accent: #5cc3cf; --accent-strong: #86d6df; --accent-soft: #163339;
    --chip: #1e2b35; --code-bg: #0a1014; --code-fg: #d9e6ec; --code-dim: #7f95a1; --code-line: #1c2a33;
    --warn: #f0c060; --warn-soft: #2e2512; --ok: #6fd39a; --ok-soft: #142d1f; --imp: #f29a86; --imp-soft: #331b18;
    --shadow: 0 1px 2px rgba(0, 0, 0, .3), 0 12px 32px rgba(0, 0, 0, .45);
    color-scheme: dark;
  }
}
:root[data-theme="dark"] {
  --bg: #0f161b; --surface: #16212a; --surface-2: #1a2731; --fg: #e3eaef; --muted: #98a8b4;
  --line: #2a3843; --line-soft: #22303a; --accent: #5cc3cf; --accent-strong: #86d6df; --accent-soft: #163339;
  --chip: #1e2b35; --code-bg: #0a1014; --code-fg: #d9e6ec; --code-dim: #7f95a1; --code-line: #1c2a33;
  --warn: #f0c060; --warn-soft: #2e2512; --ok: #6fd39a; --ok-soft: #142d1f; --imp: #f29a86; --imp-soft: #331b18;
  --shadow: 0 1px 2px rgba(0, 0, 0, .3), 0 12px 32px rgba(0, 0, 0, .45);
  color-scheme: dark;
}

* { box-sizing: border-box; }
html { -webkit-text-size-adjust: 100%; scroll-padding-top: calc(var(--top) + 16px); }
body {
  margin: 0; background: var(--bg); color: var(--fg);
  font-family: var(--font-ui); font-size: 16px; line-height: 1.65;
  -webkit-font-smoothing: antialiased; text-rendering: optimizeLegibility;
}
a { color: var(--accent); text-decoration: underline; text-decoration-thickness: 1px;
    text-underline-offset: 2px; text-decoration-color: color-mix(in srgb, var(--accent) 40%, transparent); }
a:hover { color: var(--accent-strong); text-decoration-color: currentColor; }
:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; border-radius: 4px; }
[hidden] { display: none !important; }
.sr-only { position: absolute; width: 1px; height: 1px; overflow: hidden; clip: rect(0 0 0 0); white-space: nowrap; }
.skip { position: absolute; left: 12px; top: -48px; z-index: 50; background: var(--surface); color: var(--fg);
        padding: 8px 12px; border-radius: 8px; box-shadow: var(--shadow); }
.skip:focus { top: 10px; }

/* Top bar */
.topbar {
  position: sticky; top: 0; z-index: 30; height: var(--top);
  display: flex; align-items: center; gap: 12px; padding: 0 16px;
  background: var(--bg); border-bottom: 1px solid var(--line-soft);
}
.brand { display: flex; align-items: center; gap: 10px; color: var(--fg); text-decoration: none;
         font-weight: 650; letter-spacing: -0.01em; white-space: nowrap; min-width: 0; }
.brand:hover { color: var(--fg); }
.brand-mark { width: 22px; height: 22px; border-radius: 6px; border: 2px solid var(--accent); position: relative; flex: none; }
.brand-mark::after { content: ""; position: absolute; inset: 4px; border-radius: 2px; background: var(--accent); }
.brand small { color: var(--muted); font-weight: 500; }
.search { position: relative; flex: 1 1 auto; max-width: 440px; margin-left: auto; min-width: 0; }
.search input {
  width: 100%; height: 36px; padding: 0 34px 0 34px; border-radius: 9px;
  border: 1px solid var(--line); background: var(--surface); color: var(--fg);
  font: inherit; font-size: 0.92rem; -webkit-appearance: none; appearance: none;
}
.search input::placeholder { color: var(--muted); }
.search input:focus { outline: none; border-color: var(--accent); box-shadow: 0 0 0 3px var(--accent-soft); }
.search input::-webkit-search-cancel-button { display: none; }
.search .glass { position: absolute; left: 11px; top: 11px; color: var(--muted); pointer-events: none; }
.search kbd { position: absolute; right: 8px; top: 7px; font-family: var(--font-mono); font-size: 0.75rem;
              color: var(--muted); border: 1px solid var(--line); border-radius: 5px; padding: 0 6px; line-height: 20px; }
.search input:focus ~ kbd { display: none; }
.results {
  position: absolute; top: 44px; left: 0; right: 0; min-width: min(520px, calc(100vw - 32px));
  max-height: min(70vh, 560px); overflow: auto; margin: 0; padding: 6px; list-style: none;
  background: var(--surface); border: 1px solid var(--line); border-radius: 12px; box-shadow: var(--shadow);
}
.results li a { display: block; padding: 8px 10px; border-radius: 8px; color: var(--fg); text-decoration: none; }
.results li[aria-selected="true"] a, .results li a:hover { background: var(--accent-soft); }
.results .r-head { font-weight: 600; font-size: 0.92rem; }
.results .r-page { color: var(--muted); font-size: 0.78rem; margin-left: 8px; font-weight: 400; }
.results .r-snip { display: block; color: var(--muted); font-size: 0.84rem; line-height: 1.45; margin-top: 2px;
                   overflow: hidden; display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical; }
.results mark { background: none; color: var(--accent); font-weight: 600; }
.results .empty { padding: 10px; color: var(--muted); font-size: 0.9rem; }
.icon-btn {
  flex: none; display: inline-grid; place-items: center; width: 36px; height: 36px; border-radius: 9px;
  border: 1px solid var(--line); background: var(--surface); color: var(--muted); cursor: pointer; padding: 0;
}
.icon-btn:hover { color: var(--fg); border-color: var(--muted); }
.menu-btn { display: none; }
.theme-btn .i-light, .theme-btn .i-dark { display: none; }
[data-theme-choice="light"] .theme-btn .i-system, [data-theme-choice="dark"] .theme-btn .i-system { display: none; }
[data-theme-choice="light"] .theme-btn .i-light { display: block; }
[data-theme-choice="dark"] .theme-btn .i-dark { display: block; }

/* Layout */
.layout { display: grid; grid-template-columns: var(--side) minmax(0, 1fr); max-width: 1400px; margin: 0 auto; }
.sidebar {
  position: sticky; top: var(--top); height: calc(100vh - var(--top)); overflow-y: auto;
  padding: 24px 12px 40px 16px; border-right: 1px solid var(--line-soft); background: var(--bg);
  overscroll-behavior: contain;
}
.nav-group { font-size: 0.72rem; font-weight: 650; letter-spacing: 0.08em; text-transform: uppercase;
             color: var(--muted); margin: 22px 10px 6px; }
.nav-group:first-child { margin-top: 0; }
.sidebar ul { list-style: none; margin: 0; padding: 0; }
.sidebar a { display: block; padding: 5px 10px; border-radius: 7px; color: var(--fg); text-decoration: none;
             font-size: 0.92rem; line-height: 1.4; }
.sidebar a:hover { background: var(--surface-2); }
.sidebar a[aria-current="page"] { background: var(--accent-soft); color: var(--accent); font-weight: 600; }
.sidebar ul ul { margin: 2px 0 6px 12px; padding-left: 8px; border-left: 1px solid var(--line); }
.sidebar ul ul a { font-size: 0.86rem; color: var(--muted); padding: 3px 10px; }
.sidebar ul ul a:hover { color: var(--fg); }
.js .sidebar li.has-sub:not(.open) > ul { display: none; }
.sidebar .file-link::after { content: " ↗"; color: var(--muted); font-size: 0.8em; }
.sidebar .num { display: inline-block; min-width: 1.4em; color: var(--muted); font-variant-numeric: tabular-nums; }
.sidebar a[aria-current="page"] .num { color: inherit; }
.backdrop { display: none; }

main { min-width: 0; padding: 40px 48px 24px; }
main:focus { outline: none; }
.page-grid { display: grid; grid-template-columns: minmax(0, 760px) 220px; gap: 56px; }
.doc { min-width: 0; }
.toc { position: sticky; top: calc(var(--top) + 32px); align-self: start; max-height: calc(100vh - var(--top) - 64px);
       overflow-y: auto; font-size: 0.84rem; }
.toc h2 { font-size: 0.72rem; font-weight: 650; letter-spacing: 0.08em; text-transform: uppercase; color: var(--muted); margin: 0 0 8px; }
.toc ul { list-style: none; margin: 0; padding: 0; border-left: 1px solid var(--line); }
.toc ul ul { border-left: none; }
.toc ul ul a { padding-left: 24px; font-size: 0.8rem; }
.toc a { display: block; padding: 4px 12px; margin-left: -1px; border-left: 2px solid transparent; color: var(--muted);
         text-decoration: none; line-height: 1.4; }
.toc a:hover { color: var(--fg); }
.toc a.active { color: var(--accent); border-left-color: var(--accent); }

/* Content */
.doc h1, .doc h2, .doc h3, .doc h4 { position: relative; line-height: 1.25; letter-spacing: -0.015em;
  text-wrap: balance; scroll-margin-top: calc(var(--top) + 20px); }
.doc h1 { font-size: 2.1rem; font-weight: 700; margin: 0 0 16px; }
.doc h2 { font-size: 1.45rem; font-weight: 650; margin: 48px 0 12px; padding-top: 24px; border-top: 1px solid var(--line-soft); }
.doc h3 { font-size: 1.15rem; font-weight: 650; margin: 32px 0 8px; }
.doc h4, .doc h5, .doc h6 { font-size: 1rem; font-weight: 650; margin: 24px 0 6px; }
.doc h1:focus, .doc h2:focus, .doc h3:focus, .doc h4:focus { outline: none; }
.anchor { margin-left: 8px; color: var(--muted); text-decoration: none; opacity: 0; font-weight: 400; }
.anchor::before { content: "#"; }
.doc :is(h1, h2, h3, h4):hover .anchor, .anchor:focus-visible { opacity: 1; }
.anchor:hover { color: var(--accent); }
.doc p, .doc ul, .doc ol, .doc .table-wrap, .doc .code, .doc .prompt, .doc .callout { margin: 0 0 18px; }
.doc .lede { font-size: 1.12rem; color: var(--muted); line-height: 1.6; max-width: 68ch; }
.doc ul, .doc ol { padding-left: 1.5em; }
.doc li { margin: 6px 0; }
.doc li::marker { color: var(--muted); }
.doc li > :last-child { margin-bottom: 0; }
.doc ul.tight > li > p, .doc ol.tight > li > p { margin: 0; }
.doc li > ul, .doc li > ol { margin-top: 6px; }
.doc strong { font-weight: 650; }
.doc hr { border: none; border-top: 1px solid var(--line); margin: 32px 0; }
.doc code { font-family: var(--font-mono); font-size: 0.86em; background: var(--chip); padding: 0.12em 0.38em;
            border-radius: 5px; overflow-wrap: anywhere; }
.doc a code { color: inherit; }
.dead-link { border-bottom: 1px dotted var(--muted); }
.img-alt { font-style: italic; color: var(--muted); }

/* Callouts */
.callout { border: 1px solid var(--line); border-left: 3px solid var(--muted); background: var(--surface);
           border-radius: 4px 10px 10px 4px; padding: 12px 16px; }
.callout > :last-child { margin-bottom: 0; }
.callout > * { margin-bottom: 10px; }
.callout-note { border-color: color-mix(in srgb, var(--accent) 25%, var(--line)); border-left-color: var(--accent); background: var(--accent-soft); }
.callout-note > p:first-child > strong:first-child { color: var(--accent); }
.callout-tip { border-color: color-mix(in srgb, var(--ok) 25%, var(--line)); border-left-color: var(--ok); background: var(--ok-soft); }
.callout-tip > p:first-child > strong:first-child { color: var(--ok); }
.callout-warning { border-color: color-mix(in srgb, var(--warn) 25%, var(--line)); border-left-color: var(--warn); background: var(--warn-soft); }
.callout-warning > p:first-child > strong:first-child { color: var(--warn); }
.callout-important { border-color: color-mix(in srgb, var(--imp) 25%, var(--line)); border-left-color: var(--imp); background: var(--imp-soft); }
.callout-important > p:first-child > strong:first-child { color: var(--imp); }
.callout code { background: color-mix(in srgb, var(--chip) 70%, transparent); }

/* Tables */
.table-wrap { overflow-x: auto; border: 1px solid var(--line); border-radius: 10px; background: var(--surface); }
.doc table { width: 100%; border-collapse: collapse; font-size: 0.92rem; line-height: 1.5; }
.doc th, .doc td { text-align: left; padding: 9px 14px; border-bottom: 1px solid var(--line-soft); vertical-align: top; }
.doc th { font-size: 0.8rem; font-weight: 650; color: var(--muted); background: var(--surface-2);
          border-bottom: 1px solid var(--line); white-space: nowrap; }
.doc tbody tr:last-child td { border-bottom: none; }
.doc tbody tr:hover td { background: color-mix(in srgb, var(--surface-2) 55%, transparent); }
.doc td:first-child code { white-space: nowrap; }
.doc .al-center { text-align: center; } .doc .al-right { text-align: right; } .doc .al-left { text-align: left; }

/* Code */
.code { border-radius: 10px; overflow: hidden; position: relative; min-width: 0; }
.code pre { margin: 0; padding: 14px 16px; overflow-x: auto; font-family: var(--font-mono); font-size: 0.84rem;
            line-height: 1.6; tab-size: 4; }
.code pre code { font: inherit; background: none; padding: 0; border-radius: 0; overflow-wrap: normal; white-space: pre; }
.code-head { display: flex; align-items: center; justify-content: space-between; gap: 8px;
             padding: 6px 8px 6px 14px; font-size: 0.74rem; letter-spacing: 0.04em; }
.code-head .lang { font-family: var(--font-mono); text-transform: uppercase; }
.cmd { background: var(--code-bg); color: var(--code-fg); }
.cmd .code-head { color: var(--code-dim); border-bottom: 1px solid var(--code-line); }
.cmd .lang::before { content: ""; display: inline-block; width: 7px; height: 7px; border-radius: 50%;
                     background: var(--accent); margin-right: 8px; vertical-align: 1px; }
.cmd .c { color: var(--code-dim); }
.cmd .prompt { color: var(--code-dim); -webkit-user-select: none; user-select: none; }
.out, .file { background: var(--surface); border: 1px solid var(--line); color: var(--fg); }
.out .code-head, .file .code-head { color: var(--muted); border-bottom: 1px solid var(--line-soft); background: var(--surface-2); }
.code.bare > .copy { position: absolute; top: 8px; right: 8px; background: var(--surface); }
.code.bare pre { padding-right: 44px; }
.copy {
  display: inline-flex; align-items: center; gap: 6px; font: inherit; font-family: var(--font-ui); font-size: 0.76rem;
  letter-spacing: 0; text-transform: none; color: inherit; background: transparent; border: 1px solid transparent;
  border-radius: 6px; padding: 3px 8px; cursor: pointer; opacity: 0.85;
}
.copy:hover { opacity: 1; border-color: currentColor; }
.copy.done { color: var(--ok); opacity: 1; }
.cmd .copy.done { color: #6fd39a; }
.code.bare > .copy .copy-label { display: none; }
@media (hover: hover) {
  .code.bare > .copy { opacity: 0; transition: opacity .15s; }
  .code.bare:hover > .copy, .code.bare > .copy:focus-visible { opacity: 1; }
}

/* Prompt card */
.prompt { margin: 0; border: 1px solid color-mix(in srgb, var(--accent) 30%, var(--line)); border-radius: 12px;
          background: var(--surface); overflow: hidden; }
.prompt figcaption { display: flex; align-items: center; gap: 8px; padding: 7px 8px 7px 14px; font-size: 0.8rem;
                     color: var(--muted); background: var(--accent-soft); }
.prompt .who { font-weight: 650; color: var(--accent); }
.prompt .to { font-weight: 600; color: var(--fg); }
.prompt .copy { margin-left: auto; }
.prompt-text { padding: 14px 16px 16px; white-space: pre-wrap; overflow-wrap: anywhere; font-size: 0.98rem; line-height: 1.6; }

/* Hero */
.hero { padding: 4px 0 36px; margin-bottom: 36px; border-bottom: 1px solid var(--line-soft); }
.hero .eyebrow { font-family: var(--font-mono); font-size: 0.74rem; letter-spacing: 0.1em; text-transform: uppercase;
                 color: var(--accent); font-weight: 600; margin: 0 0 10px; }
.hero .hero-title { font-size: clamp(1.9rem, 4.5vw, 2.6rem); font-weight: 700; letter-spacing: -0.02em; line-height: 1.15; margin: 0 0 12px; }
.hero .pitch { font-size: 1.15rem; color: var(--muted); max-width: 62ch; margin: 0 0 20px; }
.flow { display: flex; flex-wrap: wrap; align-items: center; gap: 6px 8px; padding: 0; margin: 0 0 8px; list-style: none;
        font-size: 0.84rem; }
.flow li { display: flex; align-items: center; gap: 8px; }
.flow li + li::before { content: "→"; color: var(--muted); }
.flow span { border: 1px solid var(--line); background: var(--surface); border-radius: 999px; padding: 3px 11px; white-space: nowrap; }
.flow .you { border-color: var(--accent); color: var(--accent); font-weight: 600; }
.hero .arch { font-size: 0.92rem; color: var(--muted); max-width: 70ch; margin: 0 0 24px; }
.cards { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 12px; }
.card { display: flex; flex-direction: column; gap: 4px; padding: 14px 16px; border: 1px solid var(--line);
        border-radius: 12px; background: var(--surface); color: var(--fg); text-decoration: none;
        transition: border-color .15s, box-shadow .15s, transform .15s; }
.card:hover { color: var(--fg); border-color: var(--accent); box-shadow: var(--shadow); transform: translateY(-1px); }
.card strong { font-size: 1rem; font-weight: 650; display: flex; justify-content: space-between; gap: 8px; }
.card strong::after { content: "→"; color: var(--accent); }
.card span { font-size: 0.86rem; color: var(--muted); line-height: 1.45; }

/* Pager and footer */
.pager { display: grid; grid-template-columns: 1fr 1fr; gap: 12px; margin: 48px 0 0; max-width: 760px; }
.pager a { display: flex; flex-direction: column; gap: 2px; padding: 12px 16px; border: 1px solid var(--line);
           border-radius: 10px; text-decoration: none; color: var(--fg); background: var(--surface); transition: border-color .15s; }
.pager a:hover { border-color: var(--accent); color: var(--fg); }
.pager small { font-size: 0.76rem; color: var(--muted); }
.pager .next { grid-column: 2; text-align: right; }
.site-foot { margin: 56px 0 0; padding: 20px 0 32px; border-top: 1px solid var(--line-soft); color: var(--muted);
             font-size: 0.82rem; max-width: 760px; line-height: 1.6; }
.site-foot code { font-family: var(--font-mono); font-size: 0.95em; overflow-wrap: anywhere; }
.toast { position: fixed; left: 50%; bottom: 24px; transform: translateX(-50%); background: var(--fg); color: var(--bg);
         padding: 8px 14px; border-radius: 8px; font-size: 0.86rem; box-shadow: var(--shadow); opacity: 0;
         pointer-events: none; transition: opacity .2s; z-index: 60; }
.toast.show { opacity: 1; }
.flash { animation: flash 1.6s ease-out; }
@keyframes flash { 0%, 30% { background: var(--accent-soft); } 100% { background: transparent; } }

/* Narrow screens */
@media (max-width: 1180px) {
  .page-grid { grid-template-columns: minmax(0, 1fr); }
  .toc { display: none; }
  main { padding: 32px 40px 24px; }
}
@media (max-width: 880px) {
  .layout { grid-template-columns: minmax(0, 1fr); }
  .menu-btn { display: inline-grid; }
  .sidebar {
    position: fixed; z-index: 40; top: var(--top); left: 0; bottom: 0; height: auto; width: min(300px, 86vw);
    transform: translateX(-102%); visibility: hidden; transition: transform .2s ease, visibility 0s linear .2s;
    box-shadow: var(--shadow); background: var(--bg);
  }
  .nav-open .sidebar { transform: none; visibility: visible; transition: transform .2s ease; }
  .nav-open .backdrop { display: block; position: fixed; inset: var(--top) 0 0 0; z-index: 35;
                        background: rgba(10, 16, 20, .35); }
  main { padding: 24px 16px 16px; }
  .cards { grid-template-columns: minmax(0, 1fr); }
  .brand small { display: none; }
}
@media (max-width: 520px) {
  .topbar { gap: 8px; padding: 0 12px; }
  .brand .brand-text { display: none; }
  .search kbd { display: none; }
  .doc h1 { font-size: 1.7rem; }
  .pager { grid-template-columns: 1fr; }
  .pager .next { grid-column: 1; }
}
@media (prefers-reduced-motion: reduce) {
  *, *::before, *::after { transition: none !important; animation: none !important; scroll-behavior: auto !important; }
}
@media print {
  .topbar, .sidebar, .toc, .pager, .copy, .anchor { display: none !important; }
  .layout { display: block; }
  .page[hidden] { display: block !important; }
}
"""

HEAD_JS = ("try{var t=localStorage.getItem('factory-docs-theme');"
           "if(t==='light'||t==='dark')document.documentElement.setAttribute('data-theme',t);"
           "document.documentElement.setAttribute('data-theme-choice',t==='light'||t==='dark'?t:'system')}"
           "catch(e){document.documentElement.setAttribute('data-theme-choice','system')}"
           "document.documentElement.classList.add('js');")

JS = r"""
(function () {
  'use strict';
  var d = document, root = d.documentElement;
  var pages = Array.prototype.slice.call(d.querySelectorAll('section.page'));
  var docKeys = (d.body.getAttribute('data-docs') || '').split(' ').filter(Boolean);
  var navLinks = Array.prototype.slice.call(d.querySelectorAll('.sidebar a[data-route]'));
  var toast = d.getElementById('toast'), toastTimer = null;
  var current = null, spy = null;

  function say(msg) {
    toast.textContent = msg; toast.classList.add('show');
    clearTimeout(toastTimer); toastTimer = setTimeout(function () { toast.classList.remove('show'); }, 1600);
  }

  function find(id) {
    if (!id) return null;
    var el = d.getElementById(id);
    if (el) return el;
    var low = id.toLowerCase(), keys = (current ? [current.getAttribute('data-doc')] : []).concat(docKeys);
    for (var i = 0; i < keys.length; i++) {
      el = d.getElementById(keys[i] + ':' + low);
      if (el) return el;
    }
    return null;
  }

  function show(page) {
    if (page === current) return false;
    pages.forEach(function (p) { p.hidden = p !== page; });
    current = page;
    var route = page.id;
    navLinks.forEach(function (a) {
      var on = a.getAttribute('data-route') === route;
      if (on) a.setAttribute('aria-current', 'page'); else a.removeAttribute('aria-current');
      var li = a.parentNode;
      if (li.classList.contains('has-sub')) li.classList.toggle('open', on);
    });
    d.title = page.getAttribute('data-title') + ' · AI Code Factory docs';
    watchToc(page);
    return true;
  }

  function route(initial) {
    var id = location.hash.slice(1);
    try { id = decodeURIComponent(id); } catch (e) {}
    var el = id && id !== '/' ? find(id) : null;
    var page = el ? (el.classList.contains('page') ? el : el.closest('section.page')) : null;
    if (el && !page) { if (!current) show(pages[0]); return; }   // e.g. the skip link's #main
    if (!page) page = current || pages[0];
    var changed = show(page);
    closeNav();
    if (el && el !== page) {
      el.scrollIntoView();
      el.classList.remove('flash'); void el.offsetWidth; el.classList.add('flash');
      if (!initial) el.focus({ preventScroll: true });
    } else if (changed || el === page) {
      window.scrollTo(0, 0);
      var h = page.querySelector('h1');
      if (h && !initial) h.focus({ preventScroll: true });
    }
  }

  // "On this page": highlight the section being read.
  function watchToc(page) {
    if (spy) spy.disconnect();
    var links = page.querySelectorAll('.toc a');
    if (!links.length || !('IntersectionObserver' in window)) return;
    var map = {};
    Array.prototype.forEach.call(links, function (a) { map[a.getAttribute('href').slice(1)] = a; });
    spy = new IntersectionObserver(function (entries) {
      entries.forEach(function (e) {
        if (!e.isIntersecting) return;
        Array.prototype.forEach.call(links, function (a) { a.classList.toggle('active', a === map[e.target.id]); });
      });
    }, { rootMargin: '-64px 0px -70% 0px' });
    Object.keys(map).forEach(function (id) { var h = d.getElementById(id); if (h) spy.observe(h); });
  }

  // Theme: system -> light -> dark, remembered when storage works.
  var themeBtn = d.getElementById('theme-btn');
  function applyTheme(choice) {
    if (choice === 'system') root.removeAttribute('data-theme'); else root.setAttribute('data-theme', choice);
    root.setAttribute('data-theme-choice', choice);
    themeBtn.setAttribute('aria-label', 'Theme: ' + choice + '. Change theme');
    themeBtn.title = 'Theme: ' + choice;
  }
  themeBtn.addEventListener('click', function () {
    var order = ['system', 'light', 'dark'];
    var next = order[(order.indexOf(root.getAttribute('data-theme-choice') || 'system') + 1) % 3];
    applyTheme(next);
    try { if (next === 'system') localStorage.removeItem('factory-docs-theme'); else localStorage.setItem('factory-docs-theme', next); } catch (e) {}
    say('Theme: ' + next);
  });
  applyTheme(root.getAttribute('data-theme-choice') || 'system');

  // Mobile navigation drawer.
  var menuBtn = d.getElementById('menu-btn');
  function closeNav() { d.body.classList.remove('nav-open'); menuBtn.setAttribute('aria-expanded', 'false'); }
  menuBtn.addEventListener('click', function () {
    var open = d.body.classList.toggle('nav-open');
    menuBtn.setAttribute('aria-expanded', open ? 'true' : 'false');
  });
  d.getElementById('backdrop').addEventListener('click', closeNav);

  // Copy buttons and heading links.
  function copyText(text) {
    if (navigator.clipboard && window.isSecureContext !== false) {
      return navigator.clipboard.writeText(text).catch(function () { return fallbackCopy(text); });
    }
    return fallbackCopy(text);
  }
  function fallbackCopy(text) {
    return new Promise(function (resolve, reject) {
      var ta = d.createElement('textarea');
      ta.value = text; ta.setAttribute('readonly', ''); ta.style.position = 'fixed'; ta.style.opacity = '0';
      d.body.appendChild(ta); ta.select();
      var ok = false; try { ok = d.execCommand('copy'); } catch (e) {}
      d.body.removeChild(ta); if (ok) resolve(); else reject(new Error('copy failed'));
    });
  }
  d.addEventListener('click', function (e) {
    var btn = e.target.closest('.copy');
    if (btn) {
      var box = btn.closest('.code, .prompt'), src = box && box.querySelector('[data-copy-src]');
      if (!src) return;
      var text = src.textContent.replace(/\n+$/, '');
      if (box.classList.contains('cmd')) text = text.split('\n').map(function (l) { return l.replace(/^(\s*)\$ /, '$1'); }).join('\n');
      copyText(text).then(function () {
        var label = btn.querySelector('.copy-label');
        btn.classList.add('done'); if (label) label.textContent = 'Copied';
        say('Copied to clipboard');
        setTimeout(function () { btn.classList.remove('done'); if (label) label.textContent = 'Copy'; }, 1500);
      }, function () { say('Could not copy: select the text instead'); });
      return;
    }
    var anchor = e.target.closest('a.anchor');
    if (anchor) {
      copyText(location.href.split('#')[0] + anchor.getAttribute('href')).then(function () { say('Link copied'); }, function () {});
    }
  });

  // Search over every page's headings and text.
  var input = d.getElementById('search'), list = d.getElementById('results');
  var index = null, hits = [], active = -1;
  function buildIndex() {
    index = [];
    pages.forEach(function (p) {
      var title = p.getAttribute('data-title');
      var entry = { id: p.id, head: title, page: title, text: '' };
      index.push(entry);
      var nodes = p.querySelectorAll('.doc > *');
      Array.prototype.forEach.call(nodes, function (n) {
        if (/^H[1-4]$/.test(n.tagName) && n.id) {
          if (n.tagName === 'H1') { entry.id = n.id; return; }
          entry = { id: n.id, head: n.textContent, page: title, text: '' };
          index.push(entry);
        } else if (!n.classList.contains('hero')) {
          entry.text += ' ' + n.textContent.replace(/\s+/g, ' ');
        }
      });
    });
  }
  function highlight(parent, text, terms) {
    var low = text.toLowerCase(), i = 0;
    while (i < text.length) {
      var best = -1, len = 0;
      terms.forEach(function (t) { var k = low.indexOf(t, i); if (k >= 0 && (best < 0 || k < best)) { best = k; len = t.length; } });
      if (best < 0) { parent.appendChild(d.createTextNode(text.slice(i))); break; }
      parent.appendChild(d.createTextNode(text.slice(i, best)));
      var m = d.createElement('mark'); m.textContent = text.slice(best, best + len); parent.appendChild(m);
      i = best + len;
    }
  }
  function snippet(text, terms) {
    var low = text.toLowerCase(), at = -1;
    terms.forEach(function (t) { var k = low.indexOf(t); if (k >= 0 && (at < 0 || k < at)) at = k; });
    if (at < 0) return text.slice(0, 140).trim();
    var start = Math.max(0, at - 50);
    return (start > 0 ? '…' : '') + text.slice(start, start + 150).trim() + (start + 150 < text.length ? '…' : '');
  }
  function search(q) {
    if (!index) buildIndex();
    var terms = q.toLowerCase().split(/\s+/).filter(Boolean);
    list.textContent = ''; hits = []; active = -1;
    if (!terms.length) { close(); return; }
    index.forEach(function (e) {
      var head = e.head.toLowerCase(), hay = head + ' ' + e.text.toLowerCase(), score = 0;
      for (var i = 0; i < terms.length; i++) {
        if (hay.indexOf(terms[i]) < 0) return;
        if (head.indexOf(terms[i]) >= 0) score += 10;
        if (e.page.toLowerCase().indexOf(terms[i]) >= 0) score += 3;
        score += Math.min(5, hay.split(terms[i]).length - 1);
      }
      hits.push({ e: e, score: score });
    });
    hits.sort(function (a, b) { return b.score - a.score; });
    hits = hits.slice(0, 12);
    if (!hits.length) {
      var li = d.createElement('li'); li.className = 'empty'; li.textContent = 'No results for “' + q + '”';
      list.appendChild(li);
    }
    hits.forEach(function (h, k) {
      var li = d.createElement('li'), a = d.createElement('a');
      li.id = 'sr-' + k; li.setAttribute('role', 'option'); li.setAttribute('aria-selected', 'false');
      a.href = '#' + h.e.id; a.tabIndex = -1;
      var head = d.createElement('span'); head.className = 'r-head'; highlight(head, h.e.head, terms); a.appendChild(head);
      if (h.e.page !== h.e.head) { var pg = d.createElement('span'); pg.className = 'r-page'; pg.textContent = h.e.page; a.appendChild(pg); }
      var sn = d.createElement('span'); sn.className = 'r-snip'; highlight(sn, snippet(h.e.text, terms), terms); a.appendChild(sn);
      a.addEventListener('click', function () { close(); input.blur(); });
      li.appendChild(a); list.appendChild(li);
    });
    list.hidden = false; input.setAttribute('aria-expanded', 'true');
    if (hits.length) select(0);
  }
  function select(k) {
    var items = list.querySelectorAll('li[role="option"]');
    if (!items.length) return;
    active = (k + items.length) % items.length;
    Array.prototype.forEach.call(items, function (li, i) { li.setAttribute('aria-selected', i === active ? 'true' : 'false'); });
    input.setAttribute('aria-activedescendant', items[active].id);
    items[active].scrollIntoView({ block: 'nearest' });
  }
  function close() {
    list.hidden = true; input.setAttribute('aria-expanded', 'false'); input.removeAttribute('aria-activedescendant');
  }
  input.addEventListener('input', function () { search(input.value); });
  input.addEventListener('focus', function () { if (input.value) search(input.value); });
  input.addEventListener('keydown', function (e) {
    if (e.key === 'ArrowDown') { e.preventDefault(); select(active + 1); }
    else if (e.key === 'ArrowUp') { e.preventDefault(); select(active - 1); }
    else if (e.key === 'Enter') {
      e.preventDefault();
      if (hits.length) { location.hash = '#' + hits[Math.max(active, 0)].e.id; close(); input.blur(); }
    } else if (e.key === 'Escape') {
      if (input.value) { input.value = ''; close(); } else { input.blur(); }
    }
  });
  d.addEventListener('click', function (e) { if (!e.target.closest('.search')) close(); });
  d.addEventListener('keydown', function (e) {
    var t = e.target, typing = t && (t.tagName === 'INPUT' || t.tagName === 'TEXTAREA' || t.isContentEditable);
    if (e.key === '/' && !typing && !e.metaKey && !e.ctrlKey && !e.altKey) { e.preventDefault(); input.focus(); input.select(); }
    else if (e.key === 'Escape' && d.body.classList.contains('nav-open')) { closeNav(); menuBtn.focus(); }
  });

  window.addEventListener('hashchange', function () { route(false); });
  route(true);
})();
"""

ICON_MENU = ('<svg aria-hidden="true" viewBox="0 0 20 20" width="18" height="18"><path d="M3 5.5h14M3 10h14M3 14.5h14" '
             'stroke="currentColor" stroke-width="1.6" stroke-linecap="round"/></svg>')
ICON_SEARCH = ('<svg class="glass" aria-hidden="true" viewBox="0 0 16 16" width="14" height="14"><circle cx="7" cy="7" '
               'r="5" fill="none" stroke="currentColor" stroke-width="1.6"/><path d="M11 11l3.5 3.5" stroke="currentColor" '
               'stroke-width="1.6" stroke-linecap="round"/></svg>')
ICON_THEME = (
    '<svg class="i-system" aria-hidden="true" viewBox="0 0 20 20" width="18" height="18"><circle cx="10" cy="10" r="6.5" '
    'fill="none" stroke="currentColor" stroke-width="1.5"/><path d="M10 3.5a6.5 6.5 0 0 1 0 13z" fill="currentColor"/></svg>'
    '<svg class="i-light" aria-hidden="true" viewBox="0 0 20 20" width="18" height="18"><circle cx="10" cy="10" r="3.5" '
    'fill="none" stroke="currentColor" stroke-width="1.5"/><path d="M10 2v2M10 16v2M2 10h2M16 10h2M4.3 4.3l1.4 1.4M14.3 '
    '14.3l1.4 1.4M4.3 15.7l1.4-1.4M14.3 5.7l1.4-1.4" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"/></svg>'
    '<svg class="i-dark" aria-hidden="true" viewBox="0 0 20 20" width="18" height="18"><path d="M16 12.5A6.5 6.5 0 0 1 '
    '7.5 4a6.5 6.5 0 1 0 8.5 8.5z" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linejoin="round"/></svg>')


def git_commit(root: Path) -> str:
    try:
        res = subprocess.run(
            ["git", "-c", "core.fsmonitor=false", "-c", "core.hooksPath=/dev/null", "-C", str(root),
             "rev-parse", "--short", "HEAD"],
            capture_output=True, text=True, timeout=10, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError):
        return ""
    out = res.stdout.strip()
    return out if res.returncode == 0 and re.fullmatch(r"[0-9a-f]{4,64}", out) else ""


def _route_of(site: Site, pred) -> str:
    for p in site.pages:
        if pred(p):
            return p.route
    return ""


def render_hero(site: Site) -> str:
    guides = [p for p in site.pages if p.group == "Guides"]
    commands = _route_of(site, lambda p: p.route == "/ref/commands")
    quick = _route_of(site, lambda p: p.route == "/ref/quick-start")
    cards = []
    first = guides[0].route if guides else quick
    if first:
        cards.append((first, "Set up your machine", guides[0].title_plain if guides else "Quick start"))
    if len(guides) > 1:
        cards.append((guides[1].route, "Build a feature", guides[1].title_plain))
    if commands:
        cards.append((commands, "Command reference", "Every factory command, in one table"))
    card_html = "".join(
        f'<a class="card" href="#{esc(r)}"><strong>{esc(t)}</strong><span>{esc(s)}</span></a>' for r, t, s in cards)
    return f"""<header class="hero">
<p class="eyebrow">Documentation</p>
<p class="hero-title">AI Code Factory</p>
<p class="pitch">Tell a Coordinator agent what you want built. It plans the work and hands it to sandboxed
role agents that research, implement and review it, then gives you a branch to merge.</p>
<ul class="flow" aria-label="How a task flows">
<li><span class="you">You</span></li><li><span>Coordinator</span></li><li><span>Developer · Reviewer · Researcher</span></li><li><span>Broker</span></li>
</ul>
<p class="arch">Every agent is Claude Code in its own microVM that can reach only Anthropic. Git, packages and the
web go through the Broker, which checks each call against policy, filters secrets and logs it.</p>
<nav class="cards" aria-label="Start here">{card_html}</nav>
</header>"""


def render_nav(site: Site) -> str:
    out = []
    groups: dict[str, list[Page]] = {}
    for p in site.pages:
        groups.setdefault(p.group, []).append(p)
    for group in ("Guides", "Reference", "Platform setup"):
        pages = groups.get(group, [])
        extra = ""
        if group == "Platform setup":
            mac = site.root / "docs" / "mac-quickstart.html"
            if mac.is_file():
                # A published site serves the quickstart next to the page.
                href = "mac-quickstart.html" if site.link_base else mac.resolve().as_uri()
                extra = (f'<li><a class="file-link" href="{esc(href)}">'
                         f'macOS quickstart</a></li>')
        if not pages and not extra:
            continue
        out.append(f'<h2 class="nav-group">{esc(group)}</h2><ul>')
        for k, p in enumerate(pages, 1):
            num = f'<span class="num">{k}</span>' if group == "Guides" else ""
            if group == "Reference" and p.subheads:
                subs = "".join(f'<li><a href="#{esc(i)}">{esc(t)}</a></li>' for i, t, dp in p.subheads if dp == 0)
                out.append(f'<li class="has-sub"><a href="#{esc(p.route)}" data-route="{esc(p.route)}">'
                           f'{esc(p.nav_label)}</a><ul>{subs}</ul></li>')
            else:
                out.append(f'<li><a href="#{esc(p.route)}" data-route="{esc(p.route)}">{num}{esc(p.nav_label)}</a></li>')
        out.append(extra + "</ul>")
    return "".join(out)


def toc_list(subheads: list) -> str:
    """Nested <ul> for (id, text, depth) entries; depth 1 nests under the previous depth 0."""
    out, open_sub = ["<ul>"], False
    for k, (hid, text, depth) in enumerate(subheads):
        if depth > 0 and k > 0 and not open_sub:
            out[-1] = out[-1][:-len("</li>")] + "<ul>"
            open_sub = True
        elif depth == 0 and open_sub:
            out.append("</ul></li>")
            open_sub = False
        out.append(f'<li><a href="#{esc(hid)}">{esc(text)}</a></li>')
    if open_sub:
        out.append("</ul></li>")
    out.append("</ul>")
    return "".join(out)


def render_page(site: Site, k: int) -> str:
    p = site.pages[k]
    title_id = f' id="{esc(p.title_id)}" tabindex="-1"' if p.title_id else ' tabindex="-1"'
    anchor = (f'<a class="anchor" href="#{esc(p.title_id)}" aria-label="Link to {esc(p.title_plain)}"></a>'
              if p.title_id else "")
    hero = render_hero(site) if k == 0 else ""
    toc = ""
    if len(p.subheads) >= 2:
        toc = (f'<nav class="toc" aria-label="On this page"><h2>On this page</h2>'
               f'{toc_list(p.subheads)}</nav>')
    pager = []
    if k > 0:
        prev = site.pages[k - 1]
        pager.append(f'<a class="prev" href="#{esc(prev.route)}"><small>Previous</small>{esc(prev.nav_label)}</a>')
    if k + 1 < len(site.pages):
        nxt = site.pages[k + 1]
        pager.append(f'<a class="next" href="#{esc(nxt.route)}"><small>Next</small>{esc(nxt.nav_label)}</a>')
    pager_html = f'<nav class="pager" aria-label="Previous and next page">{"".join(pager)}</nav>' if pager else ""
    return (f'<section class="page" id="{esc(p.route)}" data-doc="{esc(p.doc)}" data-title="{esc(p.title_plain)}" '
            f'aria-label="{esc(p.title_plain)}"><div class="page-grid"><article class="doc">{hero}'
            f'<h1{title_id}>{p.title_html}{anchor}</h1>\n{p.body}</article>{toc}</div>{pager_html}'
            f'</section>')


def render_site(site: Site, now: datetime | None = None) -> str:
    now = now or datetime.now().astimezone()
    commit = git_commit(site.root)
    built = f"commit <code>{esc(commit)}</code> of " if commit else ""
    sources = ", ".join(f"<code>{esc(str(d.path.relative_to(site.root)))}</code>"
                        for d in site.docs if d.kind != "guide")
    if any(d.kind == "guide" for d in site.docs):
        sources = "<code>docs/guides/</code>, " + sources
    if site.link_base:
        footer = (f'<footer class="site-foot">Built from {built}the factory repository on '
                  f'{esc(now.strftime("%Y-%m-%d %H:%M %Z").strip())}, from {sources}. Run '
                  f'<code>factory docs</code> in your checkout for the version that matches it.</footer>')
    else:
        footer = (f'<footer class="site-foot">Built from {built}<code>{esc(str(site.root))}</code> on '
                  f'{esc(now.strftime("%Y-%m-%d %H:%M %Z").strip())}. This page is generated from {sources}: '
                  f'edit those, then run <code>factory docs</code> again.</footer>')
    pages = "\n".join(render_page(site, k) for k in range(len(site.pages)))
    docs_attr = " ".join(d.key for d in site.docs)
    first_title = site.pages[0].title_plain if site.pages else "Docs"
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="color-scheme" content="light dark">
<title>{esc(first_title)} · AI Code Factory docs</title>
<script>{HEAD_JS}</script>
<style>{CSS}</style>
</head>
<body data-docs="{esc(docs_attr)}">
<a class="skip" href="#main">Skip to content</a>
<header class="topbar">
<button type="button" class="icon-btn menu-btn" id="menu-btn" aria-label="Menu" aria-controls="sidebar" aria-expanded="false">{ICON_MENU}</button>
<a class="brand" href="#{esc(site.pages[0].route) if site.pages else '/'}"><span class="brand-mark" aria-hidden="true"></span><span class="brand-text">AI Code Factory <small>docs</small></span></a>
<div class="search" role="search">
<label class="sr-only" for="search">Search the docs</label>
{ICON_SEARCH}<input id="search" type="search" placeholder="Search" autocomplete="off" spellcheck="false" role="combobox" aria-autocomplete="list" aria-controls="results" aria-expanded="false">
<kbd aria-hidden="true">/</kbd>
<ul class="results" id="results" role="listbox" aria-label="Search results" hidden></ul>
</div>
<button type="button" class="icon-btn theme-btn" id="theme-btn" aria-label="Change theme">{ICON_THEME}</button>
</header>
<div class="layout">
<nav class="sidebar" id="sidebar" aria-label="Documentation">{render_nav(site)}</nav>
<div class="backdrop" id="backdrop"></div>
<main id="main" tabindex="-1">
{pages}
{footer}
</main>
</div>
<div class="toast" id="toast" role="status" aria-live="polite"></div>
<script>{JS}</script>
</body>
</html>
"""


def build_html(root: Path, now: datetime | None = None,
               link_base: str | None = None) -> tuple[str, list[str]]:
    """The page for the checkout at root, and any warnings (e.g. broken anchors)."""
    if not root.is_dir():
        raise DocsError(f"{root} is not a directory")
    if link_base is not None and not re.match(r"^https://[^\s\"'<>]+/$", link_base):
        raise DocsError("--link-base must be an https:// URL ending in /")
    site = Site(root, link_base)
    site.build()
    return render_site(site, now), site.warnings


def write_atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        mask = os.umask(0)
        os.umask(mask)
        os.chmod(tmp, 0o666 & ~mask)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="docs.py", description="Render the factory's docs into one self-contained HTML page.")
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parent.parent,
                        help="the factory checkout (default: the one holding this script)")
    parser.add_argument("--out", type=Path, default=None,
                        help="write the page here (atomically) instead of to stdout")
    parser.add_argument("--link-base", default=None, metavar="URL",
                        help="for a published copy (e.g. GitHub Pages): link repo files to URL<path> "
                             "(e.g. https://github.com/<org>/<repo>/blob/<commit>/) instead of file://, "
                             "and expect docs/mac-quickstart.html to be served next to the page")
    args = parser.parse_args(argv)
    try:
        page, _warnings = build_html(args.root, link_base=args.link_base)
        if args.out is not None:
            write_atomic(args.out, page)
        else:
            sys.stdout.buffer.write(page.encode("utf-8"))
            sys.stdout.buffer.flush()
    except BrokenPipeError:
        # The reader stopped early (e.g. `factory docs --print | head`): not an error.
        # Point stdout at devnull so the interpreter's final flush can't fail again.
        os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
        return 0
    except DocsError as e:
        print(f"docs: {e}", file=sys.stderr)
        return 1
    except OSError as e:
        print(f"docs: {e.strerror or e}: {e.filename or ''}".rstrip(": "), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
