#  Orchestration Engine - Rate Limiter
#
#  Shared limiter instance used by app.py and route decorators.
#
#  Depends on: config.py
#  Used by:    app.py, routes/projects.py

from slowapi import Limiter
from slowapi.util import get_remote_address

from backend.config import cfg

_rate_limit = cfg("server.rate_limit", "60/minute")
limiter = Limiter(key_func=get_remote_address, default_limits=[_rate_limit])
