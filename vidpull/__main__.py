"""Allow ``python -m vidpull``."""

from vidpull.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
