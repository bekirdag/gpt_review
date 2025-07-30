"""
GPT‑Review
~~~~~~~~~~

Browser‑driven code‑review assistant that lets ChatGPT patch a live
Git repository one file at a time.

Importing this module exposes the package version:

    >>> import gpt_review
    >>> gpt_review.__version__
"""
from importlib.metadata import version, PackageNotFoundError

try:
    __version__: str = version("gpt-review")
except PackageNotFoundError:  # local editable checkout
    __version__ = "0.1.0"
