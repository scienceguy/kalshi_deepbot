"""The credential timestamp must be ISO-8601 that TypeORM can parse."""

import datetime
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "scripts"))

from n8n_creds_sync import iso_now  # noqa: E402


def test_iso_now_is_parseable_and_not_double_suffixed():
    ts = iso_now()
    assert ts.endswith("Z")
    assert "+00:00" not in ts, "TypeORM rejects '…+00:00Z' (broke 41 rows once)"
    datetime.datetime.fromisoformat(ts.replace("Z", "+00:00"))
