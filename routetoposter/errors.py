"""The one exception type the user sees as a plain message."""


class UserError(Exception):
    """A problem the user can fix: a bad link, a typo in the trip file, a sight that can't be found.
    The command line prints the message (no traceback) and exits with code 1."""
