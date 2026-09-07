from unittest.mock import Mock

from repeater.data_acquisition.storage_collector import StorageCollector


def test_forwards_bucket_seconds():
    collector = StorageCollector.__new__(StorageCollector)
    collector.sqlite_handler = Mock()
    collector.get_neighbor_link_history(peer_hash="2A", path_hash_size=1, bucket_seconds=600)
    collector.sqlite_handler.get_neighbor_link_history.assert_called_once_with(
        peer_hash="2A", path_hash_size=1, hours=24, limit=1000, bucket_seconds=600
    )
