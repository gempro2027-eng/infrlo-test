import os

bind = "0.0.0.0:" + os.environ.get("PORT", "8000")
workers = 2
timeout = 120
