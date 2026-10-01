"""
Pydantic models for the Forecast service.

Covers training requests/responses and prediction requests/responses.
"""

from enum import Enum
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field, field_validator


class TrainingAlgorithm(str, Enum):
    """Supported ML algorithms for training."""
    RANDOM_FOREST = "random_forest"
    LINEAR = "linear"
    GRADIENT_BOOSTING = "gradient_boosting"


class JobStatus(str, Enum):
    ACCEPTED = "accepted"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    FAILED = "failed"


# ── Training ──────────────────────────────────────────────────────────────────

class TrainRequest(BaseModel):
    """
    Metadata submitted alongside the CSV file upload.

    The CSV file itself is received as a FastAPI UploadFile; this model
    carries the column configuration and optional algorithm choice.
    """

    input_cols: List[str] = Field(
        ...,
        min_length=1,
        description="Column names in the CSV to use as model features (X).",
        examples=[["date", "region", "spend"]],
    )
    output_cols: List[str] = Field(
        ...,
        min_length=1,
        description="Column names in the CSV to predict (y). Multi-output is supported.",
        examples=[["revenue", "demand"]],
    )
    algorithm: TrainingAlgorithm = Field(
        default=TrainingAlgorithm.RANDOM_FOREST,
        description="ML algorithm to use for training.",
    )

    @field_validator("input_cols", "output_cols")
    @classmethod
    def no_empty_strings(cls, v: List[str]) -> List[str]:
        if any(not c.strip() for c in v):
            raise ValueError("Column names must not be empty strings.")
        return [c.strip() for c in v]


class TrainJobCreatedResponse(BaseModel):
    """Returned immediately when a training job is accepted."""
    job_id: str
    status: JobStatus = JobStatus.ACCEPTED


class TrainJobStatusResponse(BaseModel):
    """Returned when polling a training job for its current status."""
    job_id: str
    status: JobStatus
    filename: Optional[str] = None
    input_cols: Optional[List[str]] = None
    output_cols: Optional[List[str]] = None
    algorithm: Optional[str] = None
    trained_at: Optional[str] = None
    error: Optional[str] = None


# ── Prediction ────────────────────────────────────────────────────────────────

class PredictRequest(BaseModel):
    """
    Request body for /v1/forecast/predict.

    `inputs` is a list of row dicts whose keys must match the input_cols
    declared during training.
    """

    job_id: str = Field(..., description="ID of the completed training job.")
    inputs: List[Dict[str, Any]] = Field(
        ...,
        min_length=1,
        description="List of input rows to predict. Each dict maps input column name → value.",
        examples=[[{"date": "2025-10", "region": "US", "spend": 5000}]],
    )


class PredictResponse(BaseModel):
    """Prediction results, one entry per input row."""
    job_id: str
    predictions: List[Dict[str, Any]] = Field(
        description="List of prediction dicts, one per input row. Keys are the output column names."
    )
