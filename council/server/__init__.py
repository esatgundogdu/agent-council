"""The daemon: the one process that owns every council and every harness call.

Deliberately without re-exports. Importing this package used to pull in `app`, and so
FastAPI and the whole registry — which meant `council status`, a command that needs
only a port and a token out of `daemon.py`, paid for the web framework every time it
ran. Import the module you want: `from .server import daemon`.
"""
