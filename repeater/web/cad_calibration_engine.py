import asyncio
import logging
import threading
import time
from typing import Any, Dict, Optional, Tuple

logger = logging.getLogger("HTTPServer")


class CADCalibrationEngine:
    def __init__(self, daemon_instance=None, event_loop=None):
        self.daemon_instance = daemon_instance
        self.event_loop = event_loop
        self.running = False
        self.results = {}
        self.current_test = None
        self.progress = {"current": 0, "total": 0}
        self.clients = set()  # SSE clients
        self.calibration_thread = None
        self.session_config: dict[str, Any] = {}

    @staticmethod
    def _default_thresholds_for_sf(spreading_factor: int) -> tuple[int, int]:
        defaults = {
            7: (22, 10),
            8: (22, 10),
            9: (24, 10),
            10: (25, 10),
            11: (26, 10),
            12: (30, 10),
        }
        return defaults.get(spreading_factor, defaults[8])

    @staticmethod
    def _normalize_int(value: Any, default: int, minimum: int, maximum: int) -> int:
        try:
            parsed = int(value)
        except (TypeError, ValueError):
            parsed = default
        return max(minimum, min(maximum, parsed))

    @staticmethod
    def _normalize_bool(value: Any, default: bool = False) -> bool:
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            lowered = value.strip().lower()
            if lowered in {"1", "true", "yes", "y", "on"}:
                return True
            if lowered in {"0", "false", "no", "n", "off"}:
                return False
        return default

    def _get_radio_runtime_config(self, radio) -> dict[str, Any]:
        config = getattr(self.daemon_instance, "config", {}) if self.daemon_instance else {}
        radio_cfg = config.get("radio", {})

        frequency = getattr(radio, "frequency", radio_cfg.get("frequency"))
        spreading_factor = getattr(radio, "spreading_factor", radio_cfg.get("spreading_factor", 8))
        bandwidth = getattr(radio, "bandwidth", radio_cfg.get("bandwidth", 125000))
        coding_rate = getattr(radio, "coding_rate", radio_cfg.get("coding_rate", 5))

        try:
            spreading_factor = int(spreading_factor)
        except (TypeError, ValueError):
            spreading_factor = 8
        try:
            bandwidth = int(bandwidth)
        except (TypeError, ValueError):
            bandwidth = 125000
        try:
            coding_rate = int(coding_rate)
        except (TypeError, ValueError):
            coding_rate = 5

        det_peak, det_min = self._default_thresholds_for_sf(spreading_factor)
        if hasattr(radio, "_get_thresholds_for_current_settings"):
            try:
                det_peak, det_min = radio._get_thresholds_for_current_settings()
            except Exception:
                logger.debug("Failed to read runtime CAD thresholds from radio", exc_info=True)

        return {
            "frequency": frequency,
            "spreading_factor": spreading_factor,
            "bandwidth": bandwidth,
            "coding_rate": coding_rate,
            "current_cad_peak": int(det_peak),
            "current_cad_min": int(det_min),
        }

    def get_test_ranges(self, spreading_factor: int, base_peak: int, base_min: int):
        """Get a small practical CAD test range around current/default values."""
        semtech_peak, semtech_min = self._default_thresholds_for_sf(spreading_factor)
        center_peak = int(base_peak if base_peak is not None else semtech_peak)
        center_min = int(base_min if base_min is not None else semtech_min)

        peak_candidates = {
            center_peak - 2,
            center_peak - 1,
            center_peak,
            center_peak + 1,
            center_peak + 2,
            semtech_peak,
        }
        min_candidates = {center_min - 1, center_min, center_min + 1, semtech_min}

        peak_values = sorted(v for v in peak_candidates if 1 <= v <= 255)
        min_values = sorted(v for v in min_candidates if 1 <= v <= 255)
        return peak_values, min_values

    @staticmethod
    def _build_stepped_range(
        lower: int, upper: int, step: int, anchor: Optional[int] = None
    ) -> list[int]:
        values = list(range(lower, upper + 1, max(1, step)))
        values.extend([lower, upper])
        if anchor is not None:
            values.append(anchor)
        return sorted({v for v in values if lower <= v <= upper})

    def _rank_results_for_search(
        self, results: list[dict], known_signal_present: bool, sf: int
    ) -> list[dict]:
        semtech_peak, semtech_min = self._default_thresholds_for_sf(sf)
        if known_signal_present:
            required_detection_rate = 85.0

            def _known_signal_sort_key(r: dict):
                detection_rate = float(r.get("detection_rate", 0.0) or 0.0)
                instability = int(r.get("timeouts", 0) or 0) + int(r.get("errors", 0) or 0)
                peak = int(r.get("det_peak", semtech_peak))
                min_val = int(r.get("det_min", semtech_min))
                attempts = int(r.get("attempts", 0) or r.get("samples", 0) or 0)
                aggressiveness_penalty = max(0, semtech_peak - peak) + (
                    2 * max(0, semtech_min - min_val)
                )
                is_stable = instability == 0
                meets_detection_floor = detection_rate >= required_detection_rate
                qualification_tier = (
                    2 if (is_stable and meets_detection_floor) else (1 if is_stable else 0)
                )
                return (
                    qualification_tier,
                    -aggressiveness_penalty,
                    min_val,
                    peak,
                    detection_rate,
                    -instability,
                    attempts,
                    int(r.get("detections", 0) or 0),
                )

            return sorted(
                results,
                key=_known_signal_sort_key,
                reverse=True,
            )

        return sorted(
            results,
            key=lambda r: (
                r.get("timeouts", 0) + r.get("errors", 0),
                abs(r.get("detection_rate", 0.0)),
                abs(r.get("det_peak", semtech_peak) - semtech_peak),
                abs(r.get("det_min", semtech_min) - semtech_min),
            ),
        )

    @staticmethod
    def _search_objective_value(result: dict, known_signal_present: bool) -> float:
        if known_signal_present:
            return float(result.get("detection_rate", 0.0)) - (
                float(result.get("timeouts", 0) + result.get("errors", 0)) * 5.0
            )
        return -(
            float(result.get("timeouts", 0) + result.get("errors", 0)) * 100.0
            + abs(float(result.get("detection_rate", 0.0)))
        )

    @staticmethod
    def _merge_cad_result_samples(base: dict, extra: dict) -> dict:
        merged = dict(base)
        attempts = int(base.get("attempts", 0) or 0) + int(extra.get("attempts", 0) or 0)
        detections = int(base.get("detections", 0) or 0) + int(extra.get("detections", 0) or 0)
        non_detections = int(base.get("non_detections", 0) or 0) + int(
            extra.get("non_detections", 0) or 0
        )
        timeouts = int(base.get("timeouts", 0) or 0) + int(extra.get("timeouts", 0) or 0)
        errors = int(base.get("errors", 0) or 0) + int(extra.get("errors", 0) or 0)
        cad_done_count = int(base.get("cad_done_count", 0) or 0) + int(
            extra.get("cad_done_count", 0) or 0
        )
        merged.update(
            {
                "samples": attempts,
                "attempts": attempts,
                "detections": detections,
                "non_detections": non_detections,
                "timeouts": timeouts,
                "errors": errors,
                "cad_done_count": cad_done_count,
                "detection_rate": (detections / attempts) * 100 if attempts > 0 else 0.0,
            }
        )
        return merged

    def _build_zoom_candidates(
        self,
        centers: list[dict],
        peak_radius: int,
        min_radius: int,
        *,
        peak_limit: tuple[int, int] = (1, 255),
        min_limit: tuple[int, int] = (1, 255),
    ) -> list[tuple[int, int]]:
        candidates: set[tuple[int, int]] = set()
        for center in centers:
            cp = int(center.get("det_peak", 22))
            cm = int(center.get("det_min", 10))
            peak_lower = max(peak_limit[0], cp - peak_radius)
            peak_upper = min(peak_limit[1], cp + peak_radius)
            min_lower = max(min_limit[0], cm - min_radius)
            min_upper = min(min_limit[1], cm + min_radius)
            for peak in range(peak_lower, peak_upper + 1):
                for min_val in range(min_lower, min_upper + 1):
                    candidates.add((peak, min_val))
        return sorted(candidates)

    async def test_cad_config(
        self,
        radio,
        det_peak: int,
        det_min: int,
        samples: int = 20,
        cad_symbol_num: int = 2,
        cad_timeout_seconds: float = 0.5,
    ) -> Dict[str, Any]:

        detections = 0
        non_detections = 0
        timeouts = 0
        errors = 0
        cad_done_count = 0
        attempts = 0

        for _ in range(samples):
            attempts += 1
            try:
                result = await radio.perform_cad(
                    det_peak=det_peak,
                    det_min=det_min,
                    timeout=cad_timeout_seconds,
                    calibration=True,
                    cad_symbol_num=cad_symbol_num,
                )
            except Exception as exc:
                logger.debug("CAD sample exception for peak=%s min=%s: %s", det_peak, det_min, exc)
                errors += 1
                await asyncio.sleep(0.02)
                continue

            if not isinstance(result, dict):
                result = {"detected": bool(result), "cad_done": True}

            if result.get("error"):
                errors += 1
            elif result.get("timeout"):
                timeouts += 1
            else:
                if bool(result.get("cad_done", False)):
                    cad_done_count += 1
                if bool(result.get("detected", False)):
                    detections += 1
                else:
                    non_detections += 1

            await asyncio.sleep(0.02)

        detection_rate = (detections / attempts) * 100 if attempts > 0 else 0.0

        return {
            "det_peak": det_peak,
            "det_min": det_min,
            "samples": attempts,
            "attempts": attempts,
            "detections": detections,
            "non_detections": non_detections,
            "timeouts": timeouts,
            "errors": errors,
            "cad_done_count": cad_done_count,
            "cad_symbol_num": cad_symbol_num,
            "detection_rate": detection_rate,
        }

    def _select_recommended_result(
        self, results: list[dict], known_signal_present: bool, sf: int
    ) -> Tuple[Optional[dict], str]:
        if not results:
            return None, "No calibration results collected."

        semtech_peak, semtech_min = self._default_thresholds_for_sf(sf)

        if known_signal_present:
            required_detection_rate = 95.0

            def _known_signal_sort_key(r: dict):
                detection_rate = float(r.get("detection_rate", 0.0) or 0.0)
                instability = int(r.get("timeouts", 0) or 0) + int(r.get("errors", 0) or 0)
                peak = int(r.get("det_peak", semtech_peak))
                min_val = int(r.get("det_min", semtech_min))
                attempts = int(r.get("attempts", 0) or r.get("samples", 0) or 0)
                aggressiveness_penalty = max(0, semtech_peak - peak) + (
                    2 * max(0, semtech_min - min_val)
                )
                is_stable = instability == 0
                meets_detection_floor = detection_rate >= required_detection_rate
                qualification_tier = (
                    2 if (is_stable and meets_detection_floor) else (1 if is_stable else 0)
                )
                return (
                    qualification_tier,
                    -aggressiveness_penalty,
                    min_val,
                    peak,
                    detection_rate,
                    -instability,
                    attempts,
                    int(r.get("detections", 0) or 0),
                )

            ranked = sorted(
                results,
                key=_known_signal_sort_key,
                reverse=True,
            )
            met_required = any(
                (float(r.get("detection_rate", 0.0) or 0.0) >= required_detection_rate)
                and (int(r.get("timeouts", 0) or 0) + int(r.get("errors", 0) or 0) == 0)
                for r in results
            )
            return (
                ranked[0],
                (
                    "Recommended using known-signal qualification-first selection "
                    f"(require ≥{required_detection_rate:.0f}% detection with zero timeouts/errors, "
                    "then choose the least-sensitive stable setting that meets it)."
                    if met_required
                    else "No candidate met the strict known-signal qualification floor; "
                    "selected the most stable least-sensitive fallback from available results."
                ),
            )

        ranked = sorted(
            results,
            key=lambda r: (
                r.get("timeouts", 0) + r.get("errors", 0),
                abs(r.get("detection_rate", 0.0)),
                abs(r.get("det_peak", semtech_peak) - semtech_peak),
                abs(r.get("det_min", semtech_min) - semtech_min),
            ),
        )
        return (
            ranked[0],
            "Recommended from no-known-signal run (minimize false CAD_DETECTED and instability). Validation with a known compatible LoRa transmission is still required.",
        )

    def broadcast_to_clients(self, data):

        # Store the message for clients to pick up
        self.last_message = data
        # Also store in a queue for clients to consume
        if not hasattr(self, "message_queue"):
            self.message_queue = []
        self.message_queue.append(data)

    def calibration_worker(self, samples: int, delay_ms: int):

        try:
            # Get radio from daemon instance
            if not self.daemon_instance:
                self.broadcast_to_clients(
                    {"type": "error", "message": "No daemon instance available"}
                )
                return

            radio = getattr(self.daemon_instance, "radio", None)
            if not radio:
                self.broadcast_to_clients(
                    {"type": "error", "message": "Radio instance not available"}
                )
                return
            if not hasattr(radio, "perform_cad"):
                self.broadcast_to_clients(
                    {"type": "error", "message": "Radio does not support CAD"}
                )
                return

            runtime_cfg = self._get_radio_runtime_config(radio)
            sf = runtime_cfg["spreading_factor"]
            base_peak = runtime_cfg["current_cad_peak"]
            base_min = runtime_cfg["current_cad_min"]
            known_signal_present = bool(self.session_config.get("known_signal_present", False))
            cad_symbol_num = int(self.session_config.get("cad_symbol_num", 2))
            cad_timeout_seconds = float(self.session_config.get("cad_timeout_seconds", 0.5))

            current = 0
            self.progress = {"current": 0, "total": 0}

            if self.event_loop:
                if known_signal_present:
                    semtech_peak, semtech_min = self._default_thresholds_for_sf(sf)
                    required_detection_rate = 85.0
                    evaluation_samples = max(30, min(60, samples * 3))
                    max_escalation_steps = min(12, max(0, semtech_peak - 1))
                    candidate_pairs = [
                        (max(1, semtech_peak - step), semtech_min)
                        for step in range(max_escalation_steps + 1)
                    ]
                    self.progress["total"] = len(candidate_pairs)

                    self.broadcast_to_clients(
                        {
                            "type": "status",
                            "message": (
                                "Calibration stage 1/2 (default baseline): testing Semtech default "
                                "thresholds first; escalate only if required."
                            ),
                            "test_ranges": {
                                "peak_min": candidate_pairs[-1][0],
                                "peak_max": candidate_pairs[0][0],
                                "min_min": semtech_min,
                                "min_max": semtech_min,
                                "spreading_factor": sf,
                                "bandwidth": runtime_cfg["bandwidth"],
                                "frequency": runtime_cfg["frequency"],
                                "current_peak": base_peak,
                                "current_min": base_min,
                                "cad_symbol_num": cad_symbol_num,
                                "known_signal_present": known_signal_present,
                                "total_tests": len(candidate_pairs),
                                "pass_index": 1,
                                "max_passes": 2,
                                "stage": "default-anchor",
                            },
                        }
                    )

                    qualified_candidate: Optional[dict] = None
                    for index, (det_peak, det_min) in enumerate(candidate_pairs, start=1):
                        if not self.running:
                            break

                        current = index
                        self.progress["current"] = current
                        self.broadcast_to_clients(
                            {
                                "type": "progress",
                                "current": current,
                                "total": len(candidate_pairs),
                                "det_peak": det_peak,
                                "det_min": det_min,
                                "known_signal_present": known_signal_present,
                                "pass_index": 1,
                                "max_passes": 2,
                                "stage": "default-anchor",
                            }
                        )

                        future = asyncio.run_coroutine_threadsafe(
                            self.test_cad_config(
                                radio,
                                det_peak,
                                det_min,
                                samples=evaluation_samples,
                                cad_symbol_num=cad_symbol_num,
                                cad_timeout_seconds=cad_timeout_seconds,
                            ),
                            self.event_loop,
                        )

                        try:
                            result = future.result(timeout=45)
                            self.results[f"{det_peak}-{det_min}"] = result
                            self.broadcast_to_clients(
                                {
                                    "type": "result",
                                    "pass_index": 1,
                                    "stage": "default-anchor",
                                    **result,
                                }
                            )

                            instability = int(result.get("timeouts", 0) or 0) + int(
                                result.get("errors", 0) or 0
                            )
                            detection_rate = float(result.get("detection_rate", 0.0) or 0.0)
                            if instability == 0 and detection_rate >= required_detection_rate:
                                qualified_candidate = result
                                self.broadcast_to_clients(
                                    {
                                        "type": "status",
                                        "message": (
                                            f"Qualification met at P{det_peak}/M{det_min} "
                                            f"(rate {detection_rate:.1f}%, stable). "
                                            "Stopping escalation at first qualifying candidate."
                                        ),
                                    }
                                )
                                break
                        except Exception as e:
                            logger.error(f"CAD test failed for peak={det_peak}, min={det_min}: {e}")

                        if self.running and delay_ms > 0:
                            time.sleep(delay_ms / 1000.0)

                    if self.running and self.results and qualified_candidate is None:
                        self.broadcast_to_clients(
                            {
                                "type": "status",
                                "message": (
                                    "No candidate met strict qualification floor "
                                    f"(≥{required_detection_rate:.0f}% with zero timeouts/errors). "
                                    "Using least-sensitive stable fallback from tested candidates."
                                ),
                            }
                        )
                else:
                    # Quiet-mode keeps the previous coarse-to-fine search behaviour.
                    coarse_peak_lower = max(1, int(base_peak) - 12)
                    coarse_peak_upper = min(255, int(base_peak) + 12)
                    coarse_min_lower = max(1, int(base_min) - 5)
                    coarse_min_upper = min(255, int(base_min) + 5)
                    max_total_tests = 84
                    estimated_total = 0
                    self.progress = {"current": 0, "total": estimated_total}
                    stage_definitions: list[dict[str, Any]] = [
                        {
                            "stage_key": "coarse",
                            "label": "coarse scan",
                            "builder": lambda: [
                                (peak, min_val)
                                for peak in self._build_stepped_range(
                                    coarse_peak_lower, coarse_peak_upper, 4, anchor=int(base_peak)
                                )
                                for min_val in self._build_stepped_range(
                                    coarse_min_lower, coarse_min_upper, 2, anchor=int(base_min)
                                )
                            ],
                        },
                        {
                            "stage_key": "zoom1",
                            "label": "zoom refinement 1",
                            "builder": lambda: self._build_zoom_candidates(
                                self._rank_results_for_search(
                                    list(self.results.values()), known_signal_present, sf
                                )[:3],
                                peak_radius=4,
                                min_radius=2,
                            ),
                        },
                        {
                            "stage_key": "zoom2",
                            "label": "zoom refinement 2",
                            "builder": lambda: self._build_zoom_candidates(
                                self._rank_results_for_search(
                                    list(self.results.values()), known_signal_present, sf
                                )[:2],
                                peak_radius=2,
                                min_radius=1,
                            ),
                        },
                        {
                            "stage_key": "fine",
                            "label": "fine polish",
                            "builder": lambda: self._build_zoom_candidates(
                                self._rank_results_for_search(
                                    list(self.results.values()), known_signal_present, sf
                                )[:1],
                                peak_radius=1,
                                min_radius=1,
                            ),
                        },
                    ]
                    best_score_before_stage: Optional[float] = None
                    for stage_index, stage in enumerate(stage_definitions, start=1):
                        if not self.running or current >= max_total_tests:
                            break

                        raw_candidates: list[tuple[int, int]] = stage["builder"]()
                        candidates = [
                            candidate
                            for candidate in raw_candidates
                            if f"{candidate[0]}-{candidate[1]}" not in self.results
                        ]
                        remaining_budget = max_total_tests - current
                        candidates = candidates[:remaining_budget]
                        if not candidates:
                            continue

                        estimated_total = max(
                            self.progress.get("total", 0), current + len(candidates)
                        )
                        self.progress["total"] = estimated_total

                        peak_values = [candidate[0] for candidate in candidates]
                        min_values = [candidate[1] for candidate in candidates]
                        self.broadcast_to_clients(
                            {
                                "type": "status",
                                "message": (
                                    f"Calibration stage {stage_index}/{len(stage_definitions)} "
                                    f"({stage['label']}): testing {len(candidates)} combinations"
                                ),
                                "test_ranges": {
                                    "peak_min": min(peak_values),
                                    "peak_max": max(peak_values),
                                    "min_min": min(min_values),
                                    "min_max": max(min_values),
                                    "spreading_factor": sf,
                                    "bandwidth": runtime_cfg["bandwidth"],
                                    "frequency": runtime_cfg["frequency"],
                                    "current_peak": base_peak,
                                    "current_min": base_min,
                                    "cad_symbol_num": cad_symbol_num,
                                    "known_signal_present": known_signal_present,
                                    "total_tests": estimated_total,
                                    "pass_index": stage_index,
                                    "max_passes": len(stage_definitions),
                                    "stage": stage["stage_key"],
                                },
                            }
                        )

                        for det_peak, det_min in candidates:
                            if not self.running:
                                break

                            current += 1
                            self.progress["current"] = current

                            self.broadcast_to_clients(
                                {
                                    "type": "progress",
                                    "current": current,
                                    "total": estimated_total,
                                    "det_peak": det_peak,
                                    "det_min": det_min,
                                    "known_signal_present": known_signal_present,
                                    "pass_index": stage_index,
                                    "max_passes": len(stage_definitions),
                                    "stage": stage["stage_key"],
                                }
                            )

                            future = asyncio.run_coroutine_threadsafe(
                                self.test_cad_config(
                                    radio,
                                    det_peak,
                                    det_min,
                                    samples=samples,
                                    cad_symbol_num=cad_symbol_num,
                                    cad_timeout_seconds=cad_timeout_seconds,
                                ),
                                self.event_loop,
                            )

                            try:
                                result = future.result(timeout=30)
                                self.results[f"{det_peak}-{det_min}"] = result
                                self.broadcast_to_clients(
                                    {
                                        "type": "result",
                                        "pass_index": stage_index,
                                        "stage": stage["stage_key"],
                                        **result,
                                    }
                                )
                            except Exception as e:
                                logger.error(
                                    f"CAD test failed for peak={det_peak}, min={det_min}: {e}"
                                )

                            if self.running and delay_ms > 0:
                                time.sleep(delay_ms / 1000.0)

                        if not self.running or not self.results:
                            break

                        ranked_results = self._rank_results_for_search(
                            list(self.results.values()), known_signal_present, sf
                        )
                        best_score_after_stage = self._search_objective_value(
                            ranked_results[0], known_signal_present
                        )
                        min_improvement = 1.0
                        if (
                            stage_index >= 2
                            and best_score_before_stage is not None
                            and (best_score_after_stage - best_score_before_stage) < min_improvement
                        ):
                            self.broadcast_to_clients(
                                {
                                    "type": "status",
                                    "message": (
                                        f"Calibration converged after {stage['label']} "
                                        f"(improvement < {min_improvement:.0f}%)."
                                    ),
                                }
                            )
                            break
                        best_score_before_stage = best_score_after_stage

                    # Adaptive confidence pass remains for quiet-mode only.
                    if self.running and self.results:
                        ranked_for_verify = self._rank_results_for_search(
                            list(self.results.values()), known_signal_present, sf
                        )
                        finalist_count = min(3, len(ranked_for_verify))
                        target_samples = max(30, min(60, samples * 3))
                        extra_samples = max(0, target_samples - samples)
                        if finalist_count > 0 and extra_samples > 0:
                            self.broadcast_to_clients(
                                {
                                    "type": "status",
                                    "message": (
                                        f"Verification pass: re-testing top {finalist_count} "
                                        f"candidates with +{extra_samples} samples each."
                                    ),
                                }
                            )
                            for index, candidate in enumerate(
                                ranked_for_verify[:finalist_count], start=1
                            ):
                                if not self.running:
                                    break
                                det_peak = int(candidate.get("det_peak", 22))
                                det_min = int(candidate.get("det_min", 10))
                                self.broadcast_to_clients(
                                    {
                                        "type": "status",
                                        "message": (
                                            f"Verification {index}/{finalist_count}: "
                                            f"P{det_peak}/M{det_min}"
                                        ),
                                    }
                                )
                                future = asyncio.run_coroutine_threadsafe(
                                    self.test_cad_config(
                                        radio,
                                        det_peak,
                                        det_min,
                                        samples=extra_samples,
                                        cad_symbol_num=cad_symbol_num,
                                        cad_timeout_seconds=cad_timeout_seconds,
                                    ),
                                    self.event_loop,
                                )
                                try:
                                    verification_result = future.result(timeout=30)
                                    result_key = f"{det_peak}-{det_min}"
                                    base_result = self.results.get(result_key, candidate)
                                    merged_result = self._merge_cad_result_samples(
                                        base_result, verification_result
                                    )
                                    self.results[result_key] = merged_result
                                    self.broadcast_to_clients(
                                        {
                                            "type": "result",
                                            "stage": "verification",
                                            **merged_result,
                                        }
                                    )
                                except Exception as e:
                                    logger.error(
                                        "CAD finalist verification failed for peak=%s, min=%s: %s",
                                        det_peak,
                                        det_min,
                                        e,
                                    )

            if self.running:
                best_result = None
                recommended_result = None
                recommendation_reason = "No recommendation generated."
                signal_activity_observed = False
                known_signal_effective = known_signal_present
                quiet_mode_invalid = False
                quiet_mode_invalid_reason = ""
                aggregate_detection_rate = 0.0
                qualification = (
                    "Known compatible LoRa signal present during calibration."
                    if known_signal_present
                    else "No known compatible LoRa signal confirmed during calibration."
                )
                if self.results:
                    all_results = list(self.results.values())
                    best_result = max(all_results, key=lambda x: x.get("detection_rate", 0.0))
                    recommended_result, recommendation_reason = self._select_recommended_result(
                        all_results, known_signal_present=known_signal_present, sf=sf
                    )
                    total_attempts = sum(int(r.get("attempts", 0) or 0) for r in all_results)
                    total_detections = sum(int(r.get("detections", 0) or 0) for r in all_results)
                    best_rate = float(best_result.get("detection_rate", 0.0) or 0.0)
                    aggregate_detection_rate = (
                        (float(total_detections) / float(total_attempts)) * 100.0
                        if total_attempts > 0
                        else 0.0
                    )
                    min_detection_floor = max(5, int(total_attempts * 0.03))
                    signal_activity_observed = (
                        total_detections >= min_detection_floor and best_rate >= 15.0
                    )
                    known_signal_effective = known_signal_present or signal_activity_observed
                    if not known_signal_present and (
                        signal_activity_observed or aggregate_detection_rate > 10.0
                    ):
                        quiet_mode_invalid = True
                        quiet_mode_invalid_reason = (
                            "Quiet-mode run observed significant channel activity "
                            f"(aggregate CAD detection {aggregate_detection_rate:.1f}%). "
                            "Re-run quiet baseline during a truly idle channel."
                        )

                    if not known_signal_present and signal_activity_observed:
                        qualification = (
                            "Signal activity was observed during quiet-mode calibration, "
                            "but known-signal mode was not explicitly enabled."
                        )

                self.broadcast_to_clients(
                    {
                        "type": "completed",
                        "message": "Calibration completed",
                        "results": (
                            {
                                "best": best_result,
                                "recommended": recommended_result,
                                "recommendation_reason": recommendation_reason,
                                "known_signal_present": known_signal_present,
                                "signal_activity_observed": signal_activity_observed,
                                "known_signal_effective": known_signal_effective,
                                "quiet_mode_invalid": quiet_mode_invalid,
                                "quiet_mode_invalid_reason": quiet_mode_invalid_reason,
                                "aggregate_detection_rate": aggregate_detection_rate,
                                "qualification": qualification,
                                "total_tests": len(self.results),
                            }
                            if best_result
                            else None
                        ),
                    }
                )
            else:
                self.broadcast_to_clients({"type": "status", "message": "Calibration stopped"})

        except Exception as e:
            logger.error(f"Calibration worker error: {e}")
            self.broadcast_to_clients({"type": "error", "message": str(e)})
        finally:
            self.running = False

    def start_calibration(self, samples: int = 8, delay_ms: int = 100):

        if self.running:
            return False
        samples = self._normalize_int(samples, default=8, minimum=1, maximum=64)
        delay_ms = self._normalize_int(delay_ms, default=100, minimum=0, maximum=2000)
        known_signal_present = self._normalize_bool(
            self.session_config.get("known_signal_present", False), default=False
        )
        cad_symbol_num = self._normalize_int(
            self.session_config.get("cad_symbol_num", 2), default=2, minimum=1, maximum=16
        )
        if cad_symbol_num not in {1, 2, 4, 8, 16}:
            cad_symbol_num = 2
        cad_timeout_ms = self._normalize_int(
            self.session_config.get("cad_timeout_ms", 500), default=500, minimum=50, maximum=5000
        )

        self.session_config = {
            "known_signal_present": known_signal_present,
            "cad_symbol_num": cad_symbol_num,
            "cad_timeout_ms": cad_timeout_ms,
            "cad_timeout_seconds": cad_timeout_ms / 1000.0,
        }

        self.running = True
        self.results.clear()
        self.progress = {"current": 0, "total": 0}
        self.clear_message_queue()  # Clear any old messages

        # Start calibration in separate thread
        self.calibration_thread = threading.Thread(
            target=self.calibration_worker, args=(samples, delay_ms)
        )
        self.calibration_thread.daemon = True
        self.calibration_thread.start()

        return True

    def stop_calibration(self):

        self.running = False
        if self.calibration_thread:
            self.calibration_thread.join(timeout=2)

    def clear_message_queue(self):

        if hasattr(self, "message_queue"):
            self.message_queue.clear()
