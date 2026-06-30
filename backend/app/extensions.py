"""Shared Flask extensions."""

from flask_sock import Sock

# WebSocket handler for real-time push to admin UI / per-call event streams.
sock = Sock()
