"""
Translation Service
===================
PRODUCTION-LEVEL real-time voice translation using Google Cloud Translation NMT.

Optimizations:
- Google Cloud Translation (NMT) - fastest translation API
- ~50-100ms latency vs 500ms+ for LLM
- Smart caching for repeated phrases
"""

import html
import time
from google.cloud import translate_v2 as translate

from config import Config

# Initialize Google Cloud Translation client
_translate_client = None


def get_translate_client():
    """Get or create Google Cloud Translation client."""
    global _translate_client
    if _translate_client is None:
        _translate_client = translate.Client()
        print("🌐 Google Cloud Translation client initialized")
    return _translate_client


# Simple cache for repeated translations
_translation_cache = {}
_cache_max_size = 100

# Language code mapping for Google Translate
GOOGLE_LANG_MAP = {
    "hi": "hi",  # Hindi
    "en": "en",  # English
    "te": "te",  # Telugu
    "ta": "ta",  # Tamil
    "kn": "kn",  # Kannada
    "mr": "mr",  # Marathi
    "ml": "ml",  # Malayalam
    "ur": "ur",  # Urdu
}


def translate_text(text, target_language, source_language=None):
    """
    PRODUCTION: Ultra-fast translation using Google Cloud Translation NMT.
    
    Target latency: <100ms per translation (vs 500ms+ for LLM)
    
    Args:
        text: Text to translate
        target_language: Target language code
        source_language: Source language code (optional, auto-detected if not provided)
    
    Returns:
        Translated text
    """
    if not text or not text.strip():
        return text
    
    t_start = time.time()
    
    # Check cache first
    cache_key = f"{source_language}:{target_language}:{text.lower().strip()}"
    if cache_key in _translation_cache:
        print(f"⚡ Cache hit: '{text[:20]}...'")
        return _translation_cache[cache_key]
    
    try:
        # Determine source language
        if source_language:
            source_lang = source_language
        elif target_language == Config.OPERATOR_LANG:
            source_lang = Config.CUSTOMER_LANG
        else:
            source_lang = Config.OPERATOR_LANG
        
        # Map to Google Translate language codes
        google_source = GOOGLE_LANG_MAP.get(source_lang, source_lang)
        google_target = GOOGLE_LANG_MAP.get(target_language, target_language)
        
        # Use Google Cloud Translation API (NMT - Neural Machine Translation)
        client = get_translate_client()
        result = client.translate(
            text,
            target_language=google_target,
            source_language=google_source,
            format_="text"
        )
        
        translated = result["translatedText"]
        
        # Decode HTML entities that Google sometimes returns
        if translated:
            translated = html.unescape(translated).strip()
        else:
            translated = text
        
        # Cache the result
        if len(_translation_cache) >= _cache_max_size:
            # Clear oldest half
            keys = list(_translation_cache.keys())
            for k in keys[:len(keys)//2]:
                del _translation_cache[k]
        _translation_cache[cache_key] = translated
        
        t_elapsed = time.time() - t_start
        print(f"🔄 [{t_elapsed:.3f}s] {source_lang}→{target_language}: '{text[:25]}' → '{translated[:25]}'")
        
        return translated
        
    except Exception as e:
        print(f"❌ Google Translation Error: {e}")
        return text
