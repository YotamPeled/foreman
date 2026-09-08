"""`python -m foreman` — the same entry point as the installed script.

Not `python -m foreman.cli`: run that way the module is imported twice, once
as __main__ and once as foreman.cli, and the verbs register in the copy that
does not build the parser.
"""

from .cli import main

if __name__ == "__main__":
    raise SystemExit(main())
