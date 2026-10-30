import math
import os
import tempfile
from pathlib import Path
from urllib.parse import urlparse

import pandas as pd
import requests

from src.autobots.core.url_utils import extract_urls

SPREADSHEET_EXTS = (".xlsx", ".xls", ".xlsm", ".xlsb", ".csv")


def _clean_url(u: str) -> str:
    """
    Clean common artifacts from URLs embedded in markdown/text.
    """
    if not u:
        return u
    s = str(u).strip()
    # strip common wrappers
    s = s.strip("()[]`<>")
    # strip trailing punctuation that often sticks to URLs
    s = s.rstrip(".,")
    return s


def _suffix_from_path_or_url(path_or_url: str) -> str:
    """
    Returns a lowercase suffix from a URL or local path (ignores query params).
    """
    if not path_or_url:
        return ""
    s = str(path_or_url)
    try:
        p = urlparse(s).path if "://" in s else s
    except Exception:
        p = s
    p = p.split("?", 1)[0]
    return Path(p).suffix.lower()


def _urls_from_text(text: str) -> list[str]:
    """
    Extract http/https URLs from text (same logic as DataBlock.process_url_in_text).
    """
    if not text:
        return []
    return extract_urls(
        text,
        allowed_schemes={"http", "https"},
        unique=True,
        validate=True,
    )


def pick_spreadsheet_url(urls: list[str], text: str = "") -> str:
    """
    Pick the first URL that looks like a supported spreadsheet/tabular file.
    Supported: .xlsx/.xls/.xlsm/.xlsb/.csv

    If text is provided, URLs are extracted from it (process_url_in_text style) and
    merged with the urls list before picking.

    Raises if none found.
    """
    all_urls: list[str] = list(urls or [])
    if text:
        extracted = _urls_from_text(text)
        for u in extracted:
            if u not in all_urls:
                all_urls.append(u)

    if not all_urls:
        raise ValueError("No URLs provided; expected urls list or text containing spreadsheet URL(s)")

    cleaned = [_clean_url(u) for u in all_urls if u]
    for u in cleaned:
        if _suffix_from_path_or_url(u) in SPREADSHEET_EXTS:
            return u

    raise ValueError(f"No spreadsheet URL found in urls={urls}")


def normalize_european_names(df: pd.DataFrame) -> pd.DataFrame:
    """
    Normalize European names to handle special characters properly.

    Args:
        df (pd.DataFrame): DataFrame with potentially distorted European names

    Returns:
        pd.DataFrame: DataFrame with properly handled European characters
    """
    # Apply string normalization to handle European characters
    for col in df.columns:
        if df[col].dtype == "object":  # Only process string columns
            # Ensure proper string handling for European characters
            df[col] = df[col].astype(str)
            # Remove any potential encoding artifacts
            df[col] = df[col].apply(
                lambda x: x.encode("utf-8", errors="ignore").decode("utf-8")
                if isinstance(x, str)
                else x
            )

    return df


def split_excel_into_batches(
    file_path: str, batch_size: int, header: bool = False, normalize_european_chars: bool = True
):
    """
    Reads an Excel file and splits it into smaller batches.

    Args:
        file_path (str): Path to the Excel file or URL.
        batch_size (int): Number of rows per batch.
        header (bool): Whether to treat first row as header. Defaults to False.
        normalize_european_chars (bool): Whether to normalize European characters. Defaults to True.

    Returns:
        list: A list of DataFrames, each containing a batch of rows.
    """
    try:
        # Handle both local files and URLs
        if file_path.startswith(("http://", "https://")):
            # Download the file from URL
            response = requests.get(file_path, timeout=30)
            response.raise_for_status()  # Raise an exception for bad status codes

            # Preserve extension if possible (csv vs xlsx)
            suffix = _suffix_from_path_or_url(file_path) or ".xlsx"
            with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as temp_file:
                temp_file.write(response.content)
                temp_file_path = temp_file.name
        else:
            # Use local file path
            temp_file_path = file_path
            if not Path(file_path).exists():
                raise FileNotFoundError(f"File not found: {file_path}")

        try:
            # Keep as string to preserve formatting
            common_kwargs = {
                "dtype": str,  # Keep as string to preserve formatting
                "na_filter": False,  # Don't convert empty cells to NaN
                "keep_default_na": False,  # Don't treat certain strings as NaN
            }

            suffix = _suffix_from_path_or_url(temp_file_path)
            if suffix == ".csv":
                # CSV path
                read_kwargs = dict(common_kwargs)
                read_kwargs["header"] = 0 if header else None
                df = pd.read_csv(temp_file_path, **read_kwargs)
            else:
                # Excel path
                read_kwargs = dict(common_kwargs)
                if not header:
                    read_kwargs["header"] = None
                df = pd.read_excel(temp_file_path, **read_kwargs)

        finally:
            # Clean up temporary file if it was downloaded
            if file_path.startswith(("http://", "https://")) and os.path.exists(temp_file_path):
                os.remove(temp_file_path)

        print("Excel file loaded successfully!")
        print("Detected encoding preserved for European characters")

        # Normalize European names and special characters if requested
        if normalize_european_chars:
            df = normalize_european_names(df)
            print("European character normalization applied")

        # Calculate the total number of batches
        total_rows = len(df)
        num_batches = math.ceil(total_rows / batch_size)
        print(f"Total rows: {total_rows}, Batch size: {batch_size}, Total batches: {num_batches}")

        # Split the DataFrame into batches
        batches = []
        for i in range(num_batches):
            start_index = i * batch_size
            end_index = min(start_index + batch_size, total_rows)
            batch = df.iloc[start_index:end_index]
            batches.append(batch)
            print(f"Batch {i + 1} created with rows {start_index} to {end_index - 1}")

        return batches

    except FileNotFoundError:
        print("Error: File not found. Please check the file path.")
        raise
    except Exception as e:
        print(f"An error occurred: {e}")
        raise


# # Example usage
# file_path = "your_excel_file.xlsx"  # Replace with the actual file path or URL
# batch_size = 100  # Number of rows per batch
# header = True  # Set to True if first row contains headers
# normalize_european_chars = True  # Set to True to fix European name encoding issues

# batches = split_excel_into_batches(file_path, batch_size, header, normalize_european_chars)

# # Print the first few rows of each batch (for demonstration)
# for idx, batch in enumerate(batches):
#     print(f"Batch {idx + 1}:\n", batch.head())
#     # Check for European names like: José, María, François, München, etc.