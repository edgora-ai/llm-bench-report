"""Pure command builders and event parsers for the benchmark CLIs.

Use claude_code.command/summarize or opencode.command/summarize. Process
execution, stdin, isolated environments, and provider configuration belong to
the caller; importing this package performs no discovery or CLI invocation.
"""

from . import claude_code, opencode

__all__ = ["claude_code", "opencode"]
