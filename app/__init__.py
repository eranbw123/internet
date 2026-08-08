"""`app` is a shorter alias for the `discovery` CLI -- nothing else lives here.

    python -m app score ...   ==   python -m discovery score ...

Both entry points run the same `discovery.__main__.main()`, so there is one
CLI to keep working, not two.
"""
