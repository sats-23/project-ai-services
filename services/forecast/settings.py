"""
Configuration settings for the Forecast service.
All values can be overridden via environment variables.

Environment variable prefix: FORECAST_
  e.g. FORECAST_ARTEFACT_DIR, FORECAST_MAX_CSV_ROWS
"""

from pathlib import Path

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from common.misc_utils import get_logger

logger = get_logger("settings")


class ForecastConfig(BaseSettings):
    """Forecast-specific settings. All env vars use the FORECAST_ prefix."""

    model_config = SettingsConfigDict(env_prefix="FORECAST_")

    # Where trained model artefacts are persisted (joblib files)
    artefact_dir: Path = Field(
        default=Path("/var/cache/forecast/models"),
        description="Directory where serialised model artefacts are stored.",
    )

    # Where uploaded CSV files are staged before training
    staging_dir: Path = Field(
        default=Path("/var/cache/forecast/staging"),
        description="Directory where uploaded CSV files are staged during training.",
    )

    # Hard cap on the number of rows accepted in a training CSV
    max_csv_rows: int = Field(
        default=500_000,
        gt=0,
        description="Maximum number of data rows accepted in a training CSV.",
    )

    # Cap on the number of input rows accepted in a single predict call
    max_predict_rows: int = Field(
        default=10_000,
        gt=0,
        description="Maximum number of input rows accepted in a single prediction request.",
    )

    # scikit-learn RandomForest hyper-parameters (exposed so they can be tuned via env)
    rf_n_estimators: int = Field(
        default=100,
        gt=0,
        description="Number of trees in the RandomForest.",
    )
    rf_max_depth: int = Field(
        default=10,
        gt=0,
        description="Maximum depth of each tree in the RandomForest.",
    )
    rf_random_state: int = Field(
        default=42,
        description="Random seed for reproducibility.",
    )

    # GradientBoosting hyper-parameters
    gb_n_estimators: int = Field(
        default=100,
        gt=0,
        description="Number of boosting stages.",
    )
    gb_max_depth: int = Field(
        default=4,
        gt=0,
        description="Maximum depth of each regression estimator.",
    )
    gb_random_state: int = Field(
        default=42,
        description="Random seed for reproducibility.",
    )

    @field_validator("max_csv_rows", "max_predict_rows")
    @classmethod
    def must_be_positive(cls, v: int) -> int:
        if v <= 0:
            raise ValueError("Value must be a positive integer.")
        return v


class Settings(BaseSettings):
    forecast: ForecastConfig = Field(default_factory=ForecastConfig)


# Global settings instance
settings = Settings()
