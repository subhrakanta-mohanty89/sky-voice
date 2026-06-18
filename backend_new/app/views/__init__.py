"""
Views Package
=============
API routes and WebSocket handlers.
"""

from app.views.plivo_routes import plivo_bp
from app.views.call_management import call_bp
from app.views.operator_chat import operator_bp, register_websocket, notify_operator
from app.views.media_streams import (
    media_streams_bp,
    register_media_stream_websocket,
    start_audio_stream,
    stop_audio_stream,
    get_latency_stats,
    get_active_streams,
    cleanup_all_streams,
)

__all__ = [
    'plivo_bp',
    'call_bp',
    'operator_bp',
    'media_streams_bp',
    'register_websocket',
    'register_media_stream_websocket',
    'notify_operator',
    'start_audio_stream',
    'stop_audio_stream',
    'get_latency_stats',
    'get_active_streams',
    'cleanup_all_streams',
]
