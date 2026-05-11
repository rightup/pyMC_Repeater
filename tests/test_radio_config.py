from repeater.config import get_radio_for_board


class _DummyRadio:
    _initialized = True


def test_get_radio_for_board_passes_en_pins(monkeypatch):
    captured_kwargs = {}

    class _DummySX1262Radio:
        @classmethod
        def get_instance(cls, **kwargs):
            captured_kwargs.update(kwargs)
            return _DummyRadio()

    monkeypatch.setattr(
        "pymc_core.hardware.sx1262_wrapper.SX1262Radio",
        _DummySX1262Radio,
    )

    board_config = {
        "radio_type": "sx1262",
        "sx1262": {
            "bus_id": 0,
            "cs_id": 0,
            "cs_pin": -1,
            "reset_pin": 18,
            "busy_pin": 5,
            "irq_pin": 6,
            "txen_pin": -1,
            "rxen_pin": -1,
            "en_pins": [26, 23],
        },
        "radio": {
            "frequency": 915000000,
            "tx_power": 22,
            "spreading_factor": 9,
            "bandwidth": 125000,
            "coding_rate": 5,
            "preamble_length": 17,
            "sync_word": 0x3444,
        },
    }

    get_radio_for_board(board_config)

    assert captured_kwargs["en_pins"] == [26, 23]
    assert "en_pin" not in captured_kwargs