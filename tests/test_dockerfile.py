"""The Dockerfile's shape: two targets, every base pinned by digest, and the
sbx stage last so a plain `docker build` (what ai-code-factory runs, with no
--target) keeps producing the sbx image. A text check, since the tests can't
assume Docker; CI builds both targets for real."""

import re
import unittest
from pathlib import Path

DOCKERFILE = (Path(__file__).parents[1] / "Dockerfile").read_text()
_FROM = re.compile(r"^FROM\s+(\S+)(?:\s+AS\s+(\S+))?\s*$", re.MULTILINE | re.IGNORECASE)


class DockerfileTests(unittest.TestCase):
    def stages(self):
        return [(image, name) for image, name in _FROM.findall(DOCKERFILE)]

    def test_every_base_image_is_pinned_by_digest(self):
        for image, name in self.stages():
            if image == "scratch":
                continue
            self.assertRegex(image, r"@sha256:[0-9a-f]{64}$", f"stage {name or image!r} must pin its base by digest")

    def test_has_standalone_and_sbx_targets_with_sbx_last(self):
        names = [name for _, name in self.stages()]
        self.assertIn("standalone", names)
        self.assertIn("sbx", names)
        self.assertEqual(names[-1], "sbx", "the sbx stage must stay last: ai-code-factory builds with no --target")

    def test_default_build_contexts_exist_as_stages(self):
        # A --build-context of the same name overrides these; without one,
        # the stage stands in, so `docker build` with no contexts works.
        names = [name for _, name in self.stages()]
        self.assertIn("factory-config", names)
        self.assertIn("plugins", names)
        self.assertIn("COPY --from=factory-config policy.toml", DOCKERFILE)
        self.assertIn("COPY --from=plugins / ", DOCKERFILE)

    def test_standalone_stage_sets_deployment_and_drops_root(self):
        start = DOCKERFILE.index(" AS standalone")
        end = DOCKERFILE.index(" AS sbx")
        stage = DOCKERFILE[start:end]
        self.assertIn("BROKER_DEPLOYMENT=standalone", stage)
        self.assertIsNotNone(re.search(r"^USER passdesk\s*$", stage, re.MULTILINE),
                             "the standalone stage must end as a non-root user")
        self.assertNotIn("USER root", stage)
        self.assertIn("/opt/passdesk", stage)


if __name__ == "__main__":
    unittest.main()
