from azure.ai.inference.aio import ChatCompletionsClient
from azure.core.credentials import AzureKeyCredential
from fastapi import Depends
from motor.motor_asyncio import AsyncIOMotorDatabase
from openai import AsyncAzureOpenAI

from src.autobots import SettingsProvider
from src.autobots.conn.openai.openai_common_models import OPENAI_ASST_MODELS, OPENAI_MODELS
from src.autobots.core.database.mongo_base import get_mongo_db
from src.autobots.core.settings import Settings
from src.autobots.user.user_orm_model import UserORM


class AzureOpenAIModelClientFactory:
    @staticmethod
    async def create_client(
        model: OPENAI_MODELS | str,
        settings: Settings = SettingsProvider.sget(),
        user: UserORM | None = None,
        db: AsyncIOMotorDatabase = Depends(get_mongo_db),
    ) -> AsyncAzureOpenAI | ChatCompletionsClient:
        timeout = 900
        client: AsyncAzureOpenAI | ChatCompletionsClient | None = None
        cls = AzureOpenAIModelClientFactory
        match model:
            case "model-router":
                client = AsyncAzureOpenAI(
                    api_version=settings.AZURE_OPENAI_API_VERSION_EASTUS2_MODEL_ROUTER,
                    azure_endpoint=settings.AZURE_OPENAI_BASE_URL_EASTUS2,
                    api_key=settings.AZURE_OPENAI_API_KEY_EASTUS2,
                    default_headers={"Connection": "keep-alive"},
                )
            case "gpt-4.1":
                client = AsyncAzureOpenAI(
                    api_version=settings.AZURE_OPENAI_API_VERSION_EASTUS2_GPT_4_1,
                    azure_endpoint=settings.AZURE_OPENAI_BASE_URL_EASTUS2,
                    api_key=settings.AZURE_OPENAI_API_KEY_EASTUS2,
                    default_headers={"Connection": "keep-alive"},
                )
            case "gpt-4.1-mini":
                client = AsyncAzureOpenAI(
                    api_version=settings.AZURE_OPENAI_API_VERSION_EASTUS2_GPT_4_1_MINI,
                    azure_endpoint=settings.AZURE_OPENAI_BASE_URL_EASTUS2,
                    api_key=settings.AZURE_OPENAI_API_KEY_EASTUS2,
                    default_headers={"Connection": "keep-alive"},
                )
            case "gpt-4.1-nano":
                client = AsyncAzureOpenAI(
                    api_version=settings.AZURE_OPENAI_API_VERSION_EASTUS2_GPT_4_1_NANO,
                    azure_endpoint=settings.AZURE_OPENAI_BASE_URL_EASTUS2,
                    api_key=settings.AZURE_OPENAI_API_KEY_EASTUS2,
                    default_headers={"Connection": "keep-alive"},
                )
            case "gpt-4o":
                client = cls._create_gpt_4o_model_client(
                    user=user,
                )
            case "gpt-4o-1120":
                client = cls._create_gpt_4o_1120_model_client(
                    user=user,
                )
            case "gpt-4o-0806":
                client = cls._create_gpt_4o_0806_model_client(
                    user=user,
                )
            case "gpt-4o-0513":
                client = cls._create_gpt_4o_0513_model_client(
                    user=user,
                )
            case "gpt-4o-mini":
                client = cls._create_gpt_4o_mini_model_client(
                    user=user,
                )
            case "o1":
                client = cls._create_o1_model_client(
                    user=user,
                )
            case "o1-mini":
                client = cls._create_o1_mini_model_client(
                    user=user,
                )
            case "o3":
                client = AsyncAzureOpenAI(
                    api_version=settings.AZURE_OPENAI_API_VERSION_EASTUS2_O3,
                    azure_endpoint=settings.AZURE_OPENAI_BASE_URL_EASTUS2,
                    api_key=settings.AZURE_OPENAI_API_KEY_EASTUS2,
                )
            case "o3-pro":
                client = AsyncAzureOpenAI(
                    api_version=settings.AZURE_OPENAI_API_VERSION_EASTUS2_O3_PRO,
                    azure_endpoint=settings.AZURE_OPENAI_BASE_URL_EASTUS2,
                    api_key=settings.AZURE_OPENAI_API_KEY_EASTUS2,
                )
            case "o3-mini":
                client = cls._create_o3_mini_model_client(
                    user=user,
                )
            case "o4-mini":
                client = AsyncAzureOpenAI(
                    api_version=settings.AZURE_OPENAI_API_VERSION_EASTUS2_O4_MINI,
                    azure_endpoint=settings.AZURE_OPENAI_BASE_URL_EASTUS2,
                    api_key=settings.AZURE_OPENAI_API_KEY_EASTUS2,
                )
            case "gpt-4-32k":
                client = cls._create_gpt_4_32k_model_client(
                    user=user,
                )
            case "computer-use-preview":
                client = AsyncAzureOpenAI(
                    api_version=settings.AZURE_OPENAI_API_VERSION_EASTUS2_COMPUTER_USE_PREVIEW,
                    azure_endpoint=settings.AZURE_OPENAI_BASE_URL_EASTUS2,
                    api_key=settings.AZURE_OPENAI_API_KEY_EASTUS2,
                )
            case "deepseek-r1":
                client = await cls._create_deepseek_r1_model_client(
                    user=user,
                )
            case _:
                raise ValueError(f"Invalid model: {model}")
        assert client is not None
        client.timeout = timeout
        return client

    @staticmethod
    def _create_gpt_4o_model_client(
        user: UserORM | None = None,
        db: AsyncIOMotorDatabase = Depends(get_mongo_db),
        settings: Settings = SettingsProvider.sget(),
    ) -> AsyncAzureOpenAI:
        return AsyncAzureOpenAI(
            api_key=settings.AZURE_OPENAI_GPT_FOUR_O_API_KEY,
            api_version=settings.AZURE_OPENAI_GPT_FOUR_O_API_VERSION,
            azure_endpoint=settings.AZURE_OPENAI_GPT_FOUR_O_BASE_URL,
            default_headers={"Connection": "keep-alive"},  # Add connection header
            timeout=900,
        )

    @staticmethod
    def _create_gpt_4o_mini_model_client(
        user: UserORM | None = None,
        db: AsyncIOMotorDatabase = Depends(get_mongo_db),
        settings: Settings = SettingsProvider.sget(),
    ) -> AsyncAzureOpenAI:
        return AsyncAzureOpenAI(
            api_key=settings.AZURE_OPENAI_GPT_FOUR_O_MINI_API_KEY,
            api_version=settings.AZURE_OPENAI_GPT_FOUR_O_MINI_API_VERSION,
            azure_endpoint=settings.AZURE_OPENAI_GPT_FOUR_O_MINI_BASE_URL,
            default_headers={"Connection": "keep-alive"},  # Add connection header
            timeout=900,
        )

    @staticmethod
    def _create_o1_model_client(
        user: UserORM | None = None,
        db: AsyncIOMotorDatabase = Depends(get_mongo_db),
        settings: Settings = SettingsProvider.sget(),
    ) -> AsyncAzureOpenAI:
        return AsyncAzureOpenAI(
            api_key=settings.AZURE_OPENAI_O_ONE_API_KEY,
            api_version=settings.AZURE_OPENAI_O_ONE_API_VERSION,
            azure_endpoint=settings.AZURE_OPENAI_O_ONE_BASE_URL,
            default_headers={"Connection": "keep-alive"},  # Add connection header
            timeout=900,
        )

    @staticmethod
    def _create_o1_mini_model_client(
        user: UserORM | None = None,
        db: AsyncIOMotorDatabase = Depends(get_mongo_db),
        settings: Settings = SettingsProvider.sget(),
    ) -> AsyncAzureOpenAI:
        return AsyncAzureOpenAI(
            api_key=settings.AZURE_OPENAI_O_ONE_MINI_API_KEY,
            api_version=settings.AZURE_OPENAI_O_ONE_MINI_API_VERSION,
            azure_endpoint=settings.AZURE_OPENAI_O_ONE_MINI_BASE_URL,
            default_headers={"Connection": "keep-alive"},  # Add connection header
            timeout=900,
        )

    @staticmethod
    def _create_o3_mini_model_client(
        user: UserORM | None = None,
        db: AsyncIOMotorDatabase = Depends(get_mongo_db),
        settings: Settings = SettingsProvider.sget(),
    ) -> AsyncAzureOpenAI:
        return AsyncAzureOpenAI(
            api_key=settings.AZURE_OPENAI_O_THREE_MINI_API_KEY,
            api_version=settings.AZURE_OPENAI_O_THREE_MINI_API_VERSION,
            azure_endpoint=settings.AZURE_OPENAI_O_THREE_MINI_BASE_URL,
            default_headers={"Connection": "keep-alive"},  # Add connection header
            timeout=900,
        )

    @staticmethod
    def _create_gpt_4_32k_model_client(
        user: UserORM | None = None,
        db: AsyncIOMotorDatabase = Depends(get_mongo_db),
        settings: Settings = SettingsProvider.sget(),
    ) -> AsyncAzureOpenAI:
        return AsyncAzureOpenAI(
            api_key=settings.AZURE_OPENAI_GPT_4_32K_API_KEY,
            api_version=settings.AZURE_OPENAI_GPT_4_32K_API_VERSION,
            azure_endpoint=settings.AZURE_OPENAI_GPT_4_32K_BASE_URL,
            default_headers={"Connection": "keep-alive"},  # Add connection header
            timeout=900,
        )

    @staticmethod
    def create_asst_client(
        model: OPENAI_ASST_MODELS | None = None,
        user: UserORM | None = None,
        db: AsyncIOMotorDatabase = Depends(get_mongo_db),
        settings: Settings = SettingsProvider.sget(),
    ) -> AsyncAzureOpenAI:
        return AsyncAzureOpenAI(
            api_key=settings.AZURE_OPENAI_ASST_API_KEY,
            api_version=settings.AZURE_OPENAI_ASST_API_VERSION,
            azure_endpoint=settings.AZURE_OPENAI_ASST_BASE_URL,
            default_headers={"Connection": "keep-alive"},  # Add connection header
            timeout=900,
        )

    @staticmethod
    def _create_gpt_4o_1120_model_client(
        user: UserORM | None = None,
        db: AsyncIOMotorDatabase = Depends(get_mongo_db),
        settings: Settings = SettingsProvider.sget(),
    ):
        client = AzureOpenAIModelClientFactory._create_gpt_4o_model_client(
            user=user,
            db=db,
            settings=settings,
        )
        return client

    @staticmethod
    def _create_gpt_4o_0806_model_client(
        user: UserORM | None = None,
        db: AsyncIOMotorDatabase = Depends(get_mongo_db),
        settings: Settings = SettingsProvider.sget(),
    ):
        return AsyncAzureOpenAI(
            api_key=settings.AZURE_OPENAI_ASST_API_KEY,
            api_version=settings.AZURE_OPENAI_ASST_API_VERSION,
            azure_endpoint=settings.AZURE_OPENAI_ASST_BASE_URL,
            default_headers={"Connection": "keep-alive"},  # Add connection header
            timeout=900,
        )

    @staticmethod
    def _create_gpt_4o_0513_model_client(
        user: UserORM | None = None,
        db: AsyncIOMotorDatabase = Depends(get_mongo_db),
        settings: Settings = SettingsProvider.sget(),
    ):
        return AsyncAzureOpenAI(
            api_key=settings.AZURE_OPENAI_NCUS_GPT_FOUR_O_API_KEY,
            api_version=settings.AZURE_OPENAI_NCUS_GPT_FOUR_O_API_VERSION,
            azure_endpoint=settings.AZURE_OPENAI_NCUS_GPT_FOUR_O_BASE_URL,
            default_headers={"Connection": "keep-alive"},  # Add connection header
            timeout=900,
        )

    @staticmethod
    async def _create_deepseek_r1_model_client(
        user: UserORM | None = None,
        db: AsyncIOMotorDatabase = Depends(get_mongo_db),
        settings: Settings = SettingsProvider.sget(),
    ):
        client = ChatCompletionsClient(
            endpoint=settings.AZURE_DEEPSEEK_R1_BASE_URL,
            credential=AzureKeyCredential(
                settings.AZURE_DEEPSEEK_R1_API_KEY,
            ),
        )
        # model_info = await client.get_model_info()
        # logger.info(f"Model name: {model_info.model_name}")
        # logger.info(f"Model type: {model_info.model_type}")
        # logger.info(f"Model provider name: {model_info.model_provider_name}")
        return client
