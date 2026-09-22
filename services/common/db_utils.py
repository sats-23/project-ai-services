from common.vector_db import VectorStore, VectorStoreNotReadyError
from common.settings import settings


def get_vector_store() -> VectorStore:
    """
    Factory method to initialize the configured Vector Store.
    Controlled by the vector_store_type setting.
    """
    v_store_type = settings.vector_store.vector_store_type.upper()

    if v_store_type == "OPENSEARCH":
        from common.opensearch import OpensearchVectorStore
        return OpensearchVectorStore()
    else:
        raise VectorStoreNotReadyError(f"Unsupported VectorStore type: {v_store_type}")


def get_sql_engine():
    """
    Factory method to create and return a SQLAlchemy Engine for PostgreSQL.
    Reads connection details from POSTGRES_* environment variables via
    common/db/connection.py.
    """
    from common.db.connection import create_db_engine
    return create_db_engine(logger_name="sql_agent")
