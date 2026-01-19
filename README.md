# WeatherGrab

Hourly scraper that pairs The Weather Channel hourly forecast data with the
current observed temperature for Denver, Colorado. Each run writes a new entry
to a local SQLite database so you can evaluate forecast accuracy over time or
build ML training sets from prediction-only features. Review weather.com’s
Terms of Service before automating requests.

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

To discover another location id, open your target city on weather.com and copy
the identifier from the page URL (the segment after `/l/`). The script scrapes
the public HTML and extracts the embedded JSON state; no paid API key is
required.

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

Adjust the path to your project, virtual environment activation, and logging
preferences as needed. Consider randomizing the schedule slightly to avoid
predictable scraping intervals.
