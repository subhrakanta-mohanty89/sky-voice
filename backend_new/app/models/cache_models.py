"""
Cache Models
============
SQLAlchemy models for caching translations and TTS audio.
"""

import hashlib
from datetime import datetime, timedelta
from typing import Optional

from app.extensions import db


class TranslationCache(db.Model):
    """Model for caching translations to avoid repeated API calls."""
    __tablename__ = 'translation_cache'

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    
    # Cache key components
    source_text_hash = db.Column(db.String(64), nullable=False, index=True)  # SHA-256 hash
    source_language = db.Column(db.String(10), nullable=False)
    target_language = db.Column(db.String(10), nullable=False)
    
    # Cached content
    source_text = db.Column(db.Text, nullable=False)
    translated_text = db.Column(db.Text, nullable=False)
    
    # Cache metadata
    hit_count = db.Column(db.Integer, default=0)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    last_accessed_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    expires_at = db.Column(db.DateTime, nullable=True)
    
    # Indexes
    __table_args__ = (
        db.Index('idx_cache_lookup', 'source_text_hash', 'source_language', 'target_language'),
        db.Index('idx_cache_expiry', 'expires_at'),
    )
    
    def __repr__(self):
        return f'<TranslationCache {self.source_language}->{self.target_language}>'


class TTSCache(db.Model):
    """Model for caching TTS audio to avoid repeated API calls."""
    __tablename__ = 'tts_cache'

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    
    # Cache key components
    text_hash = db.Column(db.String(64), nullable=False, index=True)  # SHA-256 hash
    language = db.Column(db.String(10), nullable=False)
    voice_name = db.Column(db.String(100), nullable=True)
    
    # Cached content
    text = db.Column(db.Text, nullable=False)
    audio_content = db.Column(db.Text, nullable=False)  # Base64 encoded audio
    audio_format = db.Column(db.String(20), default='mp3')
    
    # Cache metadata
    hit_count = db.Column(db.Integer, default=0)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    last_accessed_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    
    # Indexes
    __table_args__ = (
        db.Index('idx_tts_cache_lookup', 'text_hash', 'language'),
    )
    
    def __repr__(self):
        return f'<TTSCache {self.language}>'


# ===========================================
# REPOSITORY FUNCTIONS
# ===========================================

def cache_translation(
    source_text: str,
    source_language: str,
    target_language: str,
    translated_text: str,
    ttl_seconds: Optional[int] = None
) -> None:
    """Cache a translation for future use."""
    text_hash = hashlib.sha256(source_text.encode()).hexdigest()
    expires_at = None
    if ttl_seconds:
        expires_at = datetime.utcnow() + timedelta(seconds=ttl_seconds)
    
    # Check if exists
    existing = TranslationCache.query.filter_by(
        source_text_hash=text_hash,
        source_language=source_language,
        target_language=target_language
    ).first()
    
    if existing:
        existing.translated_text = translated_text
        existing.last_accessed_at = datetime.utcnow()
        existing.hit_count += 1
    else:
        cache_entry = TranslationCache(
            source_text_hash=text_hash,
            source_language=source_language,
            target_language=target_language,
            source_text=source_text,
            translated_text=translated_text,
            expires_at=expires_at
        )
        db.session.add(cache_entry)
    
    db.session.commit()


def get_cached_translation(
    source_text: str,
    source_language: str,
    target_language: str
) -> Optional[str]:
    """Get a cached translation if available."""
    text_hash = hashlib.sha256(source_text.encode()).hexdigest()
    
    cache_entry = TranslationCache.query.filter_by(
        source_text_hash=text_hash,
        source_language=source_language,
        target_language=target_language
    ).first()
    
    if cache_entry:
        # Check expiry
        if cache_entry.expires_at and cache_entry.expires_at < datetime.utcnow():
            db.session.delete(cache_entry)
            db.session.commit()
            return None
        
        # Update hit count and access time
        cache_entry.hit_count += 1
        cache_entry.last_accessed_at = datetime.utcnow()
        db.session.commit()
        return cache_entry.translated_text
    
    return None
