"""Ids: unique, prefixed, time-ordered."""

import re

import pytest

from foreman.ids import KIND_PREFIXES, mint

PATTERN = re.compile(r"^[a-z]{3}-[a-z2-7]{7}$")


def test_ten_thousand_mints_unique_prefixed_ordered():
    ids = [mint("job") for _ in range(10_000)]
    assert len(set(ids)) == 10_000
    assert all(i.startswith("job-") for i in ids)
    assert all(PATTERN.match(i) for i in ids)
    assert ids == sorted(ids)


@pytest.mark.parametrize("kind,prefix", sorted(KIND_PREFIXES.items()))
def test_every_kind_mints_its_prefix(kind, prefix):
    assert len(prefix) == 3
    assert mint(kind).startswith(f"{prefix}-")


def test_unknown_kind_refused():
    with pytest.raises(KeyError):
        mint("starship")
