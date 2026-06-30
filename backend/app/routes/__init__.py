"""Blueprint registry."""

from __future__ import annotations

from flask import Flask

from .agents import agents_bp
from .auth import auth_bp
from .calls import calls_bp
from .health import health_bp
from .legacy import legacy_bp
from .sims import sims_bp
from .sip import sip_bp
from .tenant import tenant_bp
from .user_auth import user_auth_bp
from .webhooks import webhooks_bp


def register_blueprints(app: Flask) -> None:
    api_v1 = "/api/v1"
    app.register_blueprint(health_bp)
    app.register_blueprint(user_auth_bp, url_prefix=f"{api_v1}/auth")
    app.register_blueprint(auth_bp, url_prefix=api_v1)
    app.register_blueprint(agents_bp, url_prefix=api_v1)
    app.register_blueprint(calls_bp, url_prefix=api_v1)
    app.register_blueprint(sims_bp, url_prefix=api_v1)
    app.register_blueprint(sip_bp, url_prefix=api_v1)
    app.register_blueprint(tenant_bp, url_prefix=api_v1)
    app.register_blueprint(legacy_bp, url_prefix="/api")
    app.register_blueprint(webhooks_bp, url_prefix="/twilio/voice")
