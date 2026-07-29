"""Constants for the Hildebrand Glow (DCC) integration."""

DOMAIN = "hildebrandglow_dcc"

# Keys used in hass.data[DOMAIN][entry_id]
DATA_CLIENT = "client"
# Resource IDs discovered during sensor setup, so that the services can act
# on every meter of an entry without walking the API again
DATA_RESOURCES = "resources"

SERVICE_CATCHUP = "catchup"
SERVICE_CLEAR_CACHE = "clear_cache"
