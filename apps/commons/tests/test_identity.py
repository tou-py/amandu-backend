import uuid

from apps.commons.mixins import PublicIdentifierMixin


def new_ids(count):
    """The field's default is the whole mixin; no table needed to exercise it."""
    field = PublicIdentifierMixin._meta.get_field('id')
    return [field.get_default() for _ in range(count)]


def test_ids_are_uuid7_and_time_ordered():
    """
    The regression this guards is someone swapping uuid7 back to uuid4 because it
    looks more familiar. uuid4 would still pass a "is it a UUID" test and would still
    work in development -- and would quietly destroy insert locality on the primary
    key index once the table no longer fits in memory.
    """
    ids = new_ids(2000)

    assert all(isinstance(i, uuid.UUID) for i in ids)
    assert {i.version for i in ids} == {7}
    assert ids == sorted(ids)


def test_ids_are_unique():
    ids = new_ids(2000)

    assert len(set(ids)) == len(ids)
