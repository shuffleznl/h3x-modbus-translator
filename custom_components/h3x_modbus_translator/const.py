"""Integration identity and the dashboard entity contract."""

DOMAIN = "h3x_modbus_translator"
NAME = "H3X Modbus Translator"
PROFILE = "SDM630"
PLATFORMS = ["sensor", "binary_sensor"]

# These suffixes are also used by the packaged Lovelace dashboard.
SENSOR_KEYS = (
    "profile",
    "status",
    "grid_power",
    "grid_import",
    "grid_export",
    "import_energy",
    "export_energy",
    "data_age",
    "requests",
    "rejected_requests",
    "poll_errors",
    "last_client_request",
    "endpoint",
    "last_error",
)
BINARY_KEYS = ("source_online", "server_listening", "client_active")
