import pytest

from repeater.web.cad_calibration_engine import CADCalibrationEngine


class _FakeRadio:
    def __init__(self, responses):
        self._responses = list(responses)
        self.frequency = 868000000
        self.spreading_factor = 8
        self.bandwidth = 125000
        self.coding_rate = 5

    async def perform_cad(self, **kwargs):
        if not self._responses:
            return {"cad_done": True, "detected": False}
        return self._responses.pop(0)


@pytest.mark.asyncio
async def test_test_cad_config_aggregates_detected_non_detected_timeout_error_counts():
    engine = CADCalibrationEngine()
    radio = _FakeRadio(
        [
            {"cad_done": True, "detected": True},
            {"cad_done": True, "detected": False},
            {"timeout": True, "detected": False},
            {"error": "spi fault", "detected": False},
            {"cad_done": True, "detected": True},
        ]
    )

    result = await engine.test_cad_config(
        radio, det_peak=22, det_min=10, samples=5, cad_symbol_num=2, cad_timeout_seconds=0.1
    )

    assert result["attempts"] == 5
    assert result["detections"] == 2
    assert result["non_detections"] == 1
    assert result["timeouts"] == 1
    assert result["errors"] == 1
    assert result["cad_done_count"] == 3


def test_get_test_ranges_is_small_and_centered():
    engine = CADCalibrationEngine()
    peaks, mins = engine.get_test_ranges(spreading_factor=8, base_peak=22, base_min=10)
    assert peaks == [20, 21, 22, 23, 24]
    assert mins == [9, 10, 11]


def test_select_recommended_result_known_signal_prefers_higher_detection_rate():
    engine = CADCalibrationEngine()
    results = [
        {"det_peak": 22, "det_min": 10, "detection_rate": 20.0, "timeouts": 0, "errors": 0},
        {"det_peak": 23, "det_min": 10, "detection_rate": 60.0, "timeouts": 0, "errors": 0},
    ]
    recommended, reason = engine._select_recommended_result(
        results, known_signal_present=True, sf=8
    )
    assert recommended["det_peak"] == 23
    assert "known-signal" in reason


def test_select_recommended_result_no_signal_prefers_low_false_detections_and_stability():
    engine = CADCalibrationEngine()
    results = [
        {"det_peak": 22, "det_min": 10, "detection_rate": 0.0, "timeouts": 0, "errors": 0},
        {"det_peak": 23, "det_min": 10, "detection_rate": 40.0, "timeouts": 0, "errors": 0},
        {"det_peak": 24, "det_min": 10, "detection_rate": 0.0, "timeouts": 2, "errors": 0},
    ]
    recommended, reason = engine._select_recommended_result(
        results, known_signal_present=False, sf=8
    )
    assert recommended["det_peak"] == 22
    assert "Validation with a known compatible LoRa transmission is still required." in reason
