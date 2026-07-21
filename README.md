# WeatherGrab

Python data-ingestion tool that pairs The Weather Channel's hourly forecast with the current observed temperature and stores each snapshot in SQLite. The accumulated records support forecast-error analysis and time-series model experiments.

## Project Status

This is a historical project. Weather.com replaced the embedded application-state format used by the parser, so live collection currently requires a parser update. The repository remains available as an example of the original ingestion and SQLite persistence workflow, but it is not one of my actively maintained portfolio projects.

## Data Workflow

- Retrieves hourly forecasts and current observations from embedded page data.
- Selects the forecast horizon closest to the configured target.
- Stores retrieval timestamps, forecast timestamps, temperatures, and raw snapshots.
- Supports configurable locations, units, language, user agent, and database path.
- Uses SQLite for append-only local collection and later analysis.

## Prerequisites

- Python 3.10+
- `requests` Python package (`pip install -r requirements.txt`)

## Usage

```
python weather_logger.py \
  --location-id USCO0105:1:US \
  --units e \
  --db-path weather_data.sqlite
```

Configuration can also be supplied via environment variables:

- `TWC_LOCATION_ID` (defaults to `USCO0105:1:US`, Denver Civic Center)
- `TWC_UNITS` (`e` for Fahrenheit, `m` for Celsius)
- `TWC_LANGUAGE`
- `WEATHER_DB_PATH`
- `TWC_USER_AGENT`

To discover another location ID, open the target city on weather.com and copy the identifier from the page URL segment after `/l/`. The script extracts the embedded JSON state; no paid API key is required.

Each execution appends a record to the `weather_hourly` table containing:

- Retrieval timestamp
- Forecast hour timestamp and temperature
- Observed timestamp and temperature
- Raw JSON snapshots of the selected forecast hour and observation

## Scheduling

Run hourly with cron:

```
0 * * * * /usr/bin/env bash -lc 'cd /path/to/WeatherGrab && source venv/bin/activate && python weather_logger.py >> logs/weather.log 2>&1'
```

Adjust the project path, virtual-environment activation, and logging destination as needed. Review the data provider's terms before scheduling automated collection.
