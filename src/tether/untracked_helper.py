"""dogfood-45 fixture: a new module whose contents reach the reviewer only
through the review gate's untracked-contents appendix."""


# This module is deliberately UNTRACKED in the working tree: the review gate
# has to read its contents out of the untracked-contents appendix, because a
# plain diff never carries a file's contents for a path git does not track.
def helper() -> int:
    """Return the canonical answer."""
    return 42
