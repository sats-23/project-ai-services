"""
Configuration settings for Chatbot/RAG service.
These values can be overridden via environment variables.
"""
from typing import ClassVar
from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from common.misc_utils import get_logger, create_llm_session
import common.misc_utils as misc_utils
from common.settings import Settings as CommonSettings

logger = get_logger("settings")

class QueryRephrasingConfig(BaseSettings):
    """Query rephrasing configuration for conversational RAG."""
    
    model_config = SettingsConfigDict(env_prefix="QUERY_REPHRASING_")
    
    timeout_seconds: float = Field(
        default=5.0,
        gt=0,
        description="Timeout for rephrasing LLM call in seconds"
    )
    
    max_response_tokens: int = Field(
        default=100,
        gt=0,
        le=615,
        description="Maximum tokens for rephrased query response (used as minimum baseline)"
    )
    
    max_response_tokens_multiplier: float = Field(
        default=1.2,
        gt=1.0,
        le=2.0,
        description="Multiplier for dynamic max_response_tokens calculation based on input query length"
    )
    
    temperature: float = Field(
        default=0.0,
        ge=0.0,
        le=1.0,
        description="Temperature for rephrasing (0=deterministic)"
    )
    
    history_token_budget: int = Field(
        default=1000,
        gt=0,
        description="Maximum tokens allocated for conversation history during query rephrasing"
    )
    
    rephrase_prompt_template_en: str = Field(
        default=(
            "Given the conversation history and the current question, create a standalone query for semantic search.\n\n"
            "Instructions:\n"
            "1. If the current question is already standalone and clear, return it EXACTLY as-is (preserve original wording)\n"
            "2. If the question references previous context (uses pronouns like 'it', 'this', 'that', 'they'), replace them with specific nouns from the conversation history\n"
            "3. Only merge context if the current question is clearly a follow-up that requires previous information\n"
            "4. Remove conversational filler words (e.g., 'Can you tell me', 'Also', 'Thanks', 'Please')\n"
            "5. Keep the query concise and focused on the core search intent\n"
            "6. If the conversation history is irrelevant to the current question, ignore it\n"
            "7. Return ONLY the rephrased query, no explanation or additional text\n\n"
            "Conversation History:\n{conversation_history}\n\n"
            "Current Question: {current_query}\n\n"
            "Rephrased Query:"
        ),
        description="English prompt template for query rephrasing with placeholders: {conversation_history}, {current_query}"
    )

    rephrase_prompt_template_de: str = Field(
        default=(
            "Erstelle anhand des Gesprächsverlaufs und der aktuellen Frage eine eigenständige Suchanfrage für die semantische Suche.\n\n"
            "Anweisungen:\n"
            "1. Wenn die aktuelle Frage bereits eigenständig und klar ist, gib sie GENAU unverändert zurück\n"
            "2. Wenn die Frage auf vorherigen Kontext verweist (z. B. mit Pronomen wie 'es', 'dies', 'diese', 'sie'), ersetze diese durch konkrete Begriffe aus dem Gesprächsverlauf\n"
            "3. Füge Kontext nur dann zusammen, wenn die aktuelle Frage eindeutig eine Anschlussfrage ist, die frühere Informationen benötigt\n"
            "4. Entferne überflüssige Gesprächsfloskeln (z. B. 'Kannst du mir sagen', 'Außerdem', 'Danke', 'Bitte')\n"
            "5. Halte die Suchanfrage kurz und auf die eigentliche Suchabsicht fokussiert\n"
            "6. Wenn der Gesprächsverlauf für die aktuelle Frage irrelevant ist, ignoriere ihn\n"
            "7. Gib NUR die umformulierte Suchanfrage zurück, ohne Erklärung oder zusätzlichen Text\n\n"
            "Gesprächsverlauf:\n{conversation_history}\n\n"
            "Aktuelle Frage: {current_query}\n\n"
            "Umformulierte Suchanfrage:"
        ),
        description="German prompt template for query rephrasing with placeholders: {conversation_history}, {current_query}"
    )


class LLMConfig(BaseSettings):
    """Chatbot-specific LLM generation settings."""

    max_tokens: int = Field(
        default=512,
        gt=0,
        description="Maximum tokens for LLM generation (English)",
    )

    max_tokens_de: int = Field(
        default=700,
        gt=0,
        description="Maximum tokens for LLM generation (German)",
    )

    temperature: float = Field(
        default=0.0,
        ge=0.0,
        lt=1.0,
        description="Temperature for LLM generation",
    )

    @field_validator('max_tokens')
    @classmethod
    def validate_max_tokens(cls, v):
        """Validate max_tokens with warning fallback."""
        if not (isinstance(v, int) and v > 0):
            logger.warning("Setting max_tokens to default '512' as it is missing or malformed in the settings")
            return 512
        return v

    @field_validator('max_tokens_de')
    @classmethod
    def validate_max_tokens_de(cls, v):
        """Validate max_tokens_de with warning fallback."""
        if not (isinstance(v, int) and v > 0):
            logger.warning("Setting max_tokens_de to default '700' as it is missing or malformed in the settings")
            return 700
        return v

    @field_validator('temperature')
    @classmethod
    def validate_temperature(cls, v):
        """Validate temperature with warning fallback."""
        if not (isinstance(v, float) and 0 <= v < 1):
            logger.warning("Setting temperature to default '0.0' as it is missing or malformed in the settings")
            return 0.0
        return v


class RAGConfig(BaseSettings):
    """RAG retrieval and ranking settings."""
    
    model_config = SettingsConfigDict(env_prefix="CHATBOT_")
    DEFAULT_SYSTEM_PROMPT_EN: ClassVar[str] = (
        "You are a helpful, conversational AI assistant. "
        "The conversation language is fixed to be English"
        "Engage naturally with users across multiple turns of conversation. "
        "Provide clear, accurate, and contextually relevant responses. "
        "Reference previous exchanges when appropriate to maintain conversation flow."
        "Answer only the specific question asked. Do not add conversational filler, offer additional assistance, suggest follow-up steps, or ask follow-up questions at the end of your response. End your response immediately once the question has been answered."
    )

    DEFAULT_SYSTEM_PROMPT_DE: ClassVar[str] = (
        "Sie sind ein hilfreicher, dialogorientierter KI-Assistent. "
        "Die Gesprächssprache ist für die gesamte Sitzung anhand der ersten Nachricht des Nutzers festgelegt. "
        "Antworten Sie immer nur in dieser Sitzungssprache, auch wenn spätere Nachrichten Sprachen mischen. "
        "Geben Sie klare, präzise und kontextbezogene Antworten. "
        "Beziehen Sie sich bei Bedarf auf frühere Nachrichten, um den Gesprächsfluss aufrechtzuerhalten. "
        "Beantworten Sie nur die konkret gestellte Frage. Fügen Sie keine Gesprächsfloskeln hinzu, "
        "bieten Sie keine zusätzliche Hilfe an, schlagen Sie keine nächsten Schritte vor und stellen Sie am Ende keine Rückfragen. "
        "Beenden Sie Ihre Antwort sofort, sobald die Frage beantwortet ist."
    )

    similarity_service_url: str = Field(
        default="http://similarity:8080",
        description="URL of the similarity search service"
    )

    rerank: bool = Field(
        default=True,
        description="Enable reranking of search results"
    )

    search_mode: str = Field(
        default="hybrid",
        description="Search mode for document retrieval (e.g., 'hybrid', 'dense', 'sparse')"
    )

    score_threshold: float = Field(
        default=0.4,
        gt=0.0,
        lt=1.0,
        description="Minimum similarity score threshold for retrieval",
    )

    num_chunks_post_search: int = Field(
        default=10,
        gt=5,
        le=15,
        description="Number of chunks to retrieve after initial search",
    )

    num_chunks_post_reranker: int = Field(
        default=3,
        gt=1,
        le=5,
        description="Number of chunks to keep after reranking",
    )

    max_query_token_length: int = Field(
        default=512,
        gt=0,
        description="Maximum token length for user queries",
    )


    system_prompt_en: str = Field(
        default=DEFAULT_SYSTEM_PROMPT_EN,
        description="English conversational system prompt for session-based behavior",
    )

    system_prompt_de: str = Field(
        default=DEFAULT_SYSTEM_PROMPT_DE,
        description="German conversational system prompt for session-based behavior",
    )

    query_system_prompt_en: str = Field(
        default=(
            "Session language: English\n\n"
            "Retrieved Context:\n{context}\n\n"
            "Rephrased Query: {rephrased_query}\n\n"
            "Instructions: Answer the user's question based on the retrieved context above. "
            "Consider the conversation history to provide contextually relevant responses. "
            "Be conversational and reference previous exchanges when relevant. "
            "If the context doesn't contain enough information, acknowledge this clearly."
        ),
        description="RAG system prompt template with context and rephrased query",
    )

    query_system_prompt_de: str = Field(
        default=(
            "Sitzungssprache: Deutsch\n\n"
            "Abgerufener Kontext:\n{context}\n\n"
            "Suchanfrage:\n{rephrased_query}\n\n"
            "Anweisungen: Beantworten Sie die aktuelle Frage des Nutzers anhand des oben abgerufenen Kontexts. "
            "Halten Sie einen natürlichen Gesprächsfluss aufrecht und beziehen Sie frühere Gesprächsbeiträge ein, wenn sie relevant sind. "
            "Antworten Sie ausschließlich auf Deutsch, weil die Sitzungssprache anhand der ersten Nachricht des Nutzers festgelegt wurde. "
            "Wenn der abgerufene Kontext nicht genügend Informationen enthält, sagen Sie das klar."
        ),
        description="German conversational RAG system prompt template with context and search query",
    )

    history_token_budget: int = Field(
        default=2000,
        gt=0,
        description="Maximum tokens allocated for conversation history",
    )

    initial_system_token_overhead: int = Field(
        default=100,
        gt=0,
        description="Estimated tokens for initial system message",
    )

    rag_system_token_overhead: int = Field(
        default=200,
        gt=0,
        description="Estimated tokens for RAG system message (excluding context)",
    )


    llm_validate_custom_system_prompt: bool = Field(
        default=True,
        description="Enable/disable LLM-based validation for custom system prompts"
    )

    @field_validator('score_threshold')
    @classmethod
    def validate_score_threshold(cls, v):
        """Validate score threshold with warning fallback."""
        if not (isinstance(v, float) and 0 < v < 1):
            logger.warning(f"Setting score threshold to default '0.4' as it is missing or malformed in the settings")
            return 0.4
        return v

    @field_validator('num_chunks_post_search')
    @classmethod
    def validate_num_chunks_post_search(cls, v):
        """Validate num chunks post search with warning fallback."""
        if not (isinstance(v, int) and 5 < v <= 15):
            logger.warning(f"Setting num_chunks_post_search to default '10' as it is missing or malformed in the settings")
            return 10
        return v

    @field_validator('num_chunks_post_reranker')
    @classmethod
    def validate_num_chunks_post_reranker(cls, v):
        """Validate num chunks post reranker with warning fallback."""
        if not (isinstance(v, int) and 1 < v <= 5):
            logger.warning(f"Setting num_chunks_post_reranker to default '3' as it is missing or malformed in the settings")
            return 3
        return v

    @field_validator('prompt_template_token_count')
    @classmethod
    def validate_prompt_template_token_count(cls, v):
        """Validate prompt_template_token_count with warning fallback."""
        if not isinstance(v, int):
            logger.warning(f"Setting prompt_template_token_count to default '250' as it is missing in the settings")
            return 250
        return v

class Settings(BaseSettings):
    common: CommonSettings = Field(default_factory=CommonSettings)
    chatbot: RAGConfig = Field(default_factory=RAGConfig)
    llm: LLMConfig = Field(default_factory=LLMConfig)
    query_rephrasing: QueryRephrasingConfig = Field(default_factory=QueryRephrasingConfig)

# Global settings instance
settings = Settings()

# Made with Bob
