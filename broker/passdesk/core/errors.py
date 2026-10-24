class Denied(Exception):
    """A request refused by policy or content filtering."""


class Staged(Exception):
    """Not a refusal: a held write staged for a person's approval, which the
    gateway makes itself once approved (core/holds.py). `text` is the call's
    result for the agent, `cid` the approval id. The protocol layer returns
    it as an ordinary result, with `decision: staged` on the call's record."""

    decision = "staged"

    def __init__(self, text: str, cid: str):
        super().__init__(text)
        self.text, self.cid = text, cid


class Made(Staged):
    """Not a refusal either: the same held call again after the gateway made
    the approved write. `text` carries the write's stored result (the PR URL,
    the pushed ref); nothing new is staged or sent. The call's record says
    `decision: made`."""

    decision = "made"
