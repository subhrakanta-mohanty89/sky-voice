"""
AI Client Module
================
Provider-agnostic AI client supporting OpenAI, Gemini, and other compatible APIs.
Configure via .env:
  AI_MODEL=gpt-5-mini (or gemini-2.0-flash, etc.)
  AI_API_KEY=your-api-key
  AI_BASE_URL=https://api.openai.com/v1
"""

import os
import time
import logging
from dotenv import load_dotenv

load_dotenv()

# ===========================================
# CONFIGURATION
# ===========================================

AI_MODEL = os.getenv("AI_MODEL", "gpt-4.1-mini-2025-04-14")
AI_API_KEY = os.getenv("AI_API_KEY")
AI_BASE_URL = os.getenv("AI_BASE_URL", "https://api.openai.com/v1")
AI_REQUEST_TIMEOUT = int(os.getenv("AI_REQUEST_TIMEOUT", "3"))
AI_MAX_RETRIES = int(os.getenv("AI_MAX_RETRIES", "0"))

# Global client instance
_ai_client = None


class AIServiceError(Exception):
    """Custom exception for AI service failures"""
    pass


def _backoff_sleep(attempt):
    """Exponential backoff with jitter"""
    import random
    sleep_time = min(2 ** attempt + random.uniform(0, 1), 30)
    logging.info(f"[AI_CLIENT] Retry backoff: sleeping {sleep_time:.2f}s")
    time.sleep(sleep_time)


def _log_ai_usage(operation, model, usage, tag=None, company_id=None, user_id=None):
    """Log AI usage for monitoring/billing"""
    if usage:
        prompt_tokens = getattr(usage, 'prompt_tokens', 0)
        completion_tokens = getattr(usage, 'completion_tokens', 0)
        total_tokens = getattr(usage, 'total_tokens', 0)
        
        # Check for reasoning tokens
        reasoning_tokens = 0
        if hasattr(usage, 'completion_tokens_details') and usage.completion_tokens_details:
            reasoning_tokens = getattr(usage.completion_tokens_details, 'reasoning_tokens', 0)
        
        logging.info(
            "[AI_USAGE] op=%s model=%s tag=%s prompt=%d completion=%d reasoning=%d total=%d",
            operation, model, tag or "-",
            prompt_tokens, completion_tokens, reasoning_tokens, total_tokens
        )


def ensure_ai_client():
    """
    Initialize and return the AI client.
    Uses AI_API_KEY and AI_BASE_URL from .env for any OpenAI-compatible API.
    """
    global _ai_client
    
    if _ai_client is not None:
        return _ai_client
    
    try:
        from openai import OpenAI
        _ai_client = OpenAI(
            api_key=AI_API_KEY,
            base_url=AI_BASE_URL
        )
        logging.info(f"[AI_CLIENT] Initialized client with base_url={AI_BASE_URL}")
    except Exception as e:
        raise AIServiceError(f"Failed to initialize AI client: {e}")
    
    return _ai_client


def ai_chat(
    messages,
    model=None,
    usage_tag=None,
    usage_company_id=None,
    usage_user_id=None,
    timeout=None,
    max_tokens=None,
    temperature=None,
    top_p=None,
):
    """
    Send a chat completion request to the AI service.
    
    Args:
        messages: List of message dicts with 'role' and 'content'
        model: Model to use (defaults to AI_MODEL)
        usage_tag: Tag for usage tracking
        timeout: Request timeout in seconds
        temperature: Sampling temperature
        top_p: Top-p sampling
    
    Returns:
        The response content string
    
    Raises:
        AIServiceError: If the request fails
    """
    client = ensure_ai_client()
    model = model or AI_MODEL
    timeout = timeout or AI_REQUEST_TIMEOUT
    
    kwargs = {
        "model": model,
        "messages": messages,
        "timeout": timeout,
    }
    
    if temperature is not None:
        kwargs["temperature"] = temperature
    if top_p is not None:
        kwargs["top_p"] = top_p
    
    attempt = 0
    last_error = None
    
    while attempt <= AI_MAX_RETRIES:
        try:
            t_start = time.time()
            response = client.chat.completions.create(**kwargs)
            t_elapsed = time.time() - t_start
            
            # Log usage
            _log_ai_usage(
                "chat",
                model,
                response.usage,
                tag=usage_tag,
                company_id=usage_company_id,
                user_id=usage_user_id
            )
            
            logging.info(f"[AI_CLIENT] Chat completed in {t_elapsed:.3f}s")
            
            # Return full response object for compatibility
            return response
            
        except Exception as e:
            last_error = e
            logging.warning(f"[AI_CLIENT] Request failed (attempt {attempt + 1}): {e}")
            
            if attempt < AI_MAX_RETRIES:
                _backoff_sleep(attempt)
            
            attempt += 1
    
    raise AIServiceError(f"AI request failed after {AI_MAX_RETRIES + 1} attempts: {last_error}")


def ai_translate(text, target_language, source_language=None, usage_tag="translation"):
    """
    Convenience function for translation using ai_chat.
    
    Args:
        text: Text to translate
        target_language: Target language name (e.g., "Telugu", "Hindi", "English")
        source_language: Source language name (optional, auto-detected if not provided)
        usage_tag: Tag for usage logging
    
    Returns:
        Translated text string
    """
    if not text or not text.strip():
        return text
    
    if source_language:
        system_prompt = f"""You are a real-time voice translator. 
Translate the following text from {source_language} to {target_language}.
Output ONLY the translation, nothing else. No explanations, no labels, no quotation marks."""
    else:
        system_prompt = f"""You are a real-time voice translator.
Translate the following text to {target_language}.
Output ONLY the translation, nothing else. No explanations, no labels, no quotation marks."""
    
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": text}
    ]
    
    try:
        response = ai_chat(messages, usage_tag=usage_tag, temperature=0.1)
        translated = response.choices[0].message.content
        if translated:
            return translated.strip()
        return text  # Fallback to original if empty
    except AIServiceError as e:
        logging.error(f"[AI_TRANSLATE] Failed: {e}")
        return text  # Fallback to original on error
