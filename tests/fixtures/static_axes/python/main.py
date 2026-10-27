"""Fixture program.

The docstring and the comments are not code.
"""

import sys  # a trailing comment leaves this a code line


def main():
    # a full-line comment
    total = 0; count = 0; print(total, count, file=sys.stderr)
    print("a message long enough to pass the formatter's eighty-eight columns", total, count, sep=", ")


if __name__ == "__main__":
    main()
