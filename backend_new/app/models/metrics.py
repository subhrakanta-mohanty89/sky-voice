"""
Call Metrics Model
==================
SQLAlchemy model for storing call metrics and analytics.
"""

from datetime import datetime
from typing import Dict, Any

from app.extensions import db


class CallMetrics(db.Model):
    """Model for storing call metrics for analytics."""
    __tablename__ = 'call_metrics'

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    call_uuid = db.Column(db.String(100), db.ForeignKey('calls.call_uuid'), nullable=False, index=True)
    
    # Performance metrics
    stt_latency_avg = db.Column(db.Float, nullable=True)  # Average STT latency in ms
    tts_latency_avg = db.Column(db.Float, nullable=True)  # Average TTS latency in ms
    translation_latency_avg = db.Column(db.Float, nullable=True)  # Average translation latency in ms
    
    # Quality metrics
    transcription_accuracy = db.Column(db.Float, nullable=True)  # 0-1 score
    audio_quality_score = db.Column(db.Float, nullable=True)  # 0-1 score
    
    # Count metrics
    total_messages = db.Column(db.Integer, default=0)
    customer_messages = db.Column(db.Integer, default=0)
    operator_messages = db.Column(db.Integer, default=0)
    translation_cache_hits = db.Column(db.Integer, default=0)
    translation_cache_misses = db.Column(db.Integer, default=0)
    
    # Error metrics
    stt_errors = db.Column(db.Integer, default=0)
    tts_errors = db.Column(db.Integer, default=0)
    translation_errors = db.Column(db.Integer, default=0)
    
    # Timestamps
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    def to_dict(self) -> Dict[str, Any]:
        """Convert metrics to dictionary."""
        return {
            'id': self.id,
            'call_uuid': self.call_uuid,
            'stt_latency_avg': self.stt_latency_avg,
            'tts_latency_avg': self.tts_latency_avg,
            'translation_latency_avg': self.translation_latency_avg,
            'transcription_accuracy': self.transcription_accuracy,
            'audio_quality_score': self.audio_quality_score,
            'total_messages': self.total_messages,
            'customer_messages': self.customer_messages,
            'operator_messages': self.operator_messages,
            'translation_cache_hits': self.translation_cache_hits,
            'translation_cache_misses': self.translation_cache_misses,
            'stt_errors': self.stt_errors,
            'tts_errors': self.tts_errors,
            'translation_errors': self.translation_errors,
        }
    
    def __repr__(self):
        return f'<CallMetrics for {self.call_uuid}>'
