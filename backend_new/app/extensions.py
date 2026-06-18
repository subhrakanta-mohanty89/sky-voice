"""
Flask Extensions
================
Initialize Flask extensions here to avoid circular imports.
"""

from flask_sqlalchemy import SQLAlchemy
from flask_sock import Sock
from flask_cors import CORS

# SQLAlchemy database instance
db = SQLAlchemy()

# WebSocket support
sock = Sock()

# CORS handler (will be initialized with app)
cors = CORS()


def init_extensions(app):
    """Initialize all Flask extensions with the app."""
    # Initialize SQLAlchemy
    db.init_app(app)
    
    # Initialize WebSocket
    sock.init_app(app)
    
    # Initialize CORS
    cors.init_app(app)
    
    # Create tables within app context
    with app.app_context():
        db.create_all()
