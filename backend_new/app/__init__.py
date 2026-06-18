"""
Application Factory
===================
Creates and configures the Flask application.

This module implements the application factory pattern for Flask,
allowing for different configurations in development, testing, and production.
"""

import sys
import logging
from flask import Flask, send_from_directory, request, jsonify

from config import Config


def create_app(config_class=Config):
    """
    Create and configure the Flask application.
    
    Args:
        config_class: Configuration class to use (default: Config)
    
    Returns:
        Configured Flask application
    """
    # Validate environment on startup
    try:
        config_class.validate_environment()
    except Exception as e:
        print(f"❌ Environment validation failed: {e}")
        sys.exit(1)
    
    # Create Flask app
    app = Flask(__name__, static_folder='static')
    app.config.from_object(config_class)
    
    # Setup logging
    from app.utils.helpers import setup_logging
    logger = setup_logging(level=config_class.LOG_LEVEL)
    logger.info("Environment validation passed")
    
    # Initialize extensions
    from app.extensions import init_extensions
    init_extensions(app)
    
    # Initialize database
    try:
        logger.info("Database initialized successfully")
    except Exception as e:
        logger.error(f"Database initialization failed: {e}")
    
    # Register middleware
    register_middleware(app)
    
    # Register blueprints
    register_blueprints(app)
    
    # Register WebSocket handlers
    from app.extensions import sock
    register_websockets(sock)
    
    # Register root routes
    register_root_routes(app)
    
    # Register error handlers
    register_error_handlers(app)
    
    return app


def register_middleware(app):
    """Register request/response middleware."""
    from app.utils.metrics import metrics, MetricNames
    from app.utils.security import api_rate_limiter, IPUtils
    
    @app.before_request
    def before_request_handler():
        """Pre-request middleware for rate limiting and metrics."""
        # Track request
        if Config.ENABLE_METRICS:
            metrics.counter_inc("http_requests_total", labels={"method": request.method})
        
        # Skip rate limiting for certain paths
        skip_paths = ['/metrics', '/health', '/console', '/ws-test']
        if request.path in skip_paths or request.path.startswith('/static'):
            return
        
        # Rate limiting
        client_ip = IPUtils.get_client_ip(request)
        allowed, info = api_rate_limiter.is_allowed(client_ip)
        
        if not allowed:
            if Config.ENABLE_METRICS:
                metrics.counter_inc("rate_limit_exceeded_total")
            response = jsonify({
                "error": "Rate limit exceeded",
                "retry_after": info['retry_after']
            })
            response.status_code = 429
            response.headers['Retry-After'] = str(info['retry_after'])
            return response
    
    @app.after_request
    def after_request_handler(response):
        """Post-request middleware for logging and metrics."""
        if Config.ENABLE_METRICS:
            metrics.counter_inc(
                "http_responses_total",
                labels={"method": request.method, "status": str(response.status_code)}
            )
        return response


def register_blueprints(app):
    """Register all application blueprints."""
    from app.views.plivo_routes import plivo_bp
    from app.views.call_management import call_bp
    from app.views.operator_chat import operator_bp
    from app.views.media_streams import media_streams_bp
    
    app.register_blueprint(plivo_bp)        # /voice, /process-speech, /check-response, /call-status
    app.register_blueprint(call_bp)         # /make-call, /active-calls, /end-call
    app.register_blueprint(operator_bp)     # /send-message
    app.register_blueprint(media_streams_bp)  # /voice-stream (Media Streams)


def register_websockets(sock):
    """Register WebSocket handlers."""
    from app.views.operator_chat import register_websocket
    from app.views.media_streams import register_media_stream_websocket
    
    register_websocket(sock)  # /operator-ws/<call_sid>
    register_media_stream_websocket(sock)  # /media-stream/<call_sid>
    
    # Simple WebSocket test endpoint
    @sock.route('/ws-test')
    def ws_test(ws):
        print("🧪 WebSocket test connection received!")
        ws.send('{"status": "connected"}')
        while True:
            data = ws.receive()
            if data is None:
                break
            print(f"🧪 WebSocket test received: {data}")
            ws.send(f'{{"echo": "{data}"}}')


def register_root_routes(app):
    """Register root-level routes."""
    
    @app.route("/")
    def index():
        """Health check"""
        return {
            "status": "running",
            "service": "AI Voice Calling System",
            "endpoints": {
                "plivo": ["/voice", "/process-speech", "/check-response/<call_uuid>", "/call-status"],
                "calls": ["/make-call", "/active-calls", "/answer-call/<call_uuid>", "/hold-call/<call_uuid>", "/unhold-call/<call_uuid>", "/end-call/<call_uuid>", "/call-history"],
                "operator": ["/send-message/<call_uuid>", "/operator-ws/<call_uuid>"],
                "console": "/console"
            }
        }
    
    @app.route("/console")
    def console():
        """Serve operator console"""
        return send_from_directory('static', 'index.html')
    
    @app.route("/metrics")
    def metrics_endpoint():
        """
        Production monitoring endpoint.
        Returns latency stats for STT, TTS, and translation.
        Also includes Prometheus-compatible metrics.
        """
        from app.services.stt import get_stt_stats, get_active_stream_count
        from app.views.media_streams import get_latency_stats, get_active_streams
        from app.utils.cache import get_all_cache_stats
        from app.utils.metrics import metrics
        
        return {
            "status": "production",
            "service": "Real-Time Dubbing System",
            "stt": get_stt_stats(),
            "tts": get_latency_stats(),
            "streams": {
                "active_count": get_active_stream_count(),
                "details": get_active_streams()
            },
            "cache": get_all_cache_stats(),
            "metrics": metrics.get_all_metrics() if Config.ENABLE_METRICS else {},
            "targets": {
                "stt_latency_ms": 200,
                "tts_latency_ms": 500,
                "total_e2e_ms": 800
            }
        }
    
    @app.route("/metrics/prometheus")
    def prometheus_metrics():
        """Prometheus-compatible metrics endpoint."""
        from app.utils.metrics import metrics
        if not Config.ENABLE_METRICS:
            return "# Metrics disabled\n", 200, {'Content-Type': 'text/plain'}
        return metrics.get_prometheus_format(), 200, {'Content-Type': 'text/plain'}
    
    @app.route("/health")
    def health_check():
        """Health check endpoint for load balancers."""
        from datetime import datetime
        return {"status": "healthy", "timestamp": datetime.utcnow().isoformat()}


def register_error_handlers(app):
    """Register global error handlers."""
    from app.utils.exceptions import create_error_response
    from app.utils.metrics import metrics, MetricNames
    
    logger = logging.getLogger(__name__)
    
    @app.errorhandler(Exception)
    def handle_exception(e):
        """Global exception handler."""
        logger.error(f"Unhandled exception: {e}", exc_info=True)
        if Config.ENABLE_METRICS:
            metrics.counter_inc(MetricNames.ERRORS_TOTAL)
        return jsonify(create_error_response(e)), 500


def register_shutdown_handlers():
    """Register cleanup handlers for graceful shutdown."""
    from app.utils.helpers import on_shutdown
    from app.utils.cache import stop_cache_cleanup_thread
    
    @on_shutdown(priority=10)
    def cleanup_cache():
        """Stop cache cleanup thread on shutdown."""
        stop_cache_cleanup_thread()
    
    @on_shutdown(priority=20)
    def cleanup_streams():
        """Clean up active streams on shutdown."""
        from app.views.media_streams import cleanup_all_streams
        try:
            cleanup_all_streams()
        except Exception as e:
            logging.getLogger(__name__).error(f"Error cleaning up streams: {e}")
