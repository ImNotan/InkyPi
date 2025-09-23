from plugins.base_plugin.base_plugin import BasePlugin
from PIL import Image
import os
import requests
import logging
from datetime import datetime, timezone
import pytz
from io import BytesIO
import math
from config import Config
import fcntl
import json

logger = logging.getLogger(__name__)

UNITS = {
    "standard": {
        "temperature": "K",
        "speed": "m/s"
    },
    "metric": {
        "temperature": "°C",
        "speed": "m/s"

    },
    "imperial": {
        "temperature": "°F",
        "speed": "mph"
    }
}

OPEN_METEO_FORECAST_URL = "https://api.open-meteo.com/v1/forecast?latitude={lat}&longitude={long}&hourly=temperature_2m,precipitation,precipitation_probability,relative_humidity_2m,surface_pressure,visibility&daily=weathercode,temperature_2m_max,temperature_2m_min,sunrise,sunset&current_weather=true&timezone=auto&models=best_match&forecast_days={forecast_days}"
OPEN_METEO_AIR_QUALITY_URL = "https://air-quality-api.open-meteo.com/v1/air-quality?latitude={lat}&longitude={long}&hourly=european_aqi,uv_index,uv_index_clear_sky&timezone=auto"
OPEN_METEO_UNIT_PARAMS = {
    "standard": "temperature_unit=kelvin&wind_speed_unit=ms&precipitation_unit=mm",
    "metric":   "temperature_unit=celsius&wind_speed_unit=ms&precipitation_unit=mm",
    "imperial": "temperature_unit=fahrenheit&wind_speed_unit=mph&precipitation_unit=inch"
}

SENSOR_FILE = "/home/anton/tempRecorder/tempRecording.json"

class Weather_local(BasePlugin):
    def generate_settings_template(self):
        template_params = super().generate_settings_template()
        template_params['api_key'] = {
            "required": True,
            "service": "OpenWeatherMap",
            "expected_key": "OPEN_WEATHER_MAP_SECRET"
        }
        template_params['style_settings'] = True
        return template_params

    def generate_image(self, settings, device_config):
        lat = settings.get('latitude')
        long = settings.get('longitude')
        if not lat or not long:
            raise RuntimeError("Latitude and Longitude are required.")

        units = settings.get('units')
        if not units or units not in ['metric', 'imperial', 'standard']:
            raise RuntimeError("Units are required.")

        weather_provider = settings.get('weatherProvider', 'OpenMeteo')
        title = settings.get('customTitle', '')

        timezone = device_config.get_config("timezone", default="America/New_York")
        time_format = device_config.get_config("time_format", default="12h")
        tz = pytz.timezone(timezone)

        try:  
            if weather_provider == "OpenMeteo":
                forecast_days = 7
                weather_data = self.get_open_meteo_data(lat, long, units, forecast_days + 1)
                aqi_data = self.get_open_meteo_air_quality(lat, long)
                sensor_data = self.get_sensor_data()
                template_params = self.parse_open_meteo_data(weather_data, aqi_data, sensor_data, tz, units, time_format)
            else:
                raise RuntimeError(f"Unknown weather provider: {weather_provider}")

            template_params['title'] = title
        except Exception as e:
            logger.error(f"{weather_provider} request failed: {str(e)}")
            raise RuntimeError(f"{weather_provider} request failure, please check logs.")
       
        dimensions = device_config.get_resolution()
        if device_config.get_config("orientation") == "vertical":
            dimensions = dimensions[::-1]

        template_params["plugin_settings"] = settings

        # Add last refresh time
        now = datetime.now(tz)
        if time_format == "24h":
            last_refresh_time = now.strftime("%Y-%m-%d %H:%M")
        else:
            last_refresh_time = now.strftime("%Y-%m-%d %I:%M %p")
        template_params["last_refresh_time"] = last_refresh_time

        image = self.render_image(dimensions, "weather_local.html", "weather_local.css", template_params)

        if not image:
            raise RuntimeError("Failed to take screenshot, please check logs.")
        return image

    def parse_open_meteo_data(self, weather_data, aqi_data, sensor_data, tz, units, time_format):
        current = weather_data.get("current_weather", {})
        dt = datetime.fromisoformat(current.get('time')).astimezone(tz) if current.get('time') else datetime.now(tz)
        weather_code = current.get("weathercode", 0)
        current_icon = self.map_weather_code_to_icon(weather_code, dt.hour)

        data = {
            "current_date": dt.strftime("%A, %B %d"),
            "current_day_icon": self.get_plugin_dir(f'icons/{current_icon}.png'),
            "current_temperature": str(round(current.get("temperature", 1))),
            "feels_like": str(round(current.get("apparent_temperature", current.get("temperature", 0)))),
            "indoor_temperature": str(round(sensor_data['temp_sensor'][-1], 1)),
            "temperature_unit": UNITS[units]["temperature"],
            "units": units,
            "time_format": time_format
        }

        data['forecast'] = self.parse_open_meteo_forecast(weather_data.get('daily', {}), tz)
        data['data_points'] = self.parse_open_meteo_data_points(weather_data, aqi_data, sensor_data, tz, units, time_format)
        
        data['hourly_forecast'] = self.parse_open_meteo_hourly(weather_data.get('hourly', {}), sensor_data, tz, time_format)
        return data

    def map_weather_code_to_icon(self, weather_code, hour):

        icon = "01d" # Default to clear day icon
        
        if weather_code in [0]: # Clear sky
            icon = "01d"
        elif weather_code in [1]: # Mainly clear
            icon = "02d"
        elif weather_code in [2]: # Partly cloudy
            icon = "03d"
        elif weather_code in [3]: # Overcast
            icon = "04d"
        elif weather_code in [45, 48]: # Fog and depositing rime fog
            icon = "50d"
        elif weather_code in [51, 53, 55]: # Drizzle
            icon = "09d"
        elif weather_code in [56, 57]: # Freezing Drizzle
            icon = "09d"
        elif weather_code in [61, 63, 65]: # Rain: Slight, moderate, heavy
            icon = "10d"
        elif weather_code in [66, 67]: # Freezing Rain
            icon = "10d"
        elif weather_code in [71, 73, 75]: # Snow fall: Slight, moderate, heavy
            icon = "13d"
        elif weather_code in [77]: # Snow grains
            icon = "13d"
        elif weather_code in [80, 81, 82]: # Rain showers: Slight, moderate, violent
            icon = "09d"
        elif weather_code in [85, 86]: # Snow showers slight and heavy
            icon = "13d"
        elif weather_code in [95]: # Thunderstorm
            icon = "11d"
        elif weather_code in [96, 99]: # Thunderstorm with slight and heavy hail
            icon = "11d"
            
        return icon

    def parse_open_meteo_forecast(self, daily_data, tz):
        """
        Parse the daily forecast from Open-Meteo API and inject moon phase from Farmsense API.
        """
        times = daily_data.get('time', [])
        weather_codes = daily_data.get('weathercode', [])
        temp_max = daily_data.get('temperature_2m_max', [])
        temp_min = daily_data.get('temperature_2m_min', [])

        forecast = []

        for i in range(0, len(times)): 
            dt = datetime.fromisoformat(times[i]).replace(tzinfo=timezone.utc).astimezone(tz)
            day_label = dt.strftime("%a")

            code = weather_codes[i] if i < len(weather_codes) else 0
            weather_icon = self.map_weather_code_to_icon(code, 12)
            weather_icon_path = self.get_plugin_dir(f"icons/{weather_icon}.png")

            timestamp = int(dt.replace(hour=12, minute=0, second=0).timestamp())
            api_url = f"https://api.farmsense.net/v1/moonphases/?d={timestamp}"
           
            try:
                resp = requests.get(api_url, verify=False)
                moon = resp.json()[0]
                phase_raw = moon.get("Phase", "New Moon")
                illum_pct = float(moon.get("Illumination", 0)) * 100
                phase_name = phase_raw.lower().replace(" ", "")
                if phase_name == "darkmoon":
                    phase_name = "newmoon"
                elif phase_name in ("3rdquarter", "thirdquarter"):
                    phase_name = "lastquarter"
                elif phase_name in ("1stquarter", "firstquarter"):
                    phase_name = "firstquarter"
            except Exception:
                illum_pct = 0
                phase_name = "newmoon"

            moon_icon_path = self.get_plugin_dir(f"icons/{phase_name}.png")

            forecast.append({
                "day": day_label,
                "high": int(temp_max[i]) if i < len(temp_max) else 0,
                "low": int(temp_min[i]) if i < len(temp_min) else 0,
                "icon": weather_icon_path,
                "moon_phase_pct": f"{illum_pct:.0f}",
                "moon_phase_icon": moon_icon_path
            })

        return forecast

    def parse_open_meteo_hourly(self, hourly_data, sensor_data, tz, time_format):
        hourly = []
        times = hourly_data.get('time', [])
        temperatures = hourly_data.get('temperature_2m', [])
        indoor_temperatures = sensor_data.get('temp_sensor', [])
        indoor_times = sensor_data.get('time', [])
        precipitation_probabilities = hourly_data.get('precipitation_probability', [])
        rain = hourly_data.get('precipitation', [])
        current_time_in_tz = datetime.now(tz)
        start_index = 0
        indoor_start_index = 0
        # for i, time_str in enumerate(times):
        #     try:
        #         dt_hourly = datetime.fromisoformat(time_str).astimezone(tz)
        #         if dt_hourly.date() == current_time_in_tz.date() and dt_hourly.hour >= current_time_in_tz.hour:
        #             start_index = i
        #             break
        #         if dt_hourly.date() > current_time_in_tz.date():
        #             break
        #     except ValueError:
        #         logger.warning(f"Could not parse time string {time_str} in hourly data.")
        #         continue
        for i, time_str in enumerate(times):
            try:
                dt_hourly = datetime.fromisoformat(time_str).astimezone(tz)
                if dt_hourly.date() == current_time_in_tz.date():
                    start_index = i
                    break
                if dt_hourly.date() > current_time_in_tz.date():
                    break
            except ValueError:
                logger.warning(f"Could not parse time string {time_str} in hourly data.")
                continue

        for i, time_str in enumerate(indoor_times):
            try:
                dt_hourly = datetime.fromisoformat(time_str)
                if dt_hourly.date() == current_time_in_tz.date():
                    indoor_start_index = i
                    break
                if dt_hourly.date() > current_time_in_tz.date():
                    break
            except ValueError:
                logger.warning(f"Could not parse time string {time_str} in indoor sensor data.")
                continue
        
        average_index = 0
        sliced_indoor_temperatures = []
        j = 0
        for i, time_str in enumerate(indoor_times):
            try:
                dt_hourly = datetime.fromisoformat(time_str)
                if dt_hourly.date() == current_time_in_tz.date() and i > j:
                    bottom_index = 0
                    top_index = 0
                    j = i - 5
                    #print(j)
                    bottom_hour = 0
                    for j_time_str in enumerate(indoor_times[(i-5):]):
                        j_dt_hourly = datetime.fromisoformat(j_time_str[1])
                        #print(j_dt_hourly)
                        if j_dt_hourly.minute >= 35 and bottom_index == 0:
                            bottom_index = j
                            bottom_hour = j_dt_hourly.hour
                        elif j_dt_hourly.minute >= 30 and bottom_index != 0 and j_dt_hourly.hour != bottom_hour:
                            top_index = j
                            break
                        j += 1
                    if top_index == 0:
                        top_index = j - 1
                    print(bottom_index)
                    print(indoor_times[bottom_index])
                    print(top_index)
                    print(indoor_times[top_index])
                    sliced_indoor_temperatures.append(round(sum(indoor_temperatures[bottom_index:top_index]) / (top_index - bottom_index), 1))
                    print(sliced_indoor_temperatures[average_index])
                    average_index += 1

            except ValueError:
                logger.warning(f"Could not parse time string {time_str} in indoor sensor data.")
                continue

        sliced_times = times[start_index:]
        sliced_temperatures = temperatures[start_index:]
        sliced_precipitation_probabilities = precipitation_probabilities[start_index:]
        sliced_rain = rain[start_index:]

        #sliced_indoor_temperatures = indoor_temperatures[indoor_start_index:]

        for i in range(min(24, len(sliced_times))):
            dt = datetime.fromisoformat(sliced_times[i]).astimezone(tz)
            hour_forecast = {
                "time": self.format_time(dt, time_format, True),
                "temperature": sliced_temperatures[i] if i < len(sliced_temperatures) else 0,
                "precipitation": (sliced_precipitation_probabilities[i] / 100) if i < len(sliced_precipitation_probabilities) else 0,
                "rain": (sliced_rain[i]) if i < len(sliced_rain) else 0,
                "indoor_temperature": sliced_indoor_temperatures[i] if i < len(sliced_indoor_temperatures) else None
            }
            hourly.append(hour_forecast)
        return hourly

    def parse_open_meteo_data_points(self, weather_data, aqi_data, sensor_data, tz, units, time_format):
        """Parses current data points from Open-Meteo API response."""
        data_points = []
        daily_data = weather_data.get('daily', {})
        current_data = weather_data.get('current_weather', {})
        hourly_data = weather_data.get('hourly', {})

        current_time = datetime.now(tz)

        # Sunrise
        sunrise_times = daily_data.get('sunrise', [])
        if sunrise_times:
            sunrise_dt = datetime.fromisoformat(sunrise_times[0]).astimezone(tz)
            data_points.append({
                "label": "Sunrise",
                "measurement": self.format_time(sunrise_dt, time_format, include_am_pm=False),
                "unit": "" if time_format == "24h" else sunrise_dt.strftime('%p'),
                "icon": self.get_plugin_dir('icons/sunrise.png')
            })
        else:
            logging.error(f"Sunrise not found in Open-Meteo response, this is expected for polar areas in midnight sun and polar night periods.")

        # Sunset
        sunset_times = daily_data.get('sunset', [])
        if sunset_times:
            sunset_dt = datetime.fromisoformat(sunset_times[0]).astimezone(tz)
            data_points.append({
                "label": "Sunset",
                "measurement": self.format_time(sunset_dt, time_format, include_am_pm=False),
                "unit": "" if time_format == "24h" else sunset_dt.strftime('%p'),
                "icon": self.get_plugin_dir('icons/sunset.png')
            })
        else:
            logging.error(f"Sunset not found in Open-Meteo response, this is expected for polar areas in midnight sun and polar night periods.")

        # Wind
        wind_speed = current_data.get("windspeed", 0)
        wind_unit = UNITS[units]["speed"]
        data_points.append({
            "label": "Wind", "measurement": wind_speed, "unit": wind_unit,
            "icon": self.get_plugin_dir('icons/wind.png')
        })

        # UV Index
        uv_index_hourly_times = aqi_data.get('hourly', {}).get('time', [])
        uv_index_values = aqi_data.get('hourly', {}).get('uv_index', [])
        current_uv_index = "N/A"
        for i, time_str in enumerate(uv_index_hourly_times):
            try:
                if datetime.fromisoformat(time_str).astimezone(tz).hour == current_time.hour:
                    current_uv_index = uv_index_values[i]
                    break
            except ValueError:
                logger.warning(f"Could not parse time string {time_str} for UV Index.")
                continue
        data_points.append({
            "label": "UV Index", "measurement": current_uv_index, "unit": '',
            "icon": self.get_plugin_dir('icons/uvi.png')
        })

        # Humidity
        current_humidity = "N/A"
        humidity_hourly_times = hourly_data.get('time', [])
        humidity_values = hourly_data.get('relative_humidity_2m', [])
        for i, time_str in enumerate(humidity_hourly_times):
            try:
                if datetime.fromisoformat(time_str).astimezone(tz).hour == current_time.hour:
                    current_humidity = int(humidity_values[i])
                    break
            except ValueError:
                logger.warning(f"Could not parse time string {time_str} for humidity.")
                continue
        data_points.append({
            "label": "Humidity", "measurement": current_humidity, "unit": '%',
            "icon": self.get_plugin_dir('icons/humidity.png')
        })

        # Indoor Humidity
        current_indoor_humidity = int(round(sensor_data['humidity'], 0))
        data_points.append({
            "label": "Indoor Humidity", "measurement": current_indoor_humidity, "unit": '%',
            "icon": self.get_plugin_dir('icons/humidity.png')
        })

        # Pressure
        # current_pressure = "N/A"
        # pressure_hourly_times = hourly_data.get('time', [])
        # pressure_values = hourly_data.get('surface_pressure', [])
        # for i, time_str in enumerate(pressure_hourly_times):
        #     try:
        #         if datetime.fromisoformat(time_str).astimezone(tz).hour == current_time.hour:
        #             current_pressure = int(pressure_values[i])
        #             break
        #     except ValueError:
        #         logger.warning(f"Could not parse time string {time_str} for pressure.")
        #         continue
        # data_points.append({
        #     "label": "Pressure", "measurement": current_pressure, "unit": 'hPa',
        #     "icon": self.get_plugin_dir('icons/pressure.png')
        # })

        # Visibility
        current_visibility = "N/A"
        visibility_hourly_times = hourly_data.get('time', [])
        visibility_values = hourly_data.get('visibility', [])
        for i, time_str in enumerate(visibility_hourly_times):
            try:
                if datetime.fromisoformat(time_str).astimezone(tz).hour == current_time.hour:
                    visibility = visibility_values[i]
                    if units == "imperial":
                        current_visibility = int(round(visibility, 0))
                        unit_label = "ft"
                    else:
                        current_visibility = round(visibility / 1000, 1)
                        unit_label = "km"
                    break
            except ValueError:
                logger.warning(f"Could not parse time string {time_str} for visibility.")
                continue

        visibility_str = f">{current_visibility}" if isinstance(current_visibility, (int, float)) and (
            (units == "imperial" and current_visibility >= 32808) or 
            (units != "imperial" and current_visibility >= 10)
        ) else current_visibility

        data_points.append({
            "label": "Visibility", "measurement": visibility_str, "unit": unit_label,
            "icon": self.get_plugin_dir('icons/visibility.png')
        })

        # Air Quality
        aqi_hourly_times = aqi_data.get('hourly', {}).get('time', [])
        aqi_values = aqi_data.get('hourly', {}).get('european_aqi', [])
        current_aqi = "N/A"
        for i, time_str in enumerate(aqi_hourly_times):
            try:
                if datetime.fromisoformat(time_str).astimezone(tz).hour == current_time.hour:
                    current_aqi = round(aqi_values[i], 1)
                    break
            except ValueError:
                logger.warning(f"Could not parse time string {time_str} for AQI.")
                continue
        scale = ""
        if current_aqi:
            scale = ["Good","Fair","Moderate","Poor","Very Poor","Ext Poor"][min(current_aqi//20,5)]
        data_points.append({
            "label": "Air Quality", "measurement": current_aqi,
            "unit": scale, "icon": self.get_plugin_dir('icons/aqi.png')
        })

        # Indoor Pressure
        # current_indoor_pressure = int(round(sensor_data['pressure'], 0))
        # data_points.append({
        #     "label": "Indoor Pressure", "measurement": current_indoor_pressure, "unit": 'hPa',
        #     "icon": self.get_plugin_dir('icons/pressure.png')
        # })

        return data_points

    def get_open_meteo_data(self, lat, long, units, forecast_days):
        unit_params = OPEN_METEO_UNIT_PARAMS[units]
        url = OPEN_METEO_FORECAST_URL.format(lat=lat, long=long, forecast_days=forecast_days) + f"&{unit_params}"
        response = requests.get(url)
        
        if not 200 <= response.status_code < 300:
            logging.error(f"Failed to retrieve Open-Meteo weather data: {response.content}")
            raise RuntimeError("Failed to retrieve Open-Meteo weather data.")
        
        return response.json()

    def get_sensor_data(self):
        sensor_data = {}
        if Config.DEV_MODE:
            # sensor_data["time"] = [datetime.now().strftime('%Y-%m-%dT%H:%M')]
            # sensor_data['temp_sensor'] = [20] 
            # sensor_data['pressure'] = 1000
            # sensor_data['humidity'] = 50
            sensor_data = {
    "time": [
        "2025-09-22T08:50",
        "2025-09-22T08:55",
        "2025-09-22T09:00",
        "2025-09-22T09:05",
        "2025-09-22T09:10",
        "2025-09-22T09:15",
        "2025-09-22T09:20",
        "2025-09-22T09:25",
        "2025-09-22T09:30",
        "2025-09-22T09:35",
        "2025-09-22T09:40",
        "2025-09-22T09:45",
        "2025-09-22T09:50",
        "2025-09-22T09:55",
        "2025-09-22T10:00",
        "2025-09-22T10:05",
        "2025-09-22T10:10",
        "2025-09-22T10:15",
        "2025-09-22T10:20",
        "2025-09-22T10:25",
        "2025-09-22T10:30",
        "2025-09-22T10:35",
        "2025-09-22T10:40",
        "2025-09-22T10:45",
        "2025-09-22T10:50",
        "2025-09-22T10:55",
        "2025-09-22T11:00",
        "2025-09-22T11:05",
        "2025-09-22T11:10",
        "2025-09-22T11:15",
        "2025-09-22T11:20",
        "2025-09-22T11:25",
        "2025-09-22T11:30",
        "2025-09-22T11:35",
        "2025-09-22T11:40",
        "2025-09-22T11:45",
        "2025-09-22T11:50",
        "2025-09-22T11:55",
        "2025-09-22T12:00",
        "2025-09-22T12:05",
        "2025-09-22T12:10",
        "2025-09-22T12:15",
        "2025-09-22T12:20",
        "2025-09-22T12:25",
        "2025-09-22T12:30",
        "2025-09-22T12:35",
        "2025-09-22T12:40",
        "2025-09-22T12:45",
        "2025-09-22T12:50",
        "2025-09-22T12:55",
        "2025-09-22T13:00",
        "2025-09-22T13:05",
        "2025-09-22T13:10",
        "2025-09-22T13:15",
        "2025-09-22T13:20",
        "2025-09-22T13:25",
        "2025-09-22T13:30",
        "2025-09-22T13:35",
        "2025-09-22T13:40",
        "2025-09-22T13:45",
        "2025-09-22T13:50",
        "2025-09-22T13:55",
        "2025-09-22T14:00",
        "2025-09-22T14:05",
        "2025-09-22T14:10",
        "2025-09-22T14:15",
        "2025-09-22T14:20",
        "2025-09-22T14:25",
        "2025-09-22T14:30",
        "2025-09-22T14:35",
        "2025-09-22T14:40",
        "2025-09-22T14:45",
        "2025-09-22T14:50",
        "2025-09-22T14:55",
        "2025-09-22T15:00",
        "2025-09-22T15:05",
        "2025-09-22T15:10",
        "2025-09-22T15:15",
        "2025-09-22T15:20",
        "2025-09-22T15:25",
        "2025-09-22T15:30",
        "2025-09-22T15:35",
        "2025-09-22T15:40",
        "2025-09-22T15:45",
        "2025-09-22T15:50",
        "2025-09-22T15:55",
        "2025-09-22T16:00",
        "2025-09-22T16:05",
        "2025-09-22T16:10",
        "2025-09-22T16:15",
        "2025-09-22T16:20",
        "2025-09-22T16:25",
        "2025-09-22T16:30",
        "2025-09-22T16:35",
        "2025-09-22T16:40",
        "2025-09-22T16:45",
        "2025-09-22T16:50",
        "2025-09-22T16:55",
        "2025-09-22T17:00",
        "2025-09-22T17:05",
        "2025-09-22T17:10",
        "2025-09-22T17:15",
        "2025-09-22T17:20",
        "2025-09-22T17:25",
        "2025-09-22T17:30",
        "2025-09-22T17:35",
        "2025-09-22T17:40",
        "2025-09-22T17:45",
        "2025-09-22T17:50",
        "2025-09-22T17:55",
        "2025-09-22T18:00",
        "2025-09-22T18:05",
        "2025-09-22T18:10",
        "2025-09-22T18:15",
        "2025-09-22T18:20",
        "2025-09-22T18:25",
        "2025-09-22T18:30",
        "2025-09-22T18:35",
        "2025-09-22T18:40",
        "2025-09-22T18:45",
        "2025-09-22T18:50",
        "2025-09-22T18:55",
        "2025-09-22T19:00",
        "2025-09-22T19:05",
        "2025-09-22T19:10",
        "2025-09-22T19:15",
        "2025-09-22T19:20",
        "2025-09-22T19:25",
        "2025-09-22T19:30",
        "2025-09-22T19:35",
        "2025-09-22T19:40",
        "2025-09-22T19:45",
        "2025-09-22T19:50",
        "2025-09-22T19:55",
        "2025-09-22T20:00",
        "2025-09-22T20:05",
        "2025-09-22T20:10",
        "2025-09-22T20:15",
        "2025-09-22T20:20",
        "2025-09-22T20:25",
        "2025-09-22T20:30",
        "2025-09-22T20:35",
        "2025-09-22T20:40",
        "2025-09-22T20:45",
        "2025-09-22T20:50",
        "2025-09-22T20:55",
        "2025-09-22T21:00",
        "2025-09-22T21:05",
        "2025-09-22T21:10",
        "2025-09-22T21:15",
        "2025-09-22T21:20",
        "2025-09-22T21:25",
        "2025-09-22T21:30",
        "2025-09-22T21:35",
        "2025-09-22T21:40",
        "2025-09-22T21:45",
        "2025-09-22T21:50",
        "2025-09-22T21:55",
        "2025-09-22T22:00",
        "2025-09-22T22:05",
        "2025-09-22T22:10",
        "2025-09-22T22:15",
        "2025-09-22T22:20",
        "2025-09-22T22:25",
        "2025-09-22T22:30",
        "2025-09-22T22:35",
        "2025-09-22T22:40",
        "2025-09-22T22:45",
        "2025-09-22T22:50",
        "2025-09-22T22:55",
        "2025-09-22T23:00",
        "2025-09-22T23:05",
        "2025-09-22T23:10",
        "2025-09-22T23:15",
        "2025-09-22T23:20",
        "2025-09-22T23:25",
        "2025-09-22T23:30",
        "2025-09-22T23:35",
        "2025-09-22T23:40",
        "2025-09-22T23:45",
        "2025-09-22T23:50",
        "2025-09-22T23:55",
        "2025-09-23T00:00",
        "2025-09-23T00:05",
        "2025-09-23T00:10",
        "2025-09-23T00:15",
        "2025-09-23T00:20",
        "2025-09-23T00:25",
        "2025-09-23T00:30",
        "2025-09-23T00:35",
        "2025-09-23T00:40",
        "2025-09-23T00:45",
        "2025-09-23T00:50",
        "2025-09-23T00:55",
        "2025-09-23T01:00",
        "2025-09-23T01:05",
        "2025-09-23T01:10",
        "2025-09-23T01:15",
        "2025-09-23T01:20",
        "2025-09-23T01:25",
        "2025-09-23T01:30",
        "2025-09-23T01:35",
        "2025-09-23T01:40",
        "2025-09-23T01:45",
        "2025-09-23T01:50",
        "2025-09-23T01:55",
        "2025-09-23T02:00",
        "2025-09-23T02:05",
        "2025-09-23T02:10",
        "2025-09-23T02:15",
        "2025-09-23T02:20",
        "2025-09-23T02:25",
        "2025-09-23T02:30",
        "2025-09-23T02:35",
        "2025-09-23T02:40",
        "2025-09-23T02:45",
        "2025-09-23T02:50",
        "2025-09-23T02:55",
        "2025-09-23T03:00",
        "2025-09-23T03:05",
        "2025-09-23T03:10",
        "2025-09-23T03:15",
        "2025-09-23T03:20",
        "2025-09-23T03:25",
        "2025-09-23T03:30",
        "2025-09-23T03:35",
        "2025-09-23T03:40",
        "2025-09-23T03:45",
        "2025-09-23T03:50",
        "2025-09-23T03:55",
        "2025-09-23T04:00",
        "2025-09-23T04:05",
        "2025-09-23T04:10",
        "2025-09-23T04:15",
        "2025-09-23T04:20",
        "2025-09-23T04:25",
        "2025-09-23T04:30",
        "2025-09-23T04:35",
        "2025-09-23T04:40",
        "2025-09-23T04:45",
        "2025-09-23T04:50",
        "2025-09-23T04:55",
        "2025-09-23T05:00",
        "2025-09-23T05:05",
        "2025-09-23T05:10",
        "2025-09-23T05:15",
        "2025-09-23T05:20",
        "2025-09-23T05:25",
        "2025-09-23T05:30",
        "2025-09-23T05:35",
        "2025-09-23T05:40",
        "2025-09-23T05:45",
        "2025-09-23T05:50",
        "2025-09-23T05:55",
        "2025-09-23T06:00",
        "2025-09-23T06:05",
        "2025-09-23T06:10",
        "2025-09-23T06:15",
        "2025-09-23T06:20",
        "2025-09-23T06:25",
        "2025-09-23T06:30",
        "2025-09-23T06:35",
        "2025-09-23T06:40",
        "2025-09-23T06:45",
        "2025-09-23T06:50",
        "2025-09-23T06:55",
        "2025-09-23T07:00",
        "2025-09-23T07:05",
        "2025-09-23T07:10",
        "2025-09-23T07:15",
        "2025-09-23T07:20",
        "2025-09-23T07:25",
        "2025-09-23T07:30",
        "2025-09-23T07:35",
        "2025-09-23T07:40",
        "2025-09-23T07:45",
        "2025-09-23T07:50",
        "2025-09-23T07:55",
        "2025-09-23T08:00",
        "2025-09-23T08:05",
        "2025-09-23T08:10",
        "2025-09-23T08:15",
        "2025-09-23T08:20",
        "2025-09-23T08:25",
        "2025-09-23T08:30",
        "2025-09-23T08:35",
        "2025-09-23T08:40",
        "2025-09-23T08:45"
    ],
    "temp_sensor": [
        21.1,
        20.9,
        21.0,
        21.4,
        21.6,
        21.7,
        21.8,
        21.9,
        22.0,
        22.1,
        22.2,
        22.2,
        22.3,
        22.4,
        22.5,
        22.5,
        22.6,
        22.7,
        22.7,
        22.8,
        22.8,
        22.9,
        22.9,
        22.9,
        22.9,
        23.0,
        23.0,
        23.1,
        23.1,
        23.1,
        23.1,
        23.1,
        23.1,
        23.1,
        23.0,
        23.0,
        23.0,
        23.0,
        23.0,
        23.0,
        23.0,
        23.0,
        22.9,
        23.0,
        23.0,
        23.0,
        23.0,
        23.0,
        23.0,
        23.0,
        23.0,
        22.9,
        22.9,
        22.9,
        22.9,
        22.9,
        22.9,
        22.9,
        22.9,
        22.8,
        22.8,
        22.8,
        22.8,
        22.8,
        22.8,
        22.8,
        22.8,
        22.8,
        22.8,
        22.8,
        22.8,
        22.8,
        22.8,
        22.8,
        22.8,
        22.8,
        22.8,
        22.8,
        22.7,
        22.7,
        22.7,
        22.7,
        22.7,
        22.7,
        22.7,
        22.7,
        22.7,
        22.7,
        22.7,
        22.7,
        22.7,
        22.7,
        22.6,
        22.6,
        22.6,
        22.6,
        22.6,
        22.6,
        22.6,
        22.6,
        22.6,
        22.6,
        22.6,
        22.6,
        22.6,
        22.6,
        22.5,
        22.6,
        22.5,
        22.5,
        22.5,
        22.5,
        22.5,
        22.5,
        22.5,
        22.5,
        22.5,
        22.5,
        22.5,
        22.4,
        22.5,
        22.5,
        22.5,
        22.5,
        22.5,
        22.6,
        22.6,
        22.6,
        22.6,
        22.7,
        22.7,
        22.7,
        22.7,
        22.8,
        22.8,
        22.8,
        22.8,
        22.8,
        22.8,
        22.8,
        22.8,
        22.8,
        22.8,
        22.8,
        22.8,
        22.8,
        22.8,
        22.8,
        22.8,
        22.8,
        22.8,
        22.7,
        22.7,
        22.7,
        22.7,
        22.8,
        22.8,
        22.8,
        22.8,
        22.8,
        22.7,
        22.7,
        22.7,
        22.7,
        22.7,
        22.7,
        22.7,
        22.7,
        22.7,
        22.7,
        22.7,
        22.7,
        22.7,
        22.7,
        22.7,
        22.7,
        22.7,
        22.7,
        22.7,
        22.7,
        22.7,
        22.7,
        22.7,
        22.7,
        22.7,
        22.7,
        22.7,
        22.7,
        22.7,
        22.7,
        22.7,
        22.7,
        22.7,
        22.7,
        22.7,
        22.7,
        22.7,
        22.6,
        22.7,
        22.7,
        22.6,
        22.7,
        22.6,
        22.6,
        22.6,
        22.6,
        22.7,
        22.6,
        22.6,
        22.6,
        22.6,
        22.6,
        22.6,
        22.6,
        22.6,
        22.6,
        22.6,
        22.6,
        22.6,
        22.6,
        22.6,
        22.6,
        22.6,
        22.6,
        22.6,
        22.6,
        22.6,
        22.6,
        22.6,
        22.6,
        22.5,
        22.5,
        22.5,
        22.5,
        22.5,
        22.5,
        22.5,
        22.5,
        22.5,
        22.5,
        22.5,
        22.5,
        22.5,
        22.4,
        22.4,
        22.4,
        22.4,
        22.4,
        22.4,
        22.4,
        22.4,
        22.4,
        22.4,
        22.4,
        22.4,
        22.4,
        22.4,
        22.4,
        22.4,
        22.4,
        22.4,
        22.4,
        22.4,
        22.4,
        22.4,
        22.4,
        22.4,
        22.4,
        22.4,
        22.4,
        22.4,
        22.4,
        22.4,
        22.4,
        22.4,
        22.4,
        22.4,
        22.4,
        22.4,
        22.4,
        22.4,
        22.4,
        22.4,
        22.4,
        22.4,
        22.4,
        22.4,
        22.4
    ],
    "pressure": 972.1,
    "humidity": 65.3}
            
        else:
            with open(SENSOR_FILE, 'r', encoding='utf-8') as f:
                fcntl.flock(f, fcntl.LOCK_SH)
                sensor_data = json.load(f) 
        return sensor_data

    def get_open_meteo_air_quality(self, lat, long):
        url = OPEN_METEO_AIR_QUALITY_URL.format(lat=lat, long=long)
        response = requests.get(url)
        if not 200 <= response.status_code < 300:
            logging.error(f"Failed to retrieve Open-Meteo air quality data: {response.content}")
            raise RuntimeError("Failed to retrieve Open-Meteo air quality data.")
        
        return response.json()
    
    
    def format_time(self, dt, time_format, hour_only=False, include_am_pm=True):
        """Format datetime based on 12h or 24h preference"""
        if time_format == "24h":
            return dt.strftime("%H:00" if hour_only else "%H:%M")
        
        if include_am_pm:
            fmt = "%-I %p" if hour_only else "%-I:%M %p"
        else:
            fmt = "%-I" if hour_only else "%-I:%M"

        return dt.strftime(fmt).lstrip("0")
