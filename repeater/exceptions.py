"""Shared exception types for the repeater.

Kept dependency-free (no other ``repeater`` imports) so any module can raise
these without risking an import cycle.
"""


class ConfigurationError(RuntimeError):
    """A user-actionable configuration problem.

    Raised for boot-time config mistakes (missing/invalid config file, missing
    required keys, colliding local identities).  ``main()`` reports the message
    and exits non-zero *without* a stack trace, since the fix is in the config,
    not the code.  Subclasses ``RuntimeError`` so existing ``except
    RuntimeError`` sites keep catching the errors that used to be raised as
    plain ``RuntimeError``.
    """
