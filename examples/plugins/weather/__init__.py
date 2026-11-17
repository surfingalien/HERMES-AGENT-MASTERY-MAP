"""Example plugin: copy to ~/.hermes/plugins/weather and run `hermes plugins enable weather`."""

import json
import urllib.parse
import urllib.request


def current_weather(args, ctx):
    q = urllib.parse.urlencode({"name": args["city"], "count": 1})
    geo = json.load(urllib.request.urlopen(f"https://geocoding-api.open-meteo.com/v1/search?{q}", timeout=20))
    if not geo.get("results"):
        return {"error": f"city not found: {args['city']}"}
    place = geo["results"][0]
    q = urllib.parse.urlencode({"latitude": place["latitude"], "longitude": place["longitude"], "current": "temperature_2m,wind_speed_10m"})
    data = json.load(urllib.request.urlopen(f"https://api.open-meteo.com/v1/forecast?{q}", timeout=20))
    return {"place": place["name"], "country": place.get("country"), "current": data.get("current")}


def register(ctx):
    ctx.register_tool(
        name="current_weather",
        toolset="weather",
        description="Get the current temperature and wind for a city.",
        parameters={"type": "object", "properties": {"city": {"type": "string"}}, "required": ["city"]},
        handler=current_weather,
    )
    ctx.register_command("weather", lambda arg, **kw: json.dumps(current_weather({"city": arg or "London"}, None)))
