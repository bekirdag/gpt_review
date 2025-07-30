import json
from review import extract_patch

RAW = """
Here is your patch:

{
  "op": "update",
  "file": "demo.js",
  "body": "function foo() { return {a:1}; }",
  "status": "in_progress"
}

ASKING‑FOR‑CONTINUE
"""


def test_balanced_extraction():
    patch = extract_patch(RAW)
    assert patch is not None
    assert patch["file"] == "demo.js"
    assert patch["status"] == "in_progress"
    # ensure the body containing braces survives intact
    assert "{a:1}" in patch["body"]
