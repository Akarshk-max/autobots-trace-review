from functools import lru_cache
from typing import Literal

import openai
from httpx import Timeout
from openai import AsyncAzureOpenAI, AsyncOpenAI
from pydantic import BaseModel, ConfigDict

from src.autobots.conn.openai.azure_openai_client_factory import AzureOpenAIModelClientFactory
from src.autobots.conn.openai.openai_assistants.openai_assistants import OpenaiAssistants
from src.autobots.conn.openai.openai_audio.openai_audio import OpenaiAudio
from src.autobots.conn.openai.openai_chat.openai_chat import OpenaiChat
from src.autobots.conn.openai.openai_chat.openai_chat_stream import OpenaiChatStream
from src.autobots.conn.openai.openai_embeddings.openai_embeddings import OpenaiEmbeddings
from src.autobots.conn.openai.openai_files.openai_files import OpenaiFiles
from src.autobots.conn.openai.openai_images.openai_images import OpenaiImages
from src.autobots.conn.openai.openai_responses.openai_responses import OpenaiResponses
from src.autobots.conn.openai.openai_responses.openai_responses_stream import OpenaiResponsesStream
from src.autobots.conn.openai.openai_vector_stores.openai_vector_store_file_batches.openai_vector_store_file_batches import (
    OpenaiVectorStoreFileBatches,
)
from src.autobots.conn.openai.openai_vector_stores.openai_vector_store_files.openai_vector_store_files import (
    OpenaiVectorStoreFiles,
)
from src.autobots.conn.openai.openai_vector_stores.openai_vector_stores.openai_vector_stores import (
    OpenaiVectorStores,
)
from src.autobots.core.settings import Settings, SettingsProvider
from src.autobots.data_model.context import Context


class AzureOpenAIConfig(BaseModel):
    ## azure
    tts_base_url: str
    tts_api_key: str
    tts_api_version: str

    whisper_base_url: str
    whisper_api_key: str
    whisper_api_version: str

    embeddings_base_url: str
    embeddings_api_key: str
    embeddings_api_version: str

    dalle_base_url: str
    dalle_api_key: str
    dalle_api_version: str

    openai_api_key: str
    openai_organization: str | None = None
    openai_timeout: float | Timeout = 900

    # asst_base_url: str
    # asst_api_key: str
    # asst_api_version: str
    # asst_timeout: float | Timeout = 120.0

    model_config = ConfigDict(arbitrary_types_allowed=True)


class OpenAIOpenAIConfig(BaseModel):
    # openai (legacy)
    api_key: str
    organization: str | None = None
    timeout: float | Timeout = 900

    model_config = ConfigDict(arbitrary_types_allowed=True)


class OpenAI:
    def __init__(
        self,
        *,
        api_type: Literal["openai", "azure"],
        config: AzureOpenAIConfig | OpenAIOpenAIConfig,
    ):
        self.provider = api_type
        if api_type == "azure":
            openai.api_type = api_type  # Global Openai settings
            tts_client = AsyncAzureOpenAI(
                azure_endpoint=config.tts_base_url,
                api_version=config.tts_api_version,
                api_key=config.tts_api_key,
            )  # Models are tts, tts-1-hd
            whisper_client = AsyncAzureOpenAI(
                azure_endpoint=config.whisper_base_url,
                api_version=config.whisper_api_version,
                api_key=config.whisper_api_key,
            )  # Models are whisper-1
            embeddings_client = AsyncAzureOpenAI(
                azure_endpoint=config.embeddings_base_url,
                api_version=config.embeddings_api_version,
                api_key=config.embeddings_api_key,
            )
            dalle_client = AsyncAzureOpenAI(
                azure_endpoint=config.dalle_base_url,
                api_version=config.dalle_api_version,
                api_key=config.dalle_api_key,
            )
            # asst_client = AsyncAzureOpenAI(
            #     azure_endpoint=config.asst_base_url, api_version=config.asst_api_version,
            #     api_key=config.asst_api_key
            # )
            asst_client = AzureOpenAIModelClientFactory.create_asst_client()
            # openai_client = AsyncOpenAI(
            #     api_key=config.openai_api_key, organization=config.openai_organization,
            #     timeout=config.openai_timeout
            # )
            self.openai_audio = OpenaiAudio(
                openai_tts_client=tts_client, openai_sst_client=whisper_client
            )
            self.openai_chat = OpenaiChat(
                provider=api_type, client_factory=AzureOpenAIModelClientFactory
            )
            self.openai_chat_stream = OpenaiChatStream(
                provider=api_type, client_factory=AzureOpenAIModelClientFactory
            )
            self.openai_embeddings = OpenaiEmbeddings(client=embeddings_client)
            self.openai_images = OpenaiImages(dalle_client)
            self.openai_assistants = OpenaiAssistants(asst_client)
            self.openai_files = OpenaiFiles(asst_client)
            # self.openai_responses = OpenaiResponses(asst_client)
            self.openai_responses = OpenaiResponses(
                provider=api_type, client_factory=AzureOpenAIModelClientFactory
            )
            self.openai_responses_stream = OpenaiResponsesStream(
                provider=api_type, client_factory=AzureOpenAIModelClientFactory
            )
            self.openai_responses_stream = OpenaiResponsesStream(
                provider=api_type, client_factory=AzureOpenAIModelClientFactory
            )
            self.openai_vector_stores = OpenaiVectorStores(asst_client)
            self.openai_vector_store_files = OpenaiVectorStoreFiles(asst_client)
            self.openai_vector_store_file_batches = OpenaiVectorStoreFileBatches(asst_client)
        elif api_type == "openai":  # openai
            if config.organization is None:
                config.organization = ""
            self.client = AsyncOpenAI(
                api_key=config.api_key, organization=config.organization, timeout=config.timeout
            )
            self.openai_audio = OpenaiAudio(self.client, self.client)
            self.openai_chat = OpenaiChat(
                provider=api_type,
                openai_client=self.client,
                client_factory=AzureOpenAIModelClientFactory,
            )
            self.openai_chat_stream = OpenaiChatStream(
                provider=api_type,
                openai_client=self.client,
                client_factory=AzureOpenAIModelClientFactory,
            )
            self.openai_embeddings = OpenaiEmbeddings(self.client)
            self.openai_images = OpenaiImages(self.client)
            self.openai_assistants = OpenaiAssistants(self.client)
            self.openai_files = OpenaiFiles(self.client)
            self.openai_responses = OpenaiResponses(provider=api_type, openai_client=self.client)
            self.openai_responses_stream = OpenaiResponsesStream(
                provider=api_type, openai_client=self.client
            )
            self.openai_vector_stores = OpenaiVectorStores(self.client)
            self.openai_vector_store_files = OpenaiVectorStoreFiles(self.client)
            self.openai_vector_store_file_batches = OpenaiVectorStoreFileBatches(self.client)
        else:
            raise ValueError("Invalid OpenAI API Type")


# @lru_cache
def get_azure_openai(settings: Settings) -> OpenAI:
    config = AzureOpenAIConfig(
        # tts
        tts_base_url=settings.AZURE_OPENAI_TTS_BASE_URL,
        tts_api_key=settings.AZURE_OPENAI_TTS_API_KEY,
        tts_api_version=settings.AZURE_OPENAI_TTS_API_VERSION,
        # whisper
        whisper_base_url=settings.AZURE_OPENAI_WHISPER_BASE_URL,
        whisper_api_key=settings.AZURE_OPENAI_WHISPER_API_KEY,
        whisper_api_version=settings.AZURE_OPENAI_WHISPER_API_VERSION,
        # embeddings
        embeddings_base_url=settings.AZURE_OPENAI_EMBEDDINGS_BASE_URL,
        embeddings_api_key=settings.AZURE_OPENAI_EMBEDDINGS_API_KEY,
        embeddings_api_version=settings.AZURE_OPENAI_EMBEDDINGS_API_VERSION,
        # dalle
        dalle_base_url=settings.AZURE_OPENAI_DALLE_BASE_URL,
        dalle_api_key=settings.AZURE_OPENAI_DALLE_API_KEY,
        dalle_api_version=settings.AZURE_OPENAI_DALLE_API_VERSION,
        # openai
        openai_api_key=settings.OPENAI_API_KEY,
        openai_organization=settings.OPENAI_ORG_ID,
        openai_timeout=settings.OPENAI_TIMEOUT,
    )
    return OpenAI(
        api_type="azure",
        config=config,
    )


# @lru_cache
def get_openai_openai(settings: Settings) -> OpenAI:
    config = OpenAIOpenAIConfig(
        api_key=settings.OPENAI_API_KEY,
        organization=settings.OPENAI_ORG_ID,
        timeout=settings.OPENAI_TIMEOUT,
    )
    return OpenAI(
        api_type="openai",
        config=config,
    )


@lru_cache
def get_openai(
    provider: Literal["azure", "openai"], settings: Settings = SettingsProvider.sget()
) -> OpenAI:
    if provider == "azure":
        return get_azure_openai(settings=settings)
    elif provider == "openai":
        return get_openai_openai(settings=settings)


# @lru_cache
# def get_old_openai(settings: Settings = SettingsProvider.sget()) -> OpenAI:
#     if settings.OPENAI_TYPE_IS_AZURE:
#         return get_azure_openai(settings=settings)
#     else:
#         return get_openai_openai(settings=settings)


def get_conn_openai(
    ctx: Context,
    api_key: str,
    org_id: str | None = None,
    settings: Settings = SettingsProvider.sget(),
) -> OpenAI:
    return OpenAI(
        api_type="openai",
        config=OpenAIOpenAIConfig(
            api_key=api_key,
            organization=org_id,
            timeout=settings.OPENAI_TIMEOUT,
        ),
    )


class AzureOpenAIBYOK:
    """Lightweight Azure OpenAI wrapper for user-supplied endpoints (chat + responses only)."""

    def __init__(
        self,
        api_key: str,
        endpoint: str,
        api_version: str,
        settings: Settings = SettingsProvider.sget(),
    ):
        azure_client = AsyncAzureOpenAI(
            api_key=api_key,
            azure_endpoint=endpoint,
            api_version=api_version,
            timeout=settings.OPENAI_TIMEOUT,
        )
        self.provider = "azure_byok"
        self.client = azure_client
        self.openai_chat = OpenaiChat(provider="azure_byok", openai_client=azure_client)
        self.openai_files = OpenaiFiles(azure_client)
        self.openai_responses = OpenaiResponses(provider="azure_byok", openai_client=azure_client)


def get_conn_azure_openai_byok(
    api_key: str,
    endpoint: str,
    api_version: str,
    settings: Settings = SettingsProvider.sget(),
) -> AzureOpenAIBYOK:
    return AzureOpenAIBYOK(
        api_key=api_key, endpoint=endpoint, api_version=api_version, settings=settings
    )
