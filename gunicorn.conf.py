import os

bind = "0.0.0.0:" + os.environ.get("PORT", "8000")
workers = 1
timeout = 120
