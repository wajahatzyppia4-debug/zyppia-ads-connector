import time

import pytest
from cryptography.fernet import Fernet

from app.ads import AdsError, clean_customer_id, pretty_id, row_to_dict
from app.config import Settings
from app.sealing import Sealer


def test_customer_ids():
    assert clean_customer_id("399-355-6480") == "3993556480"
    assert pretty_id("3993556480") == "399-355-6480"
    with pytest.raises(AdsError):
        clean_customer_id("123")


def test_sealer_kinds_and_expiry():
    s = Sealer(Fernet.generate_key().decode())
    t = s.seal("at", {"a": 1}, ttl=60)
    assert s.unseal("at", t) == {"a": 1}
    assert s.unseal("rt", t) is None  # wrong kind
    assert s.unseal("at", t[:-3] + "AAA") is None  # tampered
    old = s.seal("at", {"a": 1}, ttl=-1)
    assert s.unseal("at", old) is None


def test_email_allowlist():
    st = Settings("https://x", "a", "b", Fernet.generate_key().decode(), allowed_emails=["a@x.com", "@zyppia.com"])
    assert st.email_allowed("A@x.com")
    assert st.email_allowed("bob@zyppia.com")
    assert not st.email_allowed("bob@evilzyppia.com.au")
    assert not st.email_allowed(None)


def test_row_to_dict_micros():
    class M:
        cost_micros = 12_345_678
        clicks = 3

    class Row:
        metrics = M()

    d = row_to_dict(Row(), ["metrics.cost_micros", "metrics.clicks"])
    assert d == {"metrics.cost_micros": 12_345_678, "metrics.cost": 12.35, "metrics.clicks": 3}
