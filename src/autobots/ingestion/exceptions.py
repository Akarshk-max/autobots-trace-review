# src/autobots/ingestion/exceptions.py


class RetryableIngestionError(Exception):
    """Raised when the worker should retry the whole ingestion job."""

    pass
