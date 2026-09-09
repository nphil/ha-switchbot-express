"""Keep pytest from importing the integration package.

``policy.py`` is deliberately free of Home Assistant, bleak and pySwitchbot
imports; every other module in the package needs a running Core. The tests
import ``policy`` directly from the package directory.
"""

collect_ignore = ["../custom_components"]
