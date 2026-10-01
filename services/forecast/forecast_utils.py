"""
Core training and prediction engine for the Forecast service.

Responsibilities
----------------
- Parse and validate an uploaded CSV against declared column names.
- Encode categorical columns automatically (OrdinalEncoder).
- Train a scikit-learn estimator (RandomForest, Linear, or GradientBoosting).
- Serialise the trained pipeline to disk with joblib.
- Load a persisted pipeline and run batch inference.

No FastAPI, no DB, no job-management — pure ML logic.
"""

import io
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Tuple

import joblib
import pandas as pd
from sklearn.compose import ColumnTransformer
from sklearn.ensemble import GradientBoostingRegressor, RandomForestRegressor
from sklearn.linear_model import LinearRegression
from sklearn.multioutput import MultiOutputRegressor
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import OrdinalEncoder

from common.misc_utils import get_logger
from forecast.models import TrainingAlgorithm
from forecast.settings import settings

logger = get_logger("forecast_utils")


# ── Internal data structures ──────────────────────────────────────────────────

@dataclass
class TrainingResult:
    """Outcome of a completed training run."""
    artefact_path: Path
    input_cols: List[str]
    output_cols: List[str]
    algorithm: str
    n_rows: int
    feature_importances: Dict[str, float] = field(default_factory=dict)


@dataclass
class ForecastArtefact:
    """
    Everything persisted to disk for a trained model.

    Stored as a single joblib file so the load path is one function call.
    """
    pipeline: Pipeline          # sklearn Pipeline (preprocessor + estimator)
    input_cols: List[str]       # feature column names expected at predict time
    output_cols: List[str]      # target column names produced at predict time
    algorithm: str              # algorithm name for informational purposes
    n_rows: int                 # number of training rows


# ── Directory helpers ─────────────────────────────────────────────────────────

def ensure_directories() -> None:
    """Create artefact and staging directories if they do not exist."""
    for d in [settings.forecast.artefact_dir, settings.forecast.staging_dir]:
        d.mkdir(parents=True, exist_ok=True)
        logger.debug("Ensured directory exists: %s", d)


def artefact_path(job_id: str) -> Path:
    """Return the filesystem path for a trained model artefact."""
    return settings.forecast.artefact_dir / f"{job_id}.joblib"


# ── CSV parsing ───────────────────────────────────────────────────────────────

def parse_csv(
    csv_bytes: bytes,
    input_cols: List[str],
    output_cols: List[str],
) -> Tuple[pd.DataFrame, pd.DataFrame]:
    """
    Parse raw CSV bytes and return (X, y) DataFrames.

    Validates that all declared columns are present.  Drops rows that are
    entirely NaN in either feature or target columns.

    Args:
        csv_bytes:   Raw bytes of the uploaded CSV.
        input_cols:  Feature column names.
        output_cols: Target column names.

    Returns:
        Tuple of (X, y) DataFrames.

    Raises:
        ValueError: If required columns are missing or the CSV is empty after
                    cleaning.
    """
    try:
        df = pd.read_csv(io.BytesIO(csv_bytes))
    except Exception as exc:
        raise ValueError(f"Could not parse CSV: {exc}") from exc

    all_cols = input_cols + output_cols
    missing = [c for c in all_cols if c not in df.columns]
    if missing:
        raise ValueError(
            f"The following declared columns are not present in the CSV: {missing}. "
            f"Available columns: {list(df.columns)}"
        )

    df = df[all_cols].dropna(how="all")

    if len(df) == 0:
        raise ValueError("CSV contains no usable rows after dropping fully-empty rows.")

    if len(df) > settings.forecast.max_csv_rows:
        raise ValueError(
            f"CSV contains {len(df):,} rows which exceeds the maximum of "
            f"{settings.forecast.max_csv_rows:,}."
        )

    X = df[input_cols].copy()
    y = df[output_cols].copy()

    return X, y


# ── Pipeline construction ─────────────────────────────────────────────────────

def _build_preprocessor(X: pd.DataFrame) -> ColumnTransformer:
    """
    Build a ColumnTransformer that OrdinalEncodes categorical columns
    and passes numeric columns through unchanged.

    Using OrdinalEncoder (not OneHotEncoder) keeps the feature space small
    and predictable for a POC without domain knowledge about cardinality.
    """
    cat_cols = X.select_dtypes(include=["object", "category"]).columns.tolist()
    num_cols = X.select_dtypes(include="number").columns.tolist()

    transformers = []
    if cat_cols:
        transformers.append(
            (
                "cat",
                OrdinalEncoder(handle_unknown="use_encoded_value", unknown_value=-1),
                cat_cols,
            )
        )
    if num_cols:
        transformers.append(("num", "passthrough", num_cols))

    return ColumnTransformer(transformers=transformers, remainder="drop")


def _build_estimator(
    algorithm: TrainingAlgorithm,
    n_outputs: int,
) -> Any:
    """
    Return a (possibly wrapped) sklearn estimator for the requested algorithm.

    Multi-output targets (n_outputs > 1) are handled via MultiOutputRegressor
    for algorithms that do not natively support it (Linear, GradientBoosting).
    RandomForestRegressor supports multi-output natively.
    """
    if algorithm == TrainingAlgorithm.RANDOM_FOREST:
        return RandomForestRegressor(
            n_estimators=settings.forecast.rf_n_estimators,
            max_depth=settings.forecast.rf_max_depth,
            random_state=settings.forecast.rf_random_state,
            n_jobs=-1,
        )

    if algorithm == TrainingAlgorithm.LINEAR:
        base = LinearRegression()
        return MultiOutputRegressor(base) if n_outputs > 1 else base

    if algorithm == TrainingAlgorithm.GRADIENT_BOOSTING:
        base = GradientBoostingRegressor(
            n_estimators=settings.forecast.gb_n_estimators,
            max_depth=settings.forecast.gb_max_depth,
            random_state=settings.forecast.gb_random_state,
        )
        return MultiOutputRegressor(base) if n_outputs > 1 else base

    raise ValueError(f"Unknown algorithm: {algorithm}")


# ── Feature importances ───────────────────────────────────────────────────────

def _extract_feature_importances(
    pipeline: Pipeline,
    input_cols: List[str],
) -> Dict[str, float]:
    """
    Extract feature importances from the fitted pipeline where available.

    Returns an empty dict for algorithms that do not expose importances
    (e.g. LinearRegression).
    """
    estimator = pipeline.named_steps["estimator"]

    # Unwrap MultiOutputRegressor to reach the first sub-estimator
    if isinstance(estimator, MultiOutputRegressor):
        sub = estimator.estimators_[0] if estimator.estimators_ else None
    else:
        sub = estimator

    if sub is None or not hasattr(sub, "feature_importances_"):
        return {}

    importances = sub.feature_importances_
    # Map back to original column names on a best-effort basis.
    # After ColumnTransformer the order is [cat_cols..., num_cols...]; we rely
    # on that order matching what get_feature_names_out would return.
    names = input_cols[: len(importances)]
    return {name: round(float(imp), 6) for name, imp in zip(names, importances)}


# ── Public API ────────────────────────────────────────────────────────────────

def train(
    job_id: str,
    csv_bytes: bytes,
    input_cols: List[str],
    output_cols: List[str],
    algorithm: TrainingAlgorithm = TrainingAlgorithm.RANDOM_FOREST,
) -> TrainingResult:
    """
    Parse the CSV, fit the pipeline, and persist the artefact to disk.

    This function is intentionally synchronous so it can be called from
    ``asyncio.to_thread`` in the FastAPI layer without blocking the event loop.

    Args:
        job_id:      Unique job identifier — used to name the artefact file.
        csv_bytes:   Raw bytes from the uploaded CSV.
        input_cols:  Feature column names.
        output_cols: Target column names.
        algorithm:   Which sklearn estimator to use.

    Returns:
        TrainingResult with the artefact path and training metadata.

    Raises:
        ValueError: On invalid CSV, missing columns, or empty data.
        IOError:    If the artefact cannot be written to disk.
    """
    ensure_directories()

    logger.info(
        "Training job %s | algorithm=%s | inputs=%s | outputs=%s",
        job_id,
        algorithm.value,
        input_cols,
        output_cols,
    )

    X, y = parse_csv(csv_bytes, input_cols, output_cols)
    n_rows = len(X)
    logger.info("Job %s | parsed %d rows", job_id, n_rows)

    preprocessor = _build_preprocessor(X)
    estimator = _build_estimator(algorithm, n_outputs=len(output_cols))

    pipeline = Pipeline(
        steps=[
            ("preprocessor", preprocessor),
            ("estimator", estimator),
        ]
    )

    # Flatten y to a 1-D array when there is only a single output column so that
    # estimators that do not accept 2-D targets work without MultiOutputRegressor.
    y_fit = y.values.ravel() if len(output_cols) == 1 else y.values
    pipeline.fit(X, y_fit)
    logger.info("Job %s | training complete", job_id)

    importances = _extract_feature_importances(pipeline, input_cols)

    artefact = ForecastArtefact(
        pipeline=pipeline,
        input_cols=input_cols,
        output_cols=output_cols,
        algorithm=algorithm.value,
        n_rows=n_rows,
    )

    dest = artefact_path(job_id)
    try:
        joblib.dump(artefact, dest)
        logger.info("Job %s | artefact saved to %s", job_id, dest)
    except Exception as exc:
        raise IOError(f"Failed to persist model artefact for job {job_id}: {exc}") from exc

    return TrainingResult(
        artefact_path=dest,
        input_cols=input_cols,
        output_cols=output_cols,
        algorithm=algorithm.value,
        n_rows=n_rows,
        feature_importances=importances,
    )


def predict(
    job_id: str,
    inputs: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """
    Load the persisted model for ``job_id`` and run batch prediction.

    Args:
        job_id: ID of the completed training job.
        inputs: List of row dicts whose keys match the training input_cols.

    Returns:
        List of dicts, one per input row, mapping output column name → predicted value.

    Raises:
        FileNotFoundError: If no artefact exists for the given job_id.
        ValueError:        If input rows are missing required columns or exceed the
                           row limit.
    """
    path = artefact_path(job_id)
    if not path.exists():
        raise FileNotFoundError(
            f"No trained model found for job '{job_id}'. "
            "Ensure training has completed successfully before calling predict."
        )

    artefact: ForecastArtefact = joblib.load(path)

    if len(inputs) > settings.forecast.max_predict_rows:
        raise ValueError(
            f"Prediction request contains {len(inputs):,} rows which exceeds "
            f"the maximum of {settings.forecast.max_predict_rows:,}."
        )

    X_pred = pd.DataFrame(inputs)

    missing = [c for c in artefact.input_cols if c not in X_pred.columns]
    if missing:
        raise ValueError(
            f"Prediction input is missing columns that were used during training: {missing}."
        )

    # Reorder to match training column order; extra columns are silently ignored.
    X_pred = X_pred[artefact.input_cols]

    raw = artefact.pipeline.predict(X_pred)

    # raw shape: (n_rows,) for single-output or (n_rows, n_outputs) for multi-output
    if raw.ndim == 1:
        raw = raw.reshape(-1, 1)

    results = []
    for row_values in raw:
        results.append(
            {col: float(val) for col, val in zip(artefact.output_cols, row_values)}
        )

    logger.info(
        "Job %s | predicted %d rows → %d output columns",
        job_id,
        len(inputs),
        len(artefact.output_cols),
    )
    return results


def delete_artefact(job_id: str) -> bool:
    """
    Remove the serialised model artefact for a job.

    Returns True if the file was deleted, False if it did not exist.
    """
    path = artefact_path(job_id)
    if path.exists():
        path.unlink()
        logger.info("Deleted artefact for job %s", job_id)
        return True
    return False
