"""
Entry‑point shim so you can run:

    python -m gpt_review instructions.txt /path/to/repo [--auto]
"""
from review import main  # re‑export

if __name__ == "__main__":  # pragma: no cover
    main()
