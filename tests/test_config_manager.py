from repeater.config_manager import ConfigManager


class _DummyRepeaterHandler:
    def __init__(self):
        self.radio_config = {}


class _DummySX1262Radio:
    def __init__(self):
        self.frequency = 868000000
        self.bandwidth = 125000
        self.spreading_factor = 7
        self.coding_rate = 5
        self.tx_power = 14
        self.calls = []

    def set_frequency(self, frequency):
        self.calls.append(("set_frequency", frequency))
        self.frequency = frequency
        return True

    def set_tx_power(self, power):
        self.calls.append(("set_tx_power", power))
        self.tx_power = power
        return True

    def set_spreading_factor(self, spreading_factor):
        self.calls.append(("set_spreading_factor", spreading_factor))
        self.spreading_factor = spreading_factor
        return True

    def set_bandwidth(self, bandwidth):
        self.calls.append(("set_bandwidth", bandwidth))
        self.bandwidth = bandwidth
        return True


class _DummyKissRadio:
    def __init__(self):
        self.radio_config = {
            "frequency": 869618000,
            "bandwidth": 62500,
            "spreading_factor": 8,
            "coding_rate": 8,
            "tx_power": 20,
        }
        self.calls = []

    def configure_radio(self, **kwargs):
        self.calls.append(("configure_radio", kwargs))
        self.frequency = kwargs["frequency"]
        self.bandwidth = kwargs["bandwidth"]
        self.spreading_factor = kwargs["spreading_factor"]
        self.coding_rate = kwargs["coding_rate"]
        self.tx_power = self.radio_config["tx_power"]
        return True


class _DummyDaemon:
    def __init__(self, config, radio):
        self.config = {
            "radio": dict(config.get("radio", {})),
            "kiss": dict(config.get("kiss", {})),
        }
        self.radio = radio
        self.repeater_handler = _DummyRepeaterHandler()
        self.advert_helper = None
        self.dispatcher = None


def test_live_update_daemon_applies_sx1262_radio_config():
    config = {
        "radio": {
            "frequency": 915000000,
            "bandwidth": 250000,
            "spreading_factor": 10,
            "coding_rate": 6,
            "tx_power": 20,
        }
    }
    radio = _DummySX1262Radio()
    daemon = _DummyDaemon(config, radio)
    manager = ConfigManager("/tmp/config.yaml", config, daemon)

    assert manager.live_update_daemon(["radio"])

    assert radio.calls == [
        ("set_frequency", 915000000),
        ("set_tx_power", 20),
        ("set_spreading_factor", 10),
        ("set_bandwidth", 250000),
    ]
    assert radio.coding_rate == 6
    assert daemon.repeater_handler.radio_config == config["radio"]


def test_live_update_daemon_applies_kiss_radio_config():
    config = {
        "radio": {
            "frequency": 915500000,
            "bandwidth": 125000,
            "spreading_factor": 9,
            "coding_rate": 7,
            "tx_power": 22,
        },
        "kiss": {
            "port": "/dev/ttyUSB0",
            "baud_rate": 115200,
        },
    }
    radio = _DummyKissRadio()
    daemon = _DummyDaemon(config, radio)
    manager = ConfigManager("/tmp/config.yaml", config, daemon)

    assert manager.live_update_daemon(["radio"])

    assert radio.calls == [
        (
            "configure_radio",
            {
                "frequency": 915500000,
                "bandwidth": 125000,
                "spreading_factor": 9,
                "coding_rate": 7,
            },
        )
    ]
    assert radio.radio_config == config["radio"]
    assert daemon.repeater_handler.radio_config == config["radio"]