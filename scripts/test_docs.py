"""Tests for the factory docs generator (scripts/docs.py).

Run:
  python3 -m unittest discover -s scripts -p 'test_docs.py' -v
"""

from __future__ import annotations

import contextlib
import html
import io
import os
import re
import sys
import tempfile
import unittest
from html.parser import HTMLParser
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import docs  # noqa: E402

REPO = Path(__file__).resolve().parent.parent


def write(root: Path, rel: str, text: str) -> Path:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


class Fixture(unittest.TestCase):
    """A temp checkout with a README and optional extra files."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name).resolve() / "repo"
        self.root.mkdir()
        self.addCleanup(self._tmp.cleanup)

    def build(self, readme: str = "# T\n\nIntro.\n", **files) -> str:
        write(self.root, "README.md", readme)
        for rel, text in files.items():
            write(self.root, rel.replace("__", "/"), text)
        page, self.warnings = docs.build_html(self.root)
        return page

    def md(self, text: str) -> str:
        """Render one markdown snippet as a README body."""
        write(self.root, "README.md", text)
        site = docs.Site(self.root)
        site.discover()
        r = docs.DocRenderer(site, "readme", self.root / "README.md")
        return "\n".join(b.html for b in r.render(text))


def ids_and_links(page: str):
    ids = {html.unescape(i) for i in re.findall(r'\sid="([^"]+)"', page)}
    links = [html.unescape(h) for h in re.findall(r'\shref="#([^"]*)"', page)]
    return ids, links


# --------------------------------------------------------------------------

class MarkdownTests(Fixture):
    def test_nested_lists_with_code_blocks(self):
        out = self.md(
            "1. First step:\n"
            "   ```sh\n"
            "   sbx secret set github\n"
            "   ```\n"
            "2. Second, with sub-points:\n"
            "   - a\n"
            "     - deeper `x`\n"
            "   - b\n"
            "3. Third\n"
            "\n"
            "After the list.\n")
        self.assertEqual(out.count("<ol"), 1)
        self.assertIn("sbx secret set github", out)
        self.assertRegex(out, r"<li><p>First step:</p>\s*<div class=\"code cmd\">")
        # The fence sits inside item 1, before item 2 starts.
        self.assertLess(out.index("sbx secret set"), out.index("Second"))
        self.assertEqual(out.count("<ul"), 2)
        self.assertRegex(out, r"deeper <code>x</code></p></li></ul></li>")
        self.assertTrue(out.rstrip().endswith("<p>After the list.</p>"))
        self.assertLess(out.index("</ol>"), out.index("After the list."))

    def test_list_item_paragraphs_and_loose_lists(self):
        out = self.md("- one\n\n  more about one\n- two\n")
        self.assertNotIn('class="tight"', out)
        self.assertIn("<p>more about one</p>", out)
        tight = self.md("- one\n- two\n")
        self.assertIn('<ul class="tight">', tight)

    def test_ordered_start_and_paragraph_after_fence_in_item(self):
        out = self.md("3. three\n   ```sh\n   x\n   ```\nNot in the list.\n")
        self.assertIn('<ol start="3"', out)
        self.assertIn("<p>Not in the list.</p>", out)
        self.assertLess(out.index("</ol>"), out.index("Not in the list."))

    def test_list_interrupts_paragraph(self):
        out = self.md("Errors:\n- `HTTP 401`: bad\n- other\n")
        self.assertIn("<p>Errors:</p>", out)
        self.assertIn("<ul", out)

    def test_table_alignment_and_escaped_pipes(self):
        out = self.md(
            "| Left | Center | Right | Plain |\n"
            "|:---|:---:|---:|---|\n"
            "| `a \\| b` | **x** | 3 | ok |\n"
            "| short |\n")
        self.assertIn('<th class="al-left">Left</th>', out)
        self.assertIn('<th class="al-center">Center</th>', out)
        self.assertIn('<th class="al-right">Right</th>', out)
        self.assertIn("<th>Plain</th>", out)
        self.assertIn("<td class=\"al-left\"><code>a | b</code></td>", out)
        self.assertIn('<td class="al-center"><strong>x</strong></td>', out)
        # Short rows are padded to the header's width.
        self.assertEqual(out.count("<td"), 8)
        self.assertIn('class="table-wrap"', out)

    def test_fenced_blocks(self):
        out = self.md("```bash\n$ factory start   # opens Herdr\n# comment\necho '#not'\n```\n")
        self.assertIn('class="code cmd"', out)
        self.assertIn('<span class="prompt" aria-hidden="true">$ </span>', out)
        self.assertIn('<span class="c"># opens Herdr</span>', out)
        self.assertIn('<span class="c"># comment</span>', out)
        self.assertIn("echo &#x27;#not&#x27;", out)
        self.assertIn('aria-label="Copy commands"', out)

        out = self.md("```prompt\nAdd tests for <slug>.\nThen review.\n```\n")
        self.assertIn('class="prompt"', out)
        self.assertIn("Coordinator", out)
        self.assertIn("Add tests for &lt;slug&gt;.\nThen review.", out)

        out = self.md("```\nplain **not bold** <b>\n```\n")
        self.assertIn('class="code out bare"', out)
        self.assertIn("plain **not bold** &lt;b&gt;", out)

        out = self.md("```toml\n[git]\n```\n")
        self.assertIn('class="code file"', out)
        self.assertIn('<span class="lang">TOML</span>', out)

        out = self.md("````markdown\n```sh\ninner\n```\n````\n")
        self.assertIn("```sh\ninner\n```", out)

    def test_blockquote_callouts(self):
        for word, cls in (("Note", "note"), ("Tip", "tip"), ("Warning", "warning"), ("Important", "important")):
            out = self.md(f"> **{word}:** be careful\n> with this.\n")
            self.assertIn(f'class="callout callout-{cls}" role="note"', out)
            self.assertIn(f"<strong>{word}:</strong> be careful\nwith this.", out)
        out = self.md("> **Why two starts?** Because.\n")
        self.assertIn('class="callout callout-plain"', out)
        out = self.md("> outer\n>\n> > inner\n")
        self.assertEqual(out.count('class="callout'), 2)

    def test_emphasis_edge_cases(self):
        out = self.md("Use snake_case_words and __init__ here, *em*, _em2_, **strong**.\n")
        self.assertIn("snake_case_words", out)
        self.assertNotIn("<em>case</em>", out)
        self.assertIn("<em>em</em>", out)
        self.assertIn("<em>em2</em>", out)
        self.assertIn("<strong>strong</strong>", out)
        out = self.md("Code `a **b** _c_ *d*` stays.\n")
        self.assertIn("<code>a **b** _c_ *d*</code>", out)
        out = self.md("Escaped \\*not em\\* and 2 * 3 * 4.\n")
        self.assertNotIn("<em>", out)
        out = self.md("**Note on `factory secret set`:** text\n")
        self.assertIn("<strong>Note on <code>factory secret set</code>:</strong>", out)

    def test_code_span_across_line_break(self):
        # README.md: an inline code span continues onto the next line of a list item.
        out = self.md("- Wrapped in `<<<UNTRUSTED CONTENT id=...>>> ... <<<END\n"
                      "  UNTRUSTED CONTENT id=...>>>` markers.\n")
        self.assertIn("<code>&lt;&lt;&lt;UNTRUSTED CONTENT id=...&gt;&gt;&gt; ... "
                      "&lt;&lt;&lt;END UNTRUSTED CONTENT id=...&gt;&gt;&gt;</code> markers.", out)
        out = self.md("Run `factory approvals\nwatch` now.\n")
        self.assertIn("<code>factory approvals watch</code>", out)

    def test_headings_rules_and_autolinks(self):
        out = self.md("## Title ##\n\n---\n\nSee <https://example.com/a> and https://herdr.dev/latest.json.\n")
        self.assertIn('id="readme:title"', out)
        self.assertIn("<hr>", out)
        self.assertIn('<a href="https://example.com/a" target="_blank" rel="noopener noreferrer"', out)
        self.assertIn('href="https://herdr.dev/latest.json"', out)
        self.assertIn("latest.json</a>.", out)


class SecurityTests(Fixture):
    def test_raw_html_is_escaped(self):
        page = self.build("# T\n\n<script>alert(1)</script>\n\n<img src=x onerror=alert(1)>\n\n"
                          "Inline <b onclick=x>bold</b>.\n\n| a |\n|---|\n| <iframe> |\n")
        self.assertNotIn("<script>alert", page)
        self.assertIn("&lt;script&gt;alert(1)&lt;/script&gt;", page)
        self.assertNotIn("<img", page)
        self.assertNotIn("<b onclick", page)
        self.assertNotIn("<iframe", page)

    def test_dangerous_link_schemes_are_plain_text(self):
        page = self.build("# T\n\n[a](javascript:alert(1)) [b](JaVaScRiPt:x) [c](data:text/html,x) "
                          "[d](vbscript:x) [e](file:///etc/passwd) [f](//evil.example/x) [g](java\tscript:x)\n")
        for bad in ("javascript:", "JaVaScRiPt", "data:text", "vbscript:", "file:///etc", "//evil"):
            self.assertNotIn(f'href="{bad}', page)
        self.assertNotRegex(page, r'href="[^"]*(?i:javascript|data:|vbscript)')
        self.assertIn('<span class="dead-link">a</span>', page)

    def test_relative_links_cannot_escape_root(self):
        outside = self.root.parent / "secret.txt"
        outside.write_text("x")
        write(self.root, "scripts/lib/common.sh", "#!/bin/sh\n")
        os.symlink(outside, self.root / "link.txt")
        page = self.build("# T\n\n[up](../secret.txt) [abs](/../secret.txt) [enc](%2e%2e/secret.txt) "
                          "[sym](link.txt) [ok](scripts/lib/common.sh) [missing](nope.sh)\n")
        self.assertNotIn("secret.txt\"", page)
        self.assertNotIn(outside.as_uri(), page)
        self.assertIn(f'href="{(self.root / "scripts/lib/common.sh").as_uri()}"', page)
        for label in ("up", "abs", "enc", "sym", "missing"):
            self.assertIn(f'<span class="dead-link">{label}</span>', page)


class LinkTests(Fixture):
    def test_in_page_and_cross_doc_links_resolve(self):
        write(self.root, "docs/setup-wsl.md", "# Windows\n\n## Troubleshooting\n\nBack to [roles](../README.md#roles).\n")
        write(self.root, "docs/guides/01-start.md",
              "# Start here\n\nSummary.\n\n## Step one\n\nSee [Commands](../../README.md#commands), "
              "[next](02-more.md), [step](#step-one) and [WSL](../setup-wsl.md#troubleshooting).\n")
        write(self.root, "docs/guides/02-more.md", "# More\n\nSummary two.\n")
        page = self.build(
            "# Factory\n\nSee [WSL](docs/setup-wsl.md), [trouble](docs/setup-wsl.md#troubleshooting), "
            "[settings](#local-settings) and [guide](docs/guides/01-start.md).\n\n"
            "## Commands\n\n### Local settings\n\nText.\n\n## Roles\n\nText.\n")
        self.assertEqual(self.warnings, [])
        self.assertIn('href="#/setup/wsl"', page)
        self.assertIn('href="#wsl:troubleshooting"', page)
        self.assertIn('href="#readme:local-settings"', page)
        self.assertIn('href="#/guide/start"', page)
        self.assertIn('href="#readme:commands"', page)
        self.assertIn('href="#/guide/more"', page)
        self.assertIn('href="#guide-start:step-one"', page)
        self.assertIn('href="#readme:roles"', page)
        ids, links = ids_and_links(page)
        self.assertEqual([link for link in links if link not in ids], [])
        # Guides come first, then the README's pages, then WSL; the hero is on the first page.
        order = re.findall(r'<section class="page" id="([^"]+)"', page)
        self.assertEqual(order, ["/guide/start", "/guide/more", "/ref/overview", "/ref/commands",
                                 "/ref/roles", "/setup/wsl"])
        self.assertLess(page.index('class="hero"'), page.index('id="/guide/more"'))

    def test_broken_anchor_warns_and_falls_back_to_page(self):
        page = self.build("# T\n\n[x](#nope)\n\n## A\n")
        self.assertEqual(len(self.warnings), 1)
        self.assertIn("#nope", self.warnings[0])
        ids, links = ids_and_links(page)
        self.assertEqual([link for link in links if link not in ids], [])

    def test_zero_guides(self):
        page = self.build("# T\n\nIntro.\n\n## Quick start\n\nGo.\n\n## Commands\n\n| a |\n|---|\n| b |\n")
        self.assertNotIn("/guide/", page)
        self.assertIn('href="#/ref/quick-start"', page)
        self.assertIn('href="#/ref/commands"', page)
        ids, links = ids_and_links(page)
        self.assertEqual([link for link in links if link not in ids], [])

    def test_real_docs_links_all_resolve(self):
        page, warnings = docs.build_html(REPO)
        self.assertEqual(warnings, [])
        ids, links = ids_and_links(page)
        broken = sorted({link for link in links if link not in ids})
        self.assertEqual(broken, [], "in-page links without a target id")
        # Every link written in README, setup-wsl and every guide became a working link.
        sources = [REPO / "README.md", REPO / "docs" / "setup-wsl.md"] + sorted((REPO / "docs" / "guides").glob("*.md"))
        for src in sources:
            if not src.is_file():
                continue
            for target in re.findall(r"\]\(([^)\s]+)\)", src.read_text(encoding="utf-8")):
                if target.startswith(("http://", "https://", "mailto:")):
                    continue
                with self.subTest(source=src.name, target=target):
                    path, _, frag = target.partition("#")
                    if not path or path.endswith(".md"):
                        if frag:
                            self.assertTrue(any(i.endswith(":" + frag) for i in ids), target)
                    else:
                        self.assertTrue((src.parent / path).exists(), target)
        self.assertNotIn('class="dead-link"', page)

    def test_real_guides_render_as_pages(self):
        guides = sorted((REPO / "docs" / "guides").glob("*.md"))
        page, _ = docs.build_html(REPO)
        routes = re.findall(r'<section class="page" id="(/guide/[^"]+)"', page)
        self.assertEqual(len(routes), len(guides))



class LinkBaseTests(Fixture):
    BASE = "https://github.com/acme/factory/blob/abc1234/"

    def test_repo_file_links_go_to_the_link_base(self):
        write(self.root, "scripts/lib/common.sh", "# pins\n")
        write(self.root, "docs/mac-quickstart.html", "<!doctype html>")
        write(self.root, "README.md", "# T\n\nPins live in [common.sh](scripts/lib/common.sh).\n")
        page, warnings = docs.build_html(self.root, link_base=self.BASE)
        self.assertEqual(warnings, [])
        self.assertIn(f'href="{self.BASE}scripts/lib/common.sh"', page)
        self.assertNotIn("file://", page)
        self.assertIn('href="mac-quickstart.html"', page)  # served next to the page
        self.assertNotIn(str(self.root), page)  # no local checkout path in the footer

    def test_without_a_link_base_repo_files_stay_local(self):
        write(self.root, "scripts/lib/common.sh", "# pins\n")
        page = self.build("# T\n\nSee [common.sh](scripts/lib/common.sh).\n")
        self.assertIn((self.root / "scripts/lib/common.sh").as_uri(), page)

    def test_link_base_must_be_https_and_end_in_a_slash(self):
        write(self.root, "README.md", "# T\n")
        for bad in ("http://example.com/", "https://example.com/blob", "javascript:alert(1)/",
                    'https://e.com/"onmouseover=x/'):
            with self.assertRaises(docs.DocsError):
                docs.build_html(self.root, link_base=bad)


class ServedTests(Fixture):
    """build_served(): the dashboard's copy (MANUAL), under a CSP with no inline code."""

    def test_assets_are_separate_files(self):
        files, warnings = docs.build_served(REPO)
        self.assertEqual(warnings, [])
        self.assertEqual(set(files), {"index.html", "docs.css", "docs-head.js", "docs.js"})
        page = files["index.html"]
        self.assertIsNone(re.search(r"<script(?![^>]*\ssrc=)", page), "inline script")
        self.assertIsNone(re.search(r"\sstyle=", page), "inline style attribute")
        self.assertIsNone(re.search(r"\son[a-z]+=", page), "inline event handler")
        self.assertNotIn("<style", page)
        self.assertIn('<script src="docs-head.js"></script>', page)
        self.assertIn('<link rel="stylesheet" href="docs.css">', page)
        self.assertIn('<script src="docs.js"></script>', page)
        self.assertEqual((files["docs.css"], files["docs-head.js"], files["docs.js"]),
                         (docs.CSS, docs.HEAD_JS, docs.JS))

    def test_repo_files_are_text_and_no_git_runs(self):
        write(self.root, "scripts/lib/common.sh", "# pins\n")
        write(self.root, "docs/mac-quickstart.html", "<!doctype html>")
        write(self.root, "README.md", "# T\n\nPins live in [common.sh](scripts/lib/common.sh).\n")
        with mock.patch.object(docs.subprocess, "run", side_effect=AssertionError("ran git")):
            files, _ = docs.build_served(self.root)
        page = files["index.html"]
        self.assertNotIn("file://", page)
        self.assertNotIn("mac-quickstart", page)
        self.assertIn('<span class="dead-link">common.sh</span>', page)


class SlugTests(unittest.TestCase):
    def test_github_slugs(self):
        cases = {
            "Getting the developer's work": "getting-the-developers-work",
            "Enabling approvals (Passdesk Pro)": "enabling-approvals-passdesk-pro",
            "Enabling git push and pull requests": "enabling-git-push-and-pull-requests",
            "Migrating from the pre-roles setup": "migrating-from-the-pre-roles-setup",
            "1. Prepare Windows": "1-prepare-windows",
            "Optional extras": "optional-extras",
            "factory_test helper": "factory_test-helper",
        }
        for heading, slug in cases.items():
            self.assertEqual(docs.slugify(heading), slug)

    def test_real_readme_ids(self):
        page, _ = docs.build_html(REPO)
        for slug in ("getting-the-developers-work", "enabling-approvals-passdesk-pro", "local-settings",
                     "enabling-a-private-npm-registry", "running-tests", "commands"):
            self.assertIn(f'id="readme:{slug}"', page)

    def test_duplicate_slugs(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write(root, "README.md", "# T\n\n## Notes\n\n### Example\n\n## More\n\n### Example\n\n### Example\n")
            page, _ = docs.build_html(root)
        for hid in ("readme:example", "readme:example-1", "readme:example-2"):
            self.assertIn(f'id="{hid}"', page)


class CliTests(Fixture):
    def run_main(self, *argv):
        # A real text stream over bytes, not a Mock: argparse (3.14+) asks stdout
        # whether it is a terminal, and main() writes bytes to stdout.buffer.
        out, err = io.BytesIO(), io.StringIO()
        fake_stdout = io.TextIOWrapper(out, encoding="utf-8")
        with mock.patch.object(sys, "stdout", fake_stdout), contextlib.redirect_stderr(err):
            rc = docs.main(list(argv))
        fake_stdout.flush()
        return rc, out.getvalue().decode("utf-8"), err.getvalue()

    def test_out_writes_atomically_and_prints_nothing(self):
        write(self.root, "README.md", "# T\n\nHi.\n")
        dest = self.root / "state" / "docs" / "index.html"
        dest.parent.mkdir(parents=True)
        dest.write_text("old")
        seen = []
        real_replace = os.replace

        def spy(src, dst):
            seen.append((Path(src), Path(dst)))
            self.assertEqual(dest.read_text(), "old")  # untouched until the rename
            return real_replace(src, dst)

        with mock.patch.object(docs.os, "replace", spy):
            rc, out, err = self.run_main("--root", str(self.root), "--out", str(dest))
        self.assertEqual((rc, out, err), (0, "", ""))
        self.assertTrue(dest.read_text().startswith("<!DOCTYPE html>"))
        self.assertEqual(len(seen), 1)
        self.assertEqual(seen[0][0].parent, dest.parent)
        self.assertEqual(sorted(p.name for p in dest.parent.iterdir()), ["index.html"])

    def test_failed_write_leaves_no_temp_file(self):
        write(self.root, "README.md", "# T\n")
        dest = self.root / "out" / "index.html"
        with mock.patch.object(docs.os, "replace", side_effect=OSError(13, "Permission denied", str(dest))):
            rc, out, err = self.run_main("--root", str(self.root), "--out", str(dest))
        self.assertEqual(rc, 1)
        self.assertEqual(len(err.strip().splitlines()), 1)
        self.assertEqual(list(dest.parent.iterdir()), [])

    def test_stdout_mode(self):
        write(self.root, "README.md", "# T\n\nHello **there**.\n")
        rc, out, err = self.run_main("--root", str(self.root))
        self.assertEqual((rc, err), (0, ""))
        self.assertTrue(out.startswith("<!DOCTYPE html>"))
        self.assertIn("Hello <strong>there</strong>.", out)

    def test_missing_readme(self):
        rc, out, err = self.run_main("--root", str(self.root))
        self.assertEqual((rc, out), (1, ""))
        self.assertEqual(len(err.strip().splitlines()), 1)
        self.assertIn("README.md not found", err)


class OutputTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.page, _ = docs.build_html(REPO)

    def test_no_network_references(self):
        page = self.page
        for tag in re.findall(r"<(?:script|link|img|iframe|source|video|audio|object|embed)\b[^>]*>", page, re.I):
            self.assertNotRegex(tag, r"(?i)(src|href)\s*=\s*[\"']?\s*(https?:)?//", tag)
        self.assertNotIn("@import", page)
        self.assertNotRegex(page, r"(?i)url\(\s*[\"']?\s*(https?:)?//")
        self.assertNotRegex(page, r"(?i)<link\b")
        self.assertNotRegex(page, r"(?i)<script[^>]*\ssrc=")

    def test_html_is_balanced(self):
        void = {"meta", "br", "hr", "input", "img", "link", "path", "rect", "circle"}
        problems = []

        class Checker(HTMLParser):
            stack = []

            def handle_starttag(self, tag, attrs):
                if tag not in void:
                    self.stack.append(tag)

            def handle_endtag(self, tag):
                if tag in void:
                    return
                if not self.stack or self.stack[-1] != tag:
                    problems.append((tag, self.getpos(), self.stack[-3:]))
                else:
                    self.stack.pop()

        c = Checker()
        c.feed(self.page)
        self.assertEqual(problems, [])
        self.assertEqual(c.stack, [])

    def test_theme_tokens_and_landmarks(self):
        page = self.page
        self.assertIn('@media (prefers-color-scheme: dark)', page)
        self.assertIn(':root:not([data-theme="light"])', page)
        self.assertIn(':root[data-theme="dark"]', page)
        self.assertIn("--accent: #0d6e7a", page)
        self.assertIn("prefers-reduced-motion", page)
        for landmark in ("<main", "<nav class=\"sidebar\"", "<header class=\"topbar\"", 'role="search"'):
            self.assertIn(landmark, page)
        self.assertIn("localStorage", page)
        self.assertIn("try{", page)

    def test_unique_ids(self):
        ids = re.findall(r'\sid="([^"]+)"', self.page)
        dupes = sorted({i for i in ids if ids.count(i) > 1})
        self.assertEqual(dupes, [])


if __name__ == "__main__":
    unittest.main()
