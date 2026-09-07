from unittest.mock import Mock

from repeater.data_acquisition.storage_collector import StorageCollector


def test_packet_list_wrappers_forward_include_raw():
    collector = StorageCollector.__new__(StorageCollector)
    collector.sqlite_handler = Mock()
    collector.get_recent_packets(limit=25, include_raw=True)
    collector.sqlite_handler.get_recent_packets.assert_called_once_with(25, include_raw=True)
    collector.get_filtered_packets(limit=10, offset=5, cursor=(1.0, 2))
    collector.sqlite_handler.get_filtered_packets.assert_called_once_with(
        None, None, None, None, 10, 5, include_raw=False, cursor=(1.0, 2)
    )
