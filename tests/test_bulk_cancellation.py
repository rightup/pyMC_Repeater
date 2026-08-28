import uuid

import pytest

from repeater.data_acquisition.bulk_cancellation import (
    BulkQueryCancelled,
    BulkQueryCapacity,
    BulkQueryConflict,
    BulkQueryRegistry,
    normalize_bulk_request_id,
)


def test_cancel_before_start_is_owner_scoped_and_idempotent():
    registry = BulkQueryRegistry()
    request_id = str(uuid.uuid4())
    registry.cancel("one", request_id)
    registry.cancel("one", request_id)
    with pytest.raises(BulkQueryCancelled):
        registry.register("one", request_id)
    other = registry.register("two", request_id)
    assert not other.is_set()
    registry.finish("two", request_id, other)


def test_active_cancellation_and_duplicate_id_do_not_replace_the_owner():
    registry = BulkQueryRegistry()
    event = registry.register("one", "id")
    with pytest.raises(BulkQueryConflict):
        registry.register("one", "id")
    registry.cancel("two", "id")
    assert not event.is_set()
    registry.cancel("one", "id")
    assert event.is_set()
    registry.finish("one", "id", event)
    with pytest.raises(BulkQueryCancelled):
        registry.register("one", "id")


def test_bounded_tombstones_expire_but_active_queries_do_not():
    now = [0]
    registry = BulkQueryRegistry(max_entries=2, tombstone_seconds=10, clock=lambda: now[0])
    active = registry.register("one", "active")
    registry.cancel("one", "pending")
    with pytest.raises(BulkQueryCapacity):
        registry.register("one", "other")
    with pytest.raises(BulkQueryCapacity):
        registry.cancel("one", "other")
    now[0] = 9
    registry.cancel("one", "pending")
    now[0] = 11
    with pytest.raises(BulkQueryCancelled):
        registry.register("one", "pending")
    now[0] = 20
    assert not registry.register("one", "pending").is_set()
    registry.cancel("one", "active")
    assert active.is_set()


def test_success_releases_capacity_and_late_cleanup_cannot_remove_new_request():
    registry = BulkQueryRegistry(max_entries=1)
    old = registry.register("one", "id")
    registry.finish("one", "id", old)
    new = registry.register("one", "id")
    registry.finish("one", "id", old)
    registry.cancel("one", "id")
    assert new.is_set()


@pytest.mark.parametrize("value", [None, [], "x" * 1000, str(uuid.uuid1()), "not-an-id"])
def test_request_ids_must_be_bounded_uuid4(value):
    with pytest.raises(ValueError, match="UUIDv4"):
        normalize_bulk_request_id(value)


def test_request_ids_are_normalized():
    request_id = str(uuid.uuid4())
    assert normalize_bulk_request_id(request_id.upper()) == request_id
