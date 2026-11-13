"""Fetch daily metrics from Garmin Connect and parse into flat dicts."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _ms_to_gmt(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.0")


# ---------------------------------------------------------------------------
# Parsers — daily summary fields
# ---------------------------------------------------------------------------

def _parse_sleep(data: dict | None, hrv_summary: dict | None) -> dict:
    if not data:
        return {}

    dto: dict = data.get("dailySleepDTO") or {}
    spo2_dto: dict = data.get("wellnessSpO2SummaryDTO") or {}
    scores: dict = dto.get("sleepScores") or {}
    overall: dict = scores.get("overall") or {}
    spo2 = dto.get("averageSpO2Value") or spo2_dto.get("averageSpO2")

    sleep_hrv = None
    if hrv_summary:
        sleep_hrv = (hrv_summary.get("hrvSummary") or {}).get("lastNightAvg")

    return {
        "sleep_score": overall.get("value"),
        "sleep_duration_seconds": dto.get("sleepTimeSeconds"),
        "sleep_deep_seconds": dto.get("deepSleepSeconds"),
        "sleep_light_seconds": dto.get("lightSleepSeconds"),
        "sleep_rem_seconds": dto.get("remSleepSeconds"),
        "sleep_awake_seconds": dto.get("awakeSleepSeconds"),
        "sleep_hrv": sleep_hrv,
        "sleep_spo2": spo2,
        "stress_rest": dto.get("avgSleepStress"),
    }


def _parse_stress(data: dict | None) -> dict:
    if not data:
        return {}
    return {
        "stress_avg": data.get("avgStressLevel"),
        "stress_max": data.get("maxStressLevel"),
    }


def _parse_body_battery(data: list | None) -> dict:
    if not data:
        return {}
    entry: dict = data[0] if data else {}
    return {
        "body_battery_start": entry.get("startBodyBatteryLevel"),
        "body_battery_end": entry.get("endBodyBatteryLevel"),
    }


def _parse_heart_rate(data: dict | None) -> dict:
    if not data:
        return {}
    return {"resting_hr": data.get("restingHeartRate")}


def _parse_stats(data: dict | None) -> dict:
    if not data:
        return {}
    return {
        "steps": data.get("totalSteps"),
        "active_calories": data.get("activeKilocalories"),
    }


# ---------------------------------------------------------------------------
# Parsers — timeseries
# ---------------------------------------------------------------------------

_ACTIVITY_LEVEL_TO_STAGE = {0.0: "deep", 1.0: "light", 2.0: "rem", 3.0: "awake"}


def _parse_sleep_stages(data: dict | None) -> list[dict[str, str]]:
    if not data:
        return []
    return [
        {
            "start_gmt": entry["startGMT"],
            "end_gmt": entry["endGMT"],
            "stage": _ACTIVITY_LEVEL_TO_STAGE.get(entry["activityLevel"], "unknown"),
        }
        for entry in (data.get("sleepLevels") or [])
    ]


def _parse_hr_readings(date: str, data: dict | None) -> list[dict]:
    if not data:
        return []
    return [
        {"timestamp_gmt": _ms_to_gmt(entry[0]), "date": date, "bpm": entry[1]}
        for entry in (data.get("heartRateValues") or [])
        if entry[1] is not None
    ]


def _parse_stress_readings(date: str, data: dict | None) -> list[dict]:
    if not data:
        return []
    return [
        {"timestamp_gmt": _ms_to_gmt(entry[0]), "date": date, "stress_level": entry[1]}
        for entry in (data.get("stressValuesArray") or [])
        if entry[1] is not None and entry[1] >= 0  # -1 = no reading
    ]


def _parse_hrv_readings(date: str, data: dict | None) -> list[dict]:
    if not data:
        return []
    return [
        {"timestamp_gmt": entry["readingTimeGMT"], "date": date, "hrv_ms": entry["hrvValue"]}
        for entry in (data.get("hrvReadings") or [])
        if entry.get("hrvValue") is not None
    ]


def _parse_activity(act: dict) -> dict:
    start_local: str = act.get("startTimeLocal") or ""
    return {
        "activity_id": act.get("activityId"),
        "date": start_local[:10],
        "name": act.get("activityName"),
        "type": (act.get("activityType") or {}).get("typeKey"),
        "start_gmt": act.get("startTimeGMT"),
        "end_gmt": act.get("endTimeGMT"),
        "duration_seconds": act.get("duration"),
        "calories": act.get("calories"),
        "avg_hr": act.get("averageHR"),
        "max_hr": act.get("maxHR"),
        "aerobic_training_effect": act.get("aerobicTrainingEffect"),
        "anaerobic_training_effect": act.get("anaerobicTrainingEffect"),
        "training_load": act.get("activityTrainingLoad"),
        "training_effect_label": act.get("trainingEffectLabel"),
        "body_battery_impact": act.get("differenceBodyBattery"),
        "hr_zone_1_seconds": act.get("hrTimeInZone_1"),
        "hr_zone_2_seconds": act.get("hrTimeInZone_2"),
        "hr_zone_3_seconds": act.get("hrTimeInZone_3"),
        "hr_zone_4_seconds": act.get("hrTimeInZone_4"),
        "hr_zone_5_seconds": act.get("hrTimeInZone_5"),
        "moderate_intensity_minutes": act.get("moderateIntensityMinutes"),
        "vigorous_intensity_minutes": act.get("vigorousIntensityMinutes"),
    }


# ---------------------------------------------------------------------------
# Workout daily summary (aggregated for daily_metrics)
# ---------------------------------------------------------------------------

def _group_activities_by_date(activities: list[dict]) -> dict[str, list[dict]]:
    by_date: dict[str, list[dict]] = {}
    for act in activities:
        start_local: str = act.get("startTimeLocal") or ""
        day = start_local[:10]
        if day:
            by_date.setdefault(day, []).append(act)
    return by_date


def _parse_workouts(activities: list[dict]) -> dict:
    if not activities:
        return {
            "workout_types": None,
            "workout_duration_seconds": None,
            "workout_training_load": None,
        }

    types: list[str] = []
    total_duration = 0
    total_load = 0.0
    has_load = False

    for act in activities:
        type_key: str = (act.get("activityType") or {}).get("typeKey") or "unknown"
        types.append(type_key)
        total_duration += int(act.get("duration") or 0)
        load = act.get("activityTrainingLoad")
        if load is not None:
            total_load += float(load)
            has_load = True

    return {
        "workout_types": ",".join(types),
        "workout_duration_seconds": total_duration or None,
        "workout_training_load": total_load if has_load else None,
    }
