class Denied(Exception):
    """A request refused by policy or content filtering."""


class Staged(Exception):
    """Not a refusal: a held write staged for a person's approval, which the
    gateway makes itself once approved (core/holds.py). `text` is the call's
    result for the agent, `cid` the approval id. The protocol layer returns
    it as an ordinary result, with `decision: staged` on the call's record."""

    def __init__(self, text: str, cid: str):
        super().__init__(text)
        self.text, self.cid = text, cid
