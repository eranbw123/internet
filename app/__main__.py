"""python -m app <command> -> discovery.__main__.main()."""
import sys

from discovery.__main__ import main

if __name__ == "__main__":
    sys.exit(main())
