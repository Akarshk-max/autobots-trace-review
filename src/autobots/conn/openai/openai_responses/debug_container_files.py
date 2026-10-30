"""
Debug utility to list files in an OpenAI container using sync client.
"""
from openai import OpenAI

from src.autobots.core.settings import SettingsProvider


def get_container_files_sync(container_id: str):
    """
    List files in a container using sync OpenAI client.
    Returns the response as a serializable dict.
    """
    settings = SettingsProvider.sget()
    client = OpenAI(api_key=settings.OPENAI_API_KEY)
    
    response = client.containers.files.list(container_id=container_id)
    return response.model_dump()  # Convert entire response to dict
