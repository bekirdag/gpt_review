import json
import pytest
from patch_validator import validate_patch

GOOD = {
    "op": "create",
    "file": "foo.py",
    "body": "print('hi')",
    "status": "completed",
}
BAD_STATUS = {**GOOD, "status": "done"}  # invalid enum
MISSING_STATUS = {k: v for k, v in GOOD.items() if k != "status"}


def test_validate_good():
    assert validate_patch(json.dumps(GOOD)) == GOOD


@pytest.mark.parametrize("patch", [BAD_STATUS, MISSING_STATUS])
def test_validate_bad(patch):
    with pytest.raises(Exception):
        validate_patch(json.dumps(patch))
