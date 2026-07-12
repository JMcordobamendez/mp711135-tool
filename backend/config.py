import os

PORT = os.environ.get("MP711135_PORT", "/dev/mp711135")
BAUD = int(os.environ.get("MP711135_BAUD", "115200"))
TIMEOUT = float(os.environ.get("MP711135_TIMEOUT", "2.0"))
POLL_HZ = float(os.environ.get("MP711135_POLL_HZ", "5"))
