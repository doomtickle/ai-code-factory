class Denied(Exception):
    """A request refused by policy or content filtering."""


class Queued(Exception):
    """Not a refusal: a held write that will be completed later by the
    gateway itself (core/holds.py), or was already. `text` is the call's
    result for the agent, `cid` the approval id and `state` what the audit
    record's decision says ("queued", or "completed" for a retry after the
    gateway wrote it). The protocol layer returns it as an ordinary result."""

    def __init__(self, text: str, cid: str, state: str = "queued"):
        super().__init__(text)
        self.text, self.cid, self.state = text, cid, state
