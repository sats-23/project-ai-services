"""
LLM-based prompt validation for custom system prompts.

This module provides semantic validation and prompt injection detection
using the LLM itself to ensure custom prompts are safe and appropriate.
"""
import json
from typing import Tuple, Optional
from enum import Enum

from common.misc_utils import get_logger
from common.settings import settings
from common.llm_utils import get_vllm_headers
import common.misc_utils as misc_utils

logger = get_logger("prompt_validator")


class ValidationResult(Enum):
    """Validation result status."""
    VALID = "valid"
    INVALID_SEMANTIC = "invalid_semantic"
    UNSAFE_INJECTION = "unsafe_injection"
    VALIDATION_ERROR = "validation_error"
    VALIDATION_DISABLED = "validation_disabled"


class PromptValidationResponse:
    """Response from prompt validation."""
    
    def __init__(self, result: ValidationResult, reason: str = "", _confidence: float = 0.0):
        self.result = result
        self.reason = reason
        self._confidence = _confidence
    
    def is_valid(self) -> bool:
        """Check if validation passed."""
        return self.result in [ValidationResult.VALID, ValidationResult.VALIDATION_DISABLED]
    
    def __repr__(self):
        return f"PromptValidationResponse(result={self.result.value}, reason='{self.reason}')"


def _call_llm_for_validation(prompt: str, validation_type: str) -> str:
    """
    Internal function to call LLM for validation.
    
    Args:
        prompt: The validation prompt to send to LLM
        validation_type: Type of validation (for logging)
    
    Returns:
        Response text from LLM
    """
    if misc_utils.SESSION is None:
        logger.warning("LLM session not initialized. Skipping LLM-based validation.")
        return ""
    
    llm_endpoint = settings.llm.endpoint
    llm_model = settings.llm.model
    api_key = settings.llm.api_key
    
    if not llm_endpoint or not llm_model:
        logger.warning("LLM endpoint or model not configured. Skipping LLM-based validation.")
        return ""
    
    payload = {
        "model": llm_model,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.0,  # Deterministic for validation
        "max_tokens": 300,
        "stream": False,
    }
    
    try:
        response = misc_utils.SESSION.post(
            f"{llm_endpoint}/v1/chat/completions",
            json=payload,
            headers=get_vllm_headers(api_key),
            timeout=20.0  # 20 second timeout for validation
        )
        response.raise_for_status()
        data = response.json() or {}
        choices = data.get("choices", [])
        
        if not choices:
            logger.warning(f"{validation_type} validation: No response from LLM")
            return ""
        
        text = (choices[0].get("message", {}).get("content") or "").strip()
        logger.debug(f"{validation_type} validation response: {text}")
        return text
        
    except Exception as e:
        logger.error(f"Error during {validation_type} validation: {e}")
        return ""


def _parse_validation_response(
    response_text: str,
    valid_verdict: str,
    invalid_verdict: str,
    invalid_result_type: ValidationResult,
    validation_type: str
) -> PromptValidationResponse:
    """
    Parse LLM validation response in standard format.
    
    Args:
        response_text: Raw response text from LLM
        valid_verdict: Expected verdict string for valid result (e.g., "VALID", "SAFE")
        invalid_verdict: Expected verdict string for invalid result (e.g., "INVALID", "UNSAFE")
        invalid_result_type: ValidationResult enum to return for invalid verdict
        validation_type: Type of validation for logging (e.g., "Semantic", "Injection Detection")
    
    Returns:
        PromptValidationResponse with parsed result
    """
    try:
        lines = response_text.strip().split('\n')
        verdict = None
        reason = ""
        confidence = 0.0
        
        for line in lines:
            line = line.strip()
            if line.startswith("VERDICT:"):
                verdict = line.split(":", 1)[1].strip().upper()
            elif line.startswith("REASON:"):
                reason = line.split(":", 1)[1].strip()
            elif line.startswith("CONFIDENCE:"):
                try:
                    confidence = float(line.split(":", 1)[1].strip())
                except ValueError:
                    confidence = 0.5
        
        if verdict == valid_verdict.upper():
            logger.debug(f"{validation_type} validation passed with confidence: {confidence:.2f}")
            return PromptValidationResponse(ValidationResult.VALID, reason, confidence)
        elif verdict == invalid_verdict.upper():
            logger.debug(f"{validation_type} validation failed with confidence: {confidence:.2f}")
            return PromptValidationResponse(invalid_result_type, reason, confidence)
        else:
            logger.warning(f"Unexpected verdict from LLM: {verdict}")
            return PromptValidationResponse(
                ValidationResult.VALIDATION_ERROR,
                f"Could not parse LLM {validation_type.lower()} response"
            )
            
    except Exception as e:
        logger.error(f"Error parsing {validation_type.lower()} response: {e}")
        return PromptValidationResponse(
            ValidationResult.VALIDATION_ERROR,
            f"Error parsing validation response: {str(e)}"
        )


def validate_semantic_quality(prompt: str, prompt_type: str = "system") -> PromptValidationResponse:
    """
    Validate the semantic quality and appropriateness of a custom prompt using LLM.
    
    Args:
        prompt: The custom prompt to validate
        prompt_type: Type of prompt (e.g., "system", "initial", "query")
    
    Returns:
        PromptValidationResponse with validation result
    """
    # Import settings here to avoid circular imports
    from chatbot.settings import settings as chatbot_settings
    
    # Get validation prompt template from settings
    validation_prompt = chatbot_settings.chatbot.semantic_validation_prompt_template.format(
        prompt_type=prompt_type,
        prompt=prompt
    )

    response_text = _call_llm_for_validation(validation_prompt, "Semantic")
    
    if not response_text:
        # If LLM validation fails, return disabled status (allows fallback to basic validation)
        return PromptValidationResponse(
            ValidationResult.VALIDATION_DISABLED,
            "LLM validation unavailable, using basic validation only"
        )
    
    # Parse response using shared method
    return _parse_validation_response(
        response_text,
        valid_verdict="VALID",
        invalid_verdict="INVALID",
        invalid_result_type=ValidationResult.INVALID_SEMANTIC,
        validation_type="Semantic"
    )


def detect_prompt_injection(prompt: str) -> PromptValidationResponse:
    """
    Detect potential prompt injection attempts in custom prompts using LLM.
    
    Args:
        prompt: The custom prompt to check for injection attempts
    
    Returns:
        PromptValidationResponse with detection result
    """
    # Import settings here to avoid circular imports
    from chatbot.settings import settings as chatbot_settings
    
    # Get injection detection prompt template from settings
    validation_prompt = chatbot_settings.chatbot.injection_detection_prompt_template.format(
        prompt=prompt
    )

    response_text = _call_llm_for_validation(validation_prompt, "Injection Detection")
    
    if not response_text:
        # If LLM validation fails, return disabled status (allows fallback to basic validation)
        return PromptValidationResponse(
            ValidationResult.VALIDATION_DISABLED,
            "LLM injection detection unavailable, using basic validation only"
        )
    
    # Parse response using shared method
    return _parse_validation_response(
        response_text,
        valid_verdict="SAFE",
        invalid_verdict="UNSAFE",
        invalid_result_type=ValidationResult.UNSAFE_INJECTION,
        validation_type="Injection Detection"
    )


def validate_prompt_with_llm(
    prompt: str,
    prompt_type: str = "system",
    enable_semantic_check: bool = True,
    enable_injection_check: bool = True
) -> PromptValidationResponse:
    """
    Comprehensive prompt validation using LLM for both semantic quality and injection detection.
    
    Args:
        prompt: The custom prompt to validate
        prompt_type: Type of prompt (e.g., "system", "initial", "query")
        enable_semantic_check: Whether to perform semantic validation
        enable_injection_check: Whether to perform injection detection
    
    Returns:
        PromptValidationResponse with overall validation result
    """
    logger.info(f"Starting LLM-based validation for {prompt_type} prompt (length: {len(prompt)} chars)")
    
    # Check for injection first (security priority)
    if enable_injection_check:
        injection_result = detect_prompt_injection(prompt)
        if not injection_result.is_valid():
            logger.warning(
                f"Prompt injection detected: {injection_result.reason} "
                f"(confidence: {injection_result._confidence:.2f})"
            )
            return injection_result
        logger.info(
            f"Injection check passed: {injection_result.reason} "
            f"(confidence: {injection_result._confidence:.2f})"
        )
    
    # Then check semantic quality
    if enable_semantic_check:
        semantic_result = validate_semantic_quality(prompt, prompt_type)
        if not semantic_result.is_valid():
            logger.warning(
                f"Semantic validation failed: {semantic_result.reason} "
                f"(confidence: {semantic_result._confidence:.2f})"
            )
            return semantic_result
        logger.info(
            f"Semantic check passed: {semantic_result.reason} "
            f"(confidence: {semantic_result._confidence:.2f})"
        )
    
    # All checks passed
    return PromptValidationResponse(
        ValidationResult.VALID,
        "All validation checks passed",
        1.0
    )
