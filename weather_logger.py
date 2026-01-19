#!/usr/bin/env python3
"""Weather logger using scraped data from weather.com.

This script downloads Weather Channel forecast pages for a given location,
extracts the embedded application state, persists a snapshot of the nearest
hourly forecast with the current observation, and stores optional multi-day
forecasts in SQLite. The default configuration targets Denver, Colorado.
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional, Tuple

import requests

DEFAULT_LOCATION_ID = "USCO0105:1:US"  # Denver, CO (Civic Center)
DEFAULT_UNITS = "e"  # e = imperial/°F, m = metric/°C
DEFAULT_LANGUAGE = "en-US"
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36"
)
DEFAULT_FORECAST_DAYS = 7
APP_STATE_MARKER = 'window.__data=JSON.parse("'


class WeatherLoggerError(Exception):
    """Custom error for logging workflow failures."""


@dataclass
class Config:
    location_id: str
    units: str
    language: str
    db_path: str
    timeout: int
    user_agent: str
    forecast_days: int


def parse_args(argv: Optional[Iterable[str]] = None) -> Config:
    parser = argparse.ArgumentParser(
        description="Persist weather.com hourly forecasts and current observations."
    )
    parser.add_argument(
        "--location-id",
        default=os.environ.get("TWC_LOCATION_ID", DEFAULT_LOCATION_ID),
        help="Weather Channel location identifier (default: Denver USCO0105:1:US).",
    )
    parser.add_argument(
        "--units",
        choices=("e", "m"),
        default=os.environ.get("TWC_UNITS", DEFAULT_UNITS),
        help="Unit system (e=imperial/°F, m=metric/°C).",
    )
    parser.add_argument(
        "--language",
        default=os.environ.get("TWC_LANGUAGE", DEFAULT_LANGUAGE),
        help="Language for localized content (default: en-US).",
    )
    parser.add_argument(
        "--db-path",
        default=os.environ.get("WEATHER_DB_PATH", "weather_data.sqlite"),
        help="SQLite database file path.",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=int(os.environ.get("TWC_TIMEOUT", 15)),
        help="HTTP timeout seconds.",
    )
    parser.add_argument(
        "--user-agent",
        default=os.environ.get("TWC_USER_AGENT", DEFAULT_USER_AGENT),
        help="User-Agent header for the HTTP request.",
    )
    parser.add_argument(
        "--forecast-days",
        type=int,
        default=int(os.environ.get("TWC_FORECAST_DAYS", DEFAULT_FORECAST_DAYS)),
        help="Number of days of forecast data to store (0 to disable daily logging).",
    )

    args = parser.parse_args(argv)
    if not args.location_id:
        parser.error("A Weather Channel location id is required (--location-id or env TWC_LOCATION_ID).")
    if args.forecast_days < 0:
        parser.error("--forecast-days must be zero or a positive integer.")
    if args.forecast_days > 15:
        parser.error("--forecast-days cannot exceed 15 due to API limits.")

    return Config(
        location_id=args.location_id,
        units=args.units,
        language=args.language,
        db_path=args.db_path,
        timeout=args.timeout,
        user_agent=args.user_agent,
        forecast_days=args.forecast_days,
    )


def _fetch_weather_page(config: Config, path: str) -> str:
    url = f"https://weather.com/{path}/l/{config.location_id}"
    params = {"unit": config.units}
    headers = {
        "User-Agent": config.user_agent,
        "Accept-Language": config.language,
    }
    response = requests.get(url, params=params, headers=headers, timeout=config.timeout)
    response.raise_for_status()
    return response.text


def fetch_hourly_page(config: Config) -> str:
    return _fetch_weather_page(config, "weather/hourbyhour")


def fetch_daily_page(config: Config) -> str:
    return _fetch_weather_page(config, "weather/tenday")


def extract_app_state(html: str) -> Dict[str, Any]:
    start = html.find(APP_STATE_MARKER)
    if start == -1:
        raise WeatherLoggerError("Unable to locate application state block in page.")
    start += len(APP_STATE_MARKER)
    end = html.find('");', start)
    if end == -1:
        raise WeatherLoggerError("Unable to locate end of application state block.")

    raw = html[start:end]
    try:
        decoded = raw.encode("utf-8").decode("unicode_escape")
        return json.loads(decoded)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise WeatherLoggerError("Failed to decode application state JSON.") from exc


def _extract_data_block(app_state: Dict[str, Any], block: str) -> Dict[str, Any]:
    bucket = app_state.get("dal", {}).get(block)
    if not bucket:
        raise WeatherLoggerError(f"Missing '{block}' data block.")

    for entry in bucket.values():
        if entry.get("loaded") and entry.get("data"):
            data = entry["data"]
            if isinstance(data, dict) and "data" in data and len(data) == 1 and isinstance(data["data"], dict):
                return data["data"]
            return data
    raise WeatherLoggerError(f"No loaded entries found in '{block}'.")


def extract_location(app_state: Dict[str, Any]) -> Tuple[str, Dict[str, Any]]:
    data = _extract_data_block(app_state, "getSunV3LocationPointUrlConfig")
    location = data.get("location")
    if not isinstance(location, dict):
        raise WeatherLoggerError("Location metadata missing from application state.")
    latitude = location.get("latitude")
    longitude = location.get("longitude")
    if latitude is None or longitude is None:
        raise WeatherLoggerError("Incomplete location coordinates in metadata.")
    geocode = f"{float(latitude):.4f},{float(longitude):.4f}"
    return geocode, location


def extract_hourly_forecast(app_state: Dict[str, Any]) -> Dict[str, Any]:
    return _extract_data_block(app_state, "getSunV3HourlyForecastWithHeadersUrlConfig")


def extract_daily_forecast(app_state: Dict[str, Any]) -> Dict[str, Any]:
    return _extract_data_block(app_state, "getSunV3DailyForecastWithHeadersUrlConfig")


def extract_current_observation(app_state: Dict[str, Any]) -> Dict[str, Any]:
    return _extract_data_block(app_state, "getSunV3CurrentObservationsUrlConfig")


def select_forecast_hour(forecast: Dict[str, Any]) -> Dict[str, Any]:
    temps: List[Any] = forecast.get("temperature") or []
    valid_utc: List[Any] = forecast.get("validTimeUtc") or []
    if not temps or not valid_utc:
        raise WeatherLoggerError("Forecast payload missing temperature or validTimeUtc fields.")

    if len(valid_utc) < len(temps):
        raise WeatherLoggerError("Forecast payload has mismatched validTimeUtc length.")

    indices = [idx for idx, value in enumerate(valid_utc) if isinstance(value, (int, float))]
    if not indices:
        raise WeatherLoggerError("Forecast payload has no numeric validTimeUtc entries.")

    now_epoch = int(datetime.now(timezone.utc).timestamp())
    best_idx = min(indices, key=lambda idx: abs(valid_utc[idx] - now_epoch))

    return _extract_indexed_snapshot(forecast, best_idx)


def _extract_indexed_snapshot(payload: Dict[str, Any], index: int) -> Dict[str, Any]:
    snapshot: Dict[str, Any] = {}
    for key, value in payload.items():
        if isinstance(value, list):
            if index < len(value):
                snapshot[key] = value[index]
        else:
            snapshot[key] = value
    return snapshot


def select_daily_forecasts(forecast: Dict[str, Any], days: int) -> List[Dict[str, Any]]:
    if days <= 0:
        return []

    valid_utc: List[Any] = forecast.get("validTimeUtc") or []
    numeric_indices = [idx for idx, value in enumerate(valid_utc) if isinstance(value, (int, float))]
    if not numeric_indices:
        raise WeatherLoggerError("Daily forecast payload has no numeric validTimeUtc entries.")

    numeric_indices.sort(key=lambda idx: valid_utc[idx])
    selected_indices = numeric_indices[:days]
    if not selected_indices:
        raise WeatherLoggerError("Daily forecast payload shorter than requested days.")

    return [_extract_indexed_snapshot(forecast, idx) for idx in selected_indices]


def format_temperature(value: Any) -> str:
    if value is None:
        return "NA"
    try:
        return f"{float(value):.1f}"
    except (TypeError, ValueError):
        return str(value)


def format_daily_summary(day: Dict[str, Any]) -> str:
    day_of_week = day.get("dayOfWeek")
    date_local = day.get("validTimeLocal")
    date_part = None
    if isinstance(date_local, str):
        date_part = date_local.split("T", 1)[0]

    if day_of_week and date_part:
        label = f"{day_of_week} {date_part}"
    else:
        label = day_of_week or date_part or "Day"

    min_temp = day.get("temperatureMin")
    if min_temp is None:
        min_temp = day.get("calendarDayTemperatureMin")
    max_temp = day.get("temperatureMax")
    if max_temp is None:
        max_temp = day.get("calendarDayTemperatureMax")

    low = format_temperature(min_temp)
    high = format_temperature(max_temp)
    return f"{label}: low {low}° / high {high}°"


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS weather_hourly (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            retrieved_at_utc INTEGER NOT NULL,
            geocode TEXT NOT NULL,
            units TEXT NOT NULL,
            forecast_time_utc INTEGER,
            forecast_temperature REAL,
            forecast_payload TEXT,
            observation_time_utc INTEGER,
            observation_temperature REAL,
            observation_payload TEXT
        );

        CREATE TABLE IF NOT EXISTS weather_daily (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            retrieved_at_utc INTEGER NOT NULL,
            geocode TEXT NOT NULL,
            units TEXT NOT NULL,
            day_index INTEGER NOT NULL,
            valid_time_utc INTEGER,
            valid_time_local TEXT,
            temperature_min REAL,
            temperature_max REAL,
            payload TEXT NOT NULL
        );
        """
    )
    conn.commit()


def persist_entry(
    conn: sqlite3.Connection,
    geocode: str,
    units: str,
    forecast: Dict[str, Any],
    observation: Dict[str, Any],
) -> None:
    retrieved_at = int(datetime.now(timezone.utc).timestamp())
    forecast_time = forecast.get("validTimeUtc")
    observation_time = observation.get("validTimeUtc")

    conn.execute(
        """
        INSERT INTO weather_hourly (
            retrieved_at_utc,
            geocode,
            units,
            forecast_time_utc,
            forecast_temperature,
            forecast_payload,
            observation_time_utc,
            observation_temperature,
            observation_payload
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            retrieved_at,
            geocode,
            units,
            forecast_time if isinstance(forecast_time, (int, float)) else None,
            forecast.get("temperature"),
            json.dumps(forecast, separators=(",", ":")),
            observation_time if isinstance(observation_time, (int, float)) else None,
            observation.get("temperature"),
            json.dumps(observation, separators=(",", ":")),
        ),
    )
    conn.commit()


def persist_daily_entries(
    conn: sqlite3.Connection,
    geocode: str,
    units: str,
    snapshots: List[Dict[str, Any]],
) -> None:
    if not snapshots:
        return

    retrieved_at = int(datetime.now(timezone.utc).timestamp())
    rows = []
    for idx, day in enumerate(snapshots):
        valid_time = day.get("validTimeUtc")
        valid_time_local = day.get("validTimeLocal")
        min_temp = day.get("temperatureMin")
        if min_temp is None:
            min_temp = day.get("calendarDayTemperatureMin")
        max_temp = day.get("temperatureMax")
        if max_temp is None:
            max_temp = day.get("calendarDayTemperatureMax")
        rows.append(
            (
                retrieved_at,
                geocode,
                units,
                idx,
                valid_time if isinstance(valid_time, (int, float)) else None,
                valid_time_local if isinstance(valid_time_local, str) else None,
                min_temp,
                max_temp,
                json.dumps(day, separators=(",", ":")),
            )
        )

    conn.executemany(
        """
        INSERT INTO weather_daily (
            retrieved_at_utc,
            geocode,
            units,
            day_index,
            valid_time_utc,
            valid_time_local,
            temperature_min,
            temperature_max,
            payload
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        rows,
    )
    conn.commit()


def main(argv: Optional[Iterable[str]] = None) -> int:
    config = parse_args(argv)

    try:
        hourly_html = fetch_hourly_page(config)
        hourly_state = extract_app_state(hourly_html)
        geocode, location_meta = extract_location(hourly_state)
        forecast_raw = extract_hourly_forecast(hourly_state)
        observation_raw = extract_current_observation(hourly_state)
        forecast_snapshot = select_forecast_hour(forecast_raw)
    except requests.HTTPError as exc:
        raise WeatherLoggerError(f"HTTP error retrieving weather.com hourly page: {exc}") from exc
    except requests.RequestException as exc:
        raise WeatherLoggerError(f"Network error retrieving weather.com hourly page: {exc}") from exc
    except Exception as exc:  # noqa: BLE001 - surface parsing issues
        raise WeatherLoggerError(str(exc)) from exc

    daily_snapshots: List[Dict[str, Any]] = []
    if config.forecast_days:
        try:
            daily_html = fetch_daily_page(config)
            daily_state = extract_app_state(daily_html)
            daily_forecast_raw = extract_daily_forecast(daily_state)
            daily_snapshots = select_daily_forecasts(daily_forecast_raw, config.forecast_days)
        except requests.HTTPError as exc:
            raise WeatherLoggerError(f"HTTP error retrieving weather.com ten-day page: {exc}") from exc
        except requests.RequestException as exc:
            raise WeatherLoggerError(f"Network error retrieving weather.com ten-day page: {exc}") from exc
        except WeatherLoggerError:
            raise
        except Exception as exc:  # noqa: BLE001 - surface parsing issues
            raise WeatherLoggerError(f"Failed to parse daily forecast: {exc}") from exc

    conn = sqlite3.connect(config.db_path)
    try:
        ensure_schema(conn)
        persist_entry(
            conn,
            geocode=geocode,
            units=config.units,
            forecast=forecast_snapshot,
            observation=observation_raw,
        )
        if daily_snapshots:
            persist_daily_entries(
                conn,
                geocode=geocode,
                units=config.units,
                snapshots=daily_snapshots,
            )
    finally:
        conn.close()

    location_name = (
        location_meta.get("displayContext")
        or location_meta.get("displayName")
        or location_meta.get("city")
        or config.location_id
    )
    forecast_time = forecast_snapshot.get("validTimeLocal") or forecast_snapshot.get("validTimeUtc")
    observation_time = observation_raw.get("validTimeLocal") or observation_raw.get("validTimeUtc")
    print(
        "Stored forecast %s° and actual %s° (units=%s) for %s "
        "forecast_time=%s observation_time=%s"
        % (
            format_temperature(forecast_snapshot.get("temperature")),
            format_temperature(observation_raw.get("temperature")),
            config.units,
            location_name,
            forecast_time,
            observation_time,
        )
    )
    if daily_snapshots:
        daily_summary = "; ".join(format_daily_summary(day) for day in daily_snapshots)
        print(f"Stored {len(daily_snapshots)} daily forecasts for {location_name}: {daily_summary}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except WeatherLoggerError as err:
        print(f"error: {err}", file=sys.stderr)
        raise SystemExit(1)
