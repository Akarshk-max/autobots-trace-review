import base64
from functools import lru_cache
from typing import Dict, List, Literal, Optional

from jose.constants import ALGORITHMS
from loguru import logger
from pydantic import Field, HttpUrl, computed_field, conint
from pydantic_settings import BaseSettings

# from autobots.core.config import get_config


# def get_env_suffix(ENV):
#     return f"_{ENV}" if ENV != "prod" else ""


class Settings(BaseSettings):
    ENV: Literal["local", "test", "dev", "qa", "stage", "prod"] | str = "local"

    # value from github deployment action (CICD)
    VERSION: str = "local"

    COOKIE_DOMAIN: str = "localhost"
    ALLOW_ORIGINS: str = "http://localhost:3000"
    SYSTEM_EMAIL_DOMAIN: str = "runkai.ai"

    JWT_SECRET_KEY: str
    JWT_PUBLIC_KEY_BASE64_ENCODED: str = ""
    JWT_ALGORITHM: str = ALGORITHMS.ES256

    @computed_field
    @property
    def JWT_PUBLIC_KEY(self) -> str:
        if self.JWT_PUBLIC_KEY_BASE64_ENCODED:
            return base64.b64decode(self.JWT_PUBLIC_KEY_BASE64_ENCODED).decode("utf-8")
        return ""

    SUPABASE_URL: str
    SUPABASE_ANON_KEY: str
    SUPABASE_SERVICE_ROLE_KEY: str = ""
    SYSTEM_USER: str

    SQLALCHEMY_DATABASE_URL: str
    SQLALCHEMY_DATABASE_SCHEMA: str

    MONGO_CONN: str
    MONGO_DATABASE: str

    # Deployment-level object storage. Existing deployments default to S3.
    OBJECT_STORAGE_PROVIDER: Literal["s3", "azure"] = "s3"
    OBJECT_STORAGE_PUBLIC_CONTAINER: str = ""
    OBJECT_STORAGE_PRIVATE_CONTAINER: str = ""

    # Azure Blob Storage v1 authentication.
    AZURE_STORAGE_ACCOUNT_URL: str = ""
    AZURE_STORAGE_AUTH_MODE: Literal["account_key"] = "account_key"
    AZURE_STORAGE_ACCOUNT_KEY: str = ""

    # AWS settings remain available for S3 deployments and explicit AWS integrations.
    AWS_ACCESS_KEY_ID: str = ""
    AWS_SECRET_ACCESS_KEY: str = ""

    AWS_S3_BUCKET_REGION: str = ""
    AWS_S3_BUCKET_NAME: str = ""
    AWS_S3_PUBLIC_BUCKET_NAME: str = ""
    AWS_S3_PUBLIC_BUCKET_IMAGE_FOLDER: str = ""
    AWS_S3_FILES_FOLDER: str = "files"
    AWS_S3_RUNS_FOLDER: str = "runs"
    # Full ActionResult (action run) JSON payloads are stored under this prefix in S3.
    # Key format: {AWS_S3_ACTION_RUNS_FOLDER}/{user_id}/{action_run_id}.json
    AWS_S3_ACTION_RUNS_FOLDER: str = "action_runs"
    CLAUDE_USER_FILES_S3_FOLDER: str = "claude_user_files"

    LOOP_SEPARATOR: str
    ACTION_GRAPH_LOOPS_CONCURRENCY_LIMIT: int = 11

    # Redis Configuration
    REDIS_HOST: str = "localhost"
    REDIS_PORT: int = 6379
    REDIS_DB: int = 0
    REDIS_BATCH_SIZE: int = 500
    REDIS_BATCH_DELAY_SECONDS: float = 0.1
    REDIS_BATCH_MAX_RETRIES: int = 3

    HEARTBEAT_INTERVAL_SEC: int = 10
    REDIS_HEARTBEAT_TOPIC: str = "autobots:heartbeat"

    SCHEDULER_URL: str
    DASHBOARD_URL: str = "http://autobots-dashboard:8002"
    LIBREOFFICE_CONVERSION_API_URL: str = "http://localhost:8003/v1/libreoffice/"
    LIBREOFFICE_PATH: str = "/usr/lib/libreoffice/program/soffice"

    CHROMA_HOST: str
    CHROMA_TOKEN_CREDENTIALS: str

    WEAVIATE_URL: Optional[str] = None
    WEAVIATE_API_KEY: Optional[str] = None

    NEO4J_URI: Optional[str] = None
    NEO4J_USERNAME: Optional[str] = None
    NEO4J_PASSWORD: Optional[str] = None

    TEMPORAL_API_KEY: str = ""
    TEMPORAL_ADDRESS: str = "localhost:7233"
    TEMPORAL_NAMESPACE: str = "default"
    TEMPORAL_TASK_QUEUE: str = "autobots-tasks"
    TEMPORAL_HEARTBEAT_INTERVAL_SECONDS: int = 1
    TEMPORAL_HEARTBEAT_THROTTLE_SECONDS: int = 1
    TEMPORAL_CANCEL_CLEANUP_TIMEOUT_SECONDS: int = 60
    ACTION_GRAPH_START_TO_CLOSE_TIMEOUT_MINUTES: int = 1200
    TOOL_RUN_START_TO_CLOSE_TIMEOUT_MINUTES: int = 600
    TOOL_RUN_INLINE_WAIT_SECONDS: int = 10
    TOOL_RUN_OUTPUT_TEXT_TO_FILE_CHAR_LIMIT: int = 50000

    # Ingestion Worker Configuration
    INGESTION_WORKER_PARSING_STRATEGY: Literal["auto", "fast_with_gemini"] = Field(
        default="auto",
        description="Strategy for parsing documents. 'auto' uses Unstructured's default multimodal parsing. "
        "'fast_with_gemini' uses Gemini for pre-parsing (OCR, etc.) and then Unstructured's fast text-only mode.",
    )
    INGESTION_WORKER_URL: str = "http://autobots-ingestion-worker:8002"

    # --- Ingestion Worker: S3 Sync Settings ---
    INGESTION_WORKER_S3_SYNC_MODE: Literal["WHITELIST", "ALL_USERS", "DISABLED"] = Field(
        default="DISABLED",  # Changed from WHITELIST to DISABLED
        description="Controls the nightly S3 sync: 'WHITELIST' for specific users, 'ALL_USERS' for everyone, 'DISABLED' to turn off.",
    )
    INGESTION_WORKER_S3_SYNC_WHITELIST_USER_IDS: Optional[List[str]] = Field(
        default=["56dd105c-606e-4f6d-8e88-8001d8f4f552", "e29f3392-6124-40f5-8e24-fd6b5ebff875"],
        description="List of user IDs to process when INGESTION_WORKER_S3_SYNC_MODE is 'WHITELIST'.",
    )
    INGESTION_WORKER_S3_SYNC_BLACKLIST_USER_IDS: Optional[List[str]] = Field(
        default=[],
        description="List of user IDs to always exclude from the S3 sync, regardless of the sync mode.",
    )

    # --- Ingestion Worker: Real-time Mongo Indexer Settings ---
    INGESTION_WORKER_MONGO_INDEXER_TARGET_USER_IDS: Optional[List[str]] = Field(
        default=[
            "56dd105c-606e-4f6d-8e88-8001d8f4f552",
            # "e29f3392-6124-40f5-8e24-fd6b5ebff875",
            # "e2e00000-0000-0000-0000-000000000000" # E2E test user
        ],
        description="Optional list of user IDs to exclusively process for background services. If None, targets all users.",
    )
    INGESTION_WORKER_MONGO_INDEXER_POLL_INTERVAL_SECONDS: conint(gt=0) = Field(
        default=3, description="Seconds to wait between polling cycles. Must be greater than 0."
    )
    INGESTION_WORKER_MONGO_INDEXER_BATCH_SIZE: conint(gt=0, le=500) = Field(
        default=100,
        description="Max number of documents to fetch from MongoDB in a single batch.",
    )
    # Defines which collections are indexed and where.
    #   "Actions": []                  -> Disabled
    #   "Actions": ["weaviate"]        -> Weaviate only
    #   "Actions": ["weaviate", "neo4j"]  -> Weaviate and Neo4j
    INGESTION_WORKER_MONGO_INDEXER_TARGETS: Dict[str, List[Literal["weaviate", "neo4j"]]] = Field(
        default_factory=lambda: {
            # Default Configuration: Only EventResults are indexed to both backends.
            # The "Chats" collection is indexed to Weaviate
            "Actions": [],
            "ActionGraphs": [],
            "EventResults": [],  # Changed from ["weaviate", "neo4j"],
            "Chats": [],  # Changed from ["weaviate"],
        },
        description="Defines collections to index and their backends. An empty list `[]` disables a collection.",
    )
    INGESTION_WORKER_MONGO_INDEXER_WEAVIATE_COLLECTION_NAME: str = Field(
        default="synced_files", description="The name of the Weaviate collection to upsert into."
    )
    INGESTION_WORKER_MONGO_INDEXER_WEAVIATE_CACHE_SIZE: conint(ge=1) = Field(
        default=128, description="Max number of WeaviateStore instances to keep in LRU cache."
    )
    # New settings for flexible bootstrapping
    INGESTION_WORKER_MONGO_INDEXER_BOOTSTRAP_MODE: Literal[
        "current", "hours_ago", "full_history"
    ] = Field(
        default="current",
        description="Bootstrap strategy: 'current' starts from a few seconds ago, 'hours_ago' starts from N hours back, 'full_history' processes all data.",
    )
    INGESTION_WORKER_MONGO_INDEXER_BOOTSTRAP_HOURS_BACK: conint(ge=1, le=720) = Field(
        default=24,
        description="Hours to look back when bootstrap_mode is 'hours_ago' (max 30 days).",
    )

    # Ingestion Worker Redis Queues
    REDIS_INGESTION_HIGH_PRIORITY_QUEUE: str = "ingestion:high_priority_jobs"
    REDIS_INGESTION_LOW_PRIORITY_QUEUE: str = "ingestion:low_priority_jobs"

    # Playwright Worker
    MAX_JOBS_PER_BROWSER: int = 200
    BROWSER_IDLE_RESTART_SECONDS: int = 900
    PLAYWRIGHT_MAX_RSS_MB: int = 2000
    PLAYWRIGHT_CDP_ENDPOINT: str = ""  # "auto" to launch Chrome, or "http://host:9222"
    AUTOBOTS_PLAYWRIGHT_API_URL: str = "http://localhost:8010/v1/"

    # OpenAI Configuration
    OPENAI_ORG_ID: str = ""
    OPENAI_API_KEY: str = ""
    OPENAI_TIMEOUT: int = 900
    OPENAI_VECTOR_STORE_EXPIRES_AFTER_DAYS: int = 7
    SUBSCRIBER_OPENAI_API_KEY: str = ""
    SUBSCRIBER_OPENAI_ORG_ID: str = ""

    # Azure OpenAI Configuration
    OPENAI_TYPE_IS_AZURE: bool = True  # Always keep azure
    AZURE_OPENAI_BASE_URL_EASTUS2: str = "https://anura-m6vs2wwr-eastus2.openai.azure.com/"
    AZURE_OPENAI_API_KEY_EASTUS2: str = ""
    AZURE_OPENAI_API_VERSION_EASTUS2_GPT_4_1: str = "2025-04-01-preview"
    AZURE_OPENAI_API_VERSION_EASTUS2_GPT_4_1_MINI: str = "2025-04-01-preview"
    AZURE_OPENAI_API_VERSION_EASTUS2_GPT_4_1_NANO: str = "2025-04-01-preview"
    AZURE_OPENAI_API_VERSION_EASTUS2_GPT_4O: str = "2025-04-01-preview"
    AZURE_OPENAI_API_VERSION_EASTUS2_GPT_4O_MINI: str = "2025-04-01-preview"
    AZURE_OPENAI_API_VERSION_EASTUS2_MODEL_ROUTER: str = "2025-04-01-preview"
    AZURE_OPENAI_API_VERSION_EASTUS2_O1: str = "2025-04-01-preview"
    AZURE_OPENAI_API_VERSION_EASTUS2_O1_MINI: str = "2025-04-01-preview"
    AZURE_OPENAI_API_VERSION_EASTUS2_O3: str = "2025-04-01-preview"
    AZURE_OPENAI_API_VERSION_EASTUS2_O3_MINI: str = "2025-04-01-preview"
    AZURE_OPENAI_API_VERSION_EASTUS2_O3_PRO: str = "2025-04-01-preview"
    AZURE_OPENAI_API_VERSION_EASTUS2_O4_MINI: str = "2025-04-01-preview"
    AZURE_OPENAI_API_VERSION_EASTUS2_COMPUTER_USE_PREVIEW: str = "2025-04-01-preview"
    AZURE_OPENAI_API_VERSION_EASTUS2_SORA: str = "preview"

    AZURE_OPENAI_TTS_BASE_URL: str = "https://anura-m1soifcz-northcentralus.openai.azure.com"
    AZURE_OPENAI_TTS_API_KEY: str = ""
    AZURE_OPENAI_TTS_API_VERSION: str = "2024-05-01-preview"

    AZURE_OPENAI_WHISPER_BASE_URL: str = "https://anura-m1soifcz-northcentralus.openai.azure.com"
    AZURE_OPENAI_WHISPER_API_KEY: str = ""
    AZURE_OPENAI_WHISPER_API_VERSION: str = "2024-05-01-preview"

    # REF: AZURE_API_VERSION: https://learn.microsoft.com/en-us/azure/ai-services/openai/api-version-deprecation
    AZURE_OPENAI_GPT_FOUR_O_BASE_URL: str = (
        "https://anura-m6vs2wwr-eastus2.cognitiveservices.azure.com"
    )
    AZURE_OPENAI_GPT_FOUR_O_API_KEY: str = ""
    AZURE_OPENAI_GPT_FOUR_O_API_VERSION: str = "2025-01-01-preview"

    AZURE_OPENAI_NCUS_GPT_FOUR_O_BASE_URL: str = (
        "https://anura-m1soifcz-northcentralus.openai.azure.com"
    )
    AZURE_OPENAI_NCUS_GPT_FOUR_O_API_KEY: str = ""
    AZURE_OPENAI_NCUS_GPT_FOUR_O_API_VERSION: str = "2024-08-01-preview"

    AZURE_OPENAI_GPT_FOUR_O_MINI_BASE_URL: str = (
        "https://anura-m6vs2wwr-eastus2.cognitiveservices.azure.com"
    )
    AZURE_OPENAI_GPT_FOUR_O_MINI_API_KEY: str = ""
    AZURE_OPENAI_GPT_FOUR_O_MINI_API_VERSION: str = "2025-01-01-preview"

    AZURE_OPENAI_O_ONE_BASE_URL: str = "https://anura-m6vs2wwr-eastus2.cognitiveservices.azure.com"
    AZURE_OPENAI_O_ONE_API_KEY: str = ""
    AZURE_OPENAI_O_ONE_API_VERSION: str = "2025-01-01-preview"

    AZURE_OPENAI_O_ONE_MINI_BASE_URL: str = (
        "https://anura-m6vs2wwr-eastus2.cognitiveservices.azure.com"
    )
    AZURE_OPENAI_O_ONE_MINI_API_KEY: str = ""
    AZURE_OPENAI_O_ONE_MINI_API_VERSION: str = "2025-01-01-preview"

    AZURE_OPENAI_O_THREE_MINI_BASE_URL: str = (
        "https://anura-m6vs2wwr-eastus2.cognitiveservices.azure.com"
    )
    AZURE_OPENAI_O_THREE_MINI_API_KEY: str = ""
    AZURE_OPENAI_O_THREE_MINI_API_VERSION: str = "2025-01-01-preview"

    AZURE_OPENAI_GPT_4_32K_BASE_URL: str = (
        "https://anura-m72gq9st-swedencentral.cognitiveservices.azure.com"
    )
    AZURE_OPENAI_GPT_4_32K_API_KEY: str = ""
    AZURE_OPENAI_GPT_4_32K_API_VERSION: str = "2025-01-01-preview"

    AZURE_OPENAI_EMBEDDINGS_BASE_URL: str = "https://kiwi-dev.openai.azure.com/"
    AZURE_OPENAI_EMBEDDINGS_API_KEY: str = ""
    AZURE_OPENAI_EMBEDDINGS_API_VERSION: str = "2024-08-01-preview"

    AZURE_OPENAI_DALLE_BASE_URL: str = "https://kiwi-dev.openai.azure.com/"
    AZURE_OPENAI_DALLE_API_KEY: str = ""
    AZURE_OPENAI_DALLE_API_VERSION: str = "2024-06-01"

    AZURE_OPENAI_ASST_BASE_URL: str = "https://kiwi-dev.openai.azure.com"
    AZURE_OPENAI_ASST_API_KEY: str = ""
    AZURE_OPENAI_ASST_API_VERSION: str = "2024-12-01-preview"

    AZURE_DEEPSEEK_R1_BASE_URL: str = "https://anura-m6vs2wwr-eastus2.services.ai.azure.com/models"
    AZURE_DEEPSEEK_R1_API_KEY: str = ""

    # LLM Provider API Keys
    CLAUDE_API_KEY: str = ""
    CLAUDE_VERTEX_API_KEY: str = ""
    CLAUDE_VERTEX_REGION: str = ""
    CLAUDE_VERTEX_PROJECT_ID: str = ""

    GEMINI_API_KEY: str = ""

    GROK_API_KEY: str = ""
    GROK_MANAGEMENT_API_KEY: str = ""

    DEEPSEEK_API_KEY: str = ""
    PERPLEXITY_API_KEY: str = ""
    OPENROUTER_API_KEY: str = ""
    OPENROUTER_SYSTEM_ALLOWED_MODELS: str = "deepseek/,moonshotai/,minimax/,thudm/,zhipuai/"
    OPENROUTER_SYSTEM_FALLBACK_MODEL: str = "deepseek/deepseek-v4-pro"
    OPENROUTER_SYSTEM_HTTP_REFERER: str = "https://kiwicode.ai"
    OPENROUTER_SYSTEM_X_OPENROUTER_TITLE: str = "Kiwi AI"

    @computed_field
    @property
    def OPENROUTER_SYSTEM_ALLOWED_MODEL_PREFIXES(self) -> List[str]:
        return [p.strip() for p in self.OPENROUTER_SYSTEM_ALLOWED_MODELS.split(",") if p.strip()]

    # Third-party API Keys
    ASSEMBLYAI_API_KEY: str = ""
    DEEPGRAM_API_KEY: str = ""
    UNSTRUCTURED_API_KEY: str = ""
    UNSTRUCTURED_URL: str = "https://api.unstructuredapp.io/general/v0/general"
    RAPID_API_KEY: str = ""
    OPUS_API_KEY: str = ""
    CARTESIA_API_KEY: str = ""
    REPLICATE_API_KEY: str = ""
    UNSPLASH_ACCESS_KEY: str = ""
    EXA_API_KEY: str = ""
    DAYTONA_API_KEY: str = ""
    DAYTONA_API_URL: str = "https://app.daytona.io/api"
    AUTOBOTS_API_URL: str = "https://api.runkai.ai"
    # Public base URL of THIS API server, reachable from Daytona sandboxes
    # (used for presign callbacks). Example: "https://api.runkai.ai"
    APP_BASE_URL: str = ""

    # Stripe Billing (Kiwi-Code subscriptions)
    STRIPE_SECRET_KEY: str = ""
    STRIPE_PRICE_ID_AUTOCODE: str = ""
    STRIPE_SUCCESS_URL: str = ""
    STRIPE_CANCEL_URL: str = ""
    STRIPE_WEBHOOK_SECRET: str = ""
    STRIPE_PORTAL_RETURN_URL: str = ""
    CODE_MEETKIWI_URL: str = ""
    STRIPE_PROCESSING_URL: str = ""
    # Optional override when APP_BASE_URL differs from the externally-routable URL (proxy setups)
    DAYTONA_CALLBACK_BASE_URL: str = ""

    # Daytona Toolbox base URL (used for cross-worker polling/fallback HTTP calls)
    DAYTONA_TOOLBOX_BASE_URL: str = "https://proxy.app.daytona.io/toolbox"
    # Default sandbox image used for custom CPU/RAM sizing (CreateSandboxFromImageParams).
    # Must include python3 + pip; validated working: daytonaio/sandbox:latest
    DAYTONA_DEFAULT_IMAGE: str = "daytonaio/sandbox:latest"

    # Daytona sandbox network limits (docs: allow list must be IPv4 CIDR blocks ONLY, max 10 entries)
    # Example: "208.80.154.232/32,192.168.1.0/24"
    # Hostnames/domains/IPv6 are NOT supported by Daytona allow lists.
    DAYTONA_NETWORK_ALLOW_LIST: str = ""
    DAYTONA_NETWORK_BLOCK_ALL: bool = False

    # Daytona lifecycle policy defaults.
    #
    # New sandboxes are ephemeral unless the tool caller explicitly requests
    # lifecycle="retained" or lifecycle="persistent".
    #
    # Daytona does not necessarily count an internal background process as
    # sandbox activity. Ephemeral auto-stop is therefore derived from the
    # caller-declared max_runtime_minutes rather than using a short fixed timer.
    DAYTONA_EPHEMERAL_AUTO_STOP_GRACE_MINUTES: int = 1
    DAYTONA_EPHEMERAL_TTL_GRACE_MINUTES: int = 5
    DAYTONA_MAX_EPHEMERAL_TTL_MINUTES: int = 1440

    # Retained/persistent sandboxes stop when idle and are archived afterward.
    # Their idle timeout is supplied explicitly by the tool caller.
    DAYTONA_RETAINED_AUTO_ARCHIVE_INTERVAL_MINUTES: int = 60
    DAYTONA_PERSISTENT_AUTO_ARCHIVE_INTERVAL_MINUTES: int = 60

    # Daytona sandbox compute sizing caps (defaults match Daytona org default limits as documented).
    # If your Daytona org has higher limits, override these via environment variables.
    DAYTONA_MAX_CPU: int = 4
    DAYTONA_MAX_MEMORY_GIB: int = 8

    DISCORD_SERVER_ID: str = ""
    DISCORD_TOKEN: str = ""
    DISCORD_CHANNEL_ID: str = ""
    SERP_API_KEY: str = ""
    PINECONE_ENVIRONMENT: str = ""
    PINECONE_API_KEY: str = ""

    # OAuth Configuration - Slack
    SLACK_CLIENT_ID: str = ""
    SLACK_CLIENT_SECRET: str = ""
    SLACK_SCOPE: str = ""
    SLACK_USER_SCOPE: str = ""
    SLACK_REDIRECT_URL: str = ""
    SLACK_USER_OAUTH_TOKEN: str = ""

    # App Creds / App Auth encryption keys (RSA-4096 hybrid envelope encryption)
    ENCRYPTION_PUBLIC_KEY_B64: str = ""
    ENCRYPTION_PRIVATE_KEY_B64: str = ""

    # OAuth Configuration - Google
    GOOGLE_CLIENT_ID: str = ""
    GOOGLE_CLIENT_SECRET: str = ""
    GOOGLE_READ_ONLY_CLIENT_ID: str = ""
    GOOGLE_READ_ONLY_CLIENT_SECRET: str = ""
    GOOGLE_SCOPES: str = ""
    GOOGLE_REDIRECT_URL: str = ""
    GOOGLE_READ_ONLY_SCOPES: str = ""
    GOOGLE_READ_ONLY_REDIRECT_URL: str = ""
    GOOGLE_ADS_CLIENT_ID: str = ""
    GOOGLE_ADS_CLIENT_SECRET: str = ""
    GOOGLE_ADS_REDIRECT_URL: str = ""
    GOOGLE_ADS_SCOPES: str = ""
    GOOGLE_ADS_DEVELOPER_TOKEN: str = ""
    GOOGLE_ADS_LOGIN_CUSTOMER_ID: str = ""
    GOOGLE_ADS_MCC_REFRESH_TOKEN: str = ""
    GOOGLE_ADS_MCC_CLIENT_ID: str = ""
    GOOGLE_ADS_MCC_CLIENT_SECRET: str = ""
    GOOGLE_DRIVE_CLIENT_ID: str = ""
    GOOGLE_DRIVE_CLIENT_SECRET: str = ""
    GOOGLE_DRIVE_REDIRECT_URL: str = ""
    GOOGLE_DRIVE_SCOPES: str = "https://www.googleapis.com/auth/drive.readonly"
    GOOGLE_GMAIL_CLIENT_ID: str = ""
    GOOGLE_GMAIL_CLIENT_SECRET: str = ""
    GOOGLE_GMAIL_REDIRECT_URL: str = ""
    GOOGLE_GMAIL_SCOPES: str = "https://www.googleapis.com/auth/gmail.modify"
    GOOGLE_ANALYTICS_CLIENT_ID: str = ""
    GOOGLE_ANALYTICS_CLIENT_SECRET: str = ""
    GOOGLE_ANALYTICS_REDIRECT_URL: str = ""
    GOOGLE_ANALYTICS_SCOPES: str = "https://www.googleapis.com/auth/analytics.readonly"
    GOOGLE_YOUTUBE_CLIENT_ID: str = ""
    GOOGLE_YOUTUBE_CLIENT_SECRET: str = ""
    GOOGLE_YOUTUBE_REDIRECT_URL: str = ""
    GOOGLE_YOUTUBE_SCOPES: str = "https://www.googleapis.com/auth/youtube.readonly"
    GOOGLE_CLOUD_CLIENT_ID: str = ""
    GOOGLE_CLOUD_CLIENT_SECRET: str = ""
    GOOGLE_CLOUD_REDIRECT_URL: str = ""
    GOOGLE_CLOUD_SCOPES: str = "https://www.googleapis.com/auth/cloud-platform"
    # Google Custom Search Engine Settings
    GOOGLE_CSE_CX: str = ""
    GOOGLE_CSE_API_KEY: str = ""

    # OAuth Configuration - Intuit
    INTUIT_CLIENT_ID: str = ""
    INTUIT_CLIENT_SECRET: str = ""
    INTUIT_SANDBOX_CLIENT_ID: str = ""
    INTUIT_SANDBOX_CLIENT_SECRET: str = ""
    INTUIT_SCOPES: str = ""
    INTUIT_REDIRECT_URL: str = ""
    INTUIT_ENVIRONMENT: str = ""

    # OAuth Configuration - Salesforce
    SALESFORCE_CLIENT_ID: str = ""
    SALESFORCE_CLIENT_SECRET: str = ""
    SALESFORCE_SCOPES: str = ""
    SALESFORCE_REDIRECT_URL: str = ""

    # OAuth Configuration - Box
    BOX_CLIENT_ID: str = ""
    BOX_CLIENT_SECRET: str = ""
    BOX_REDIRECT_URL: str = ""

    # OAuth Configuration - Dropbox
    DROPBOX_CLIENT_ID: str = ""
    DROPBOX_CLIENT_SECRET: str = ""
    DROPBOX_REDIRECT_URL: str = ""

    # OAuth Configuration - Outlook
    OUTLOOK_CLIENT_ID: str = ""
    OUTLOOK_CLIENT_SECRET: str = ""
    OUTLOOK_SCOPES: str = ""
    OUTLOOK_REDIRECT_URL: str = ""

    # OAuth Configuration - Meta
    META_CLIENT_ID: str = ""
    META_CLIENT_SECRET: str = ""
    META_APP_ID: str = ""
    META_APP_SECRET: str = ""
    META_REDIRECT_URL: str = ""
    META_SCOPES: str = ""
    META_ADS_READ_ONLY_REDIRECT_URL: str = ""
    META_ADS_READ_ONLY_SCOPES: str = ""

    # OAuth Configuration - HubSpot
    HUBSPOT_CLIENT_ID: str = ""
    HUBSPOT_CLIENT_SECRET: str = ""
    HUBSPOT_REDIRECT_URL: str = ""
    HUBSPOT_SCOPES: str = ""

    # OAuth Configuration - Smartsheet
    SMARTSHEET_CLIENT_ID: str = ""
    SMARTSHEET_CLIENT_SECRET: str = ""
    SMARTSHEET_REDIRECT_URL: str = ""
    SMARTSHEET_SCOPES: str = ""

    # OAuth Configuration - Fireflies
    FIREFLIES_CLIENT_ID: str = ""
    FIREFLIES_CLIENT_SECRET: str = ""
    FIREFLIES_REDIRECT_URL: str = ""

    # OAuth Configuration - GitHub
    KEY_GITHUB_CLIENT_ID: str = ""
    KEY_GITHUB_CLIENT_SECRET: str = ""
    KEY_GITHUB_REDIRECT_URL: str = ""
    KEY_GITHUB_SCOPES: str = ""

    # OAuth Configuration - Asana
    ASANA_CLIENT_ID: str = ""
    ASANA_CLIENT_SECRET: str = ""
    ASANA_REDIRECT_URL: str = ""
    ASANA_SCOPES: str = ""

    # OAuth Configuration - Zoom
    ZOOM_CLIENT_ID: str = ""
    ZOOM_CLIENT_SECRET: str = ""
    ZOOM_REDIRECT_URL: str = ""
    ZOOM_SCOPES: str = ""

    # OAuth Configuration - WhatsApp
    WHATSAPP_REDIRECT_URL: str = ""
    WHATSAPP_SCOPES: str = ""

    # OAuth Configuration - iCIMS
    ICIMS_REDIRECT_URL: str = ""
    ICIMS_SCOPES: str = ""

    # OAuth Configuration - Workday
    WORKDAY_REDIRECT_URL: str = ""
    WORKDAY_SCOPES: str = (
        ""  # Space-separated functional areas (e.g., "System Integration Staffing")
    )

    # OAuth Configuration - DATEV (client_id and client_secret are user-specific, stored in app_creds)
    DATEV_REDIRECT_URL: str = ""
    DATEV_SCOPES: str = (
        ""  # e.g., "openid profile email datev:accounting:clients accounting:documents"
    )

    # UKG OAuth Settings - client_id and client_secret are user-specific (stored in app_creds)
    UKG_REDIRECT_URL: str = ""
    UKG_SCOPES: str = ""

    # Computed values (depend on AWS secrets above)
    ALLOWED_ORIGINS: List[str] = []

    # Internal application settings
    TIMEZONE: str = "UTC"
    ALLOW_SYSTEM_CONN_TO_SYSTEM_DOMAIN: bool = True
    RUNNABLE_BATCH_LIMIT_FOR_URLS: int = 100
    RUNNABLE_BATCH_LIMIT_FOR_URLS_ACTIONS: int = 25
    EVENT_MONITOR_LOOP_INTERVAL_SEC: int = 300
    EVENT_MONITOR_LOOP_ONCE: bool = False
    STUCK_ACTION_GRAPH_GRAPHS_TO_RUN_LIMIT: int = 2
    STUCK_ACTION_GRAPH_THRESHOLD_MINUTES: int = 720
    ACTION_GRAPH_MAX_INPUT_CHARS: int = (
        75000  # Max characters for Action Graph input before offloading to S3 file
    )
    # Warn-only until we have measured how many stored graphs fail; flip to reject on write.
    ACTION_GRAPH_VALIDATION_ENFORCED: bool = False
    # Least time between two intermediate saves of the same run. A loop node reports after every
    # iteration so the UI can show partial results, but that is a progress requirement served by
    # SSE - and each save rewrites the whole run document, which grows as the loop collates. The
    # node-level save that follows the loop is unconditional, so nothing is lost by spacing these.
    # 0 restores a save per iteration.
    ACTION_GRAPH_RESULT_FLUSH_INTERVAL_SECONDS: float = 3.0
    # How many times one redirect edge may fire in a run before the run pauses and asks a human
    # (D5). Per (i, j) pair, not global: a global cap trips on a wide graph with many independent
    # healthy cycles. Raised per-run by editing the pair's `limit` on the result document.
    ACTION_GRAPH_REDIRECT_BUDGET_DEFAULT: int = 3
    USER_INPUT_TEXT_TO_FILE_CHAR_LIMIT: int = 150000
    TEMPORARY_DIR: str = "to_del"
    DATASTORE_IDENTIFIER: str = "datastore"
    SCHEDULE_JOBSTORE_MONGO_DB_COLLECTION_NAME: str = "RUNKAI_APSCHEDULER_JOBS"

    # APP_HOST: str = "0.0.0.0"
    # APP_PORT: int = 8000
    # APP_RELOAD: bool = True
    # APP_LOG_LEVEL: str = "debug"
    # APP_WORKERS: int = 1

    # USEAPI_NET_TOKEN: str
    # USEAPI_NET_END_POINT_URL: str

    # CLAID_API_KEY: str
    # CLAID_SIDE_S3_BUCKET: str
    # CLAID_PATH_PREFIX: str

    # LINKEDIN_SCRAPING_API_KEY: str

    # STABILITY_HOST: str = None
    # STABILITY_KEY: str = None
    # STABLE_DIFFUSION_API_KEY: str = None

    # API route constants
    API_v1: str = "/v1"
    API_Hello: str = "/hello"
    API_AUTH: str = "/auth"
    API_AUTH_TOKEN: str = "/token"
    API_APP_CREDS: str = "/app-creds"
    API_APP_AUTH: str = "/app-auth"
    API_INTEGRATIONS: str = "/integrations"
    API_SECRETS: str = "/secrets"
    API_ACTIONS: str = "/actions"
    API_TOOLS: str = "/tools"
    API_MCPs: str = "/mcps"
    API_ACTION_TYPES: str = "/action_types"
    API_ACTION_RESULTS: str = "/action_results"
    API_TOKEN_USAGE: str = "/token_usage"
    API_ACTION_GRAPHS: str = "/action_graphs"
    API_ACTION_GRAPHS_RESULTS: str = "/action_graphs_results"
    API_ACTION_TEAMS: str = "/action_teams"
    API_ACTION_TEAMS_RESULTS: str = "/action_teams_results"
    API_BATCHES: str = "/batches"
    API_POD: str = "/pod"
    API_EVENT_RESULTS: str = "/event_results"
    API_SESSIONS: str = "/sessions"
    API_SCHEDULES: str = "/schedules"
    API_LIBREOFFICE: str = "/libreoffice"
    API_DASHBOARD: str = "/dashboard"
    API_DATASTORE: str = "/datastore"
    API_INGESTION: str = "/ingestion"
    API_V1_INGESTION: str = "/v1_ingestion"
    API_VECTORSTORE: str = "/vectorstore"
    API_FILES: str = "/files"
    API_OPENAI_STORAGE: str = "/openai_storage"
    API_OPENROUTER: str = "/openrouter"
    API_POLL_GRAPHS: str = "/poll_graphs"
    API_ACTION_CHATS: str = "/action_chats"
    API_AGENTS: str = "/agents"
    API_GRAPHS: str = "/graphs"
    API_WEBHOOKS: str = "/webhooks"
    API_WEBHOOKS_EVENTS: str = "/event"
    API_CHARACTERS: str = "/characters"
    API_PROMPTS: str = "/prompts"
    API_TEAMS: str = "/teams"
    API_SERVER_SENT_EVENTS: str = "/server_sent_events"
    API_SHARE_DATA: str = "/share"
    API_TERMINAL: str = "/terminal"
    API_USERS: str = "/users"
    API_ACCESS: str = "/user-access"
    API_STRIPE: str = "/stripe"

    API_TAG_EXTERNAL_APP_AUTH: str = "Integrations"

    API_PATH_SLACK: str = "/slack"
    API_PATH_GOOGLE: str = "/google"
    API_PATH_INTUIT: str = "/intuit"
    API_PATH_SIRI: str = "/siri"
    API_PATH_WHATSAPP: str = "/whatsapp"
    API_PATH_ZOOM: str = "/zoom"
    API_PATH_UKG: str = "/ukg"
    API_PATH_ICIMS: str = "/icims"
    API_PATH_WORKDAY: str = "/workday"
    API_PATH_PLAID: str = "/plaid"
    API_PATH_TIPALTI: str = "/tipalti"
    API_PATH_DATEV: str = "/datev"
    
    ## Autocode List of System Action IDs
    
    AUTOCODE_SYSTEM_ACTION_IDS: list[str] = []

    class ConfigDict:
        # env_file = f".env.local"
        env_file_encoding = "utf-8"


class SettingsProvider:
    _env_file: str = None
    _settings: Settings = None

    @staticmethod
    @lru_cache
    def set_env_file(_env_file: str = ".env.local") -> None:
        SettingsProvider._env_file = _env_file

    @staticmethod
    @lru_cache
    def set() -> None:
        SettingsProvider._settings = Settings(_env_file=SettingsProvider._env_file)
        SettingsProvider.check_for_none(SettingsProvider._settings)
        SettingsProvider._settings.ALLOWED_ORIGINS = SettingsProvider.list_allow_origins(
            SettingsProvider._settings.ALLOW_ORIGINS
        )
        logger.debug(f"Allowed origins: {SettingsProvider._settings.ALLOWED_ORIGINS}")

    @staticmethod
    def origin_of(url: str) -> str:
        """The scheme, host and port of a URL, in one shape both sides of a comparison use.

        `HttpUrl.host` drops the port while `str(HttpUrl(...))` keeps it, so comparing one
        against the other never matches an origin carrying a port. A default port is
        dropped here: a URL that omits it and one that spells it out are the same origin.
        """
        parsed = HttpUrl(url)
        port = f":{parsed.port}" if parsed.port and parsed.port not in (80, 443) else ""
        return f"{parsed.scheme}://{parsed.host}{port}/"

    @staticmethod
    @lru_cache
    def list_allow_origins(allow_origins: str) -> List[str]:
        allow_origins_splits = allow_origins.split(",")
        allowed_origins: List[str] = []
        for allow_origin in allow_origins_splits:
            try:
                url = HttpUrl(allow_origin)
                allowed_origins.append(str(url))
            except Exception:
                pass
        return allowed_origins

    @staticmethod
    async def get() -> Settings:
        return SettingsProvider._settings

    @staticmethod
    def sget() -> Settings:
        if not SettingsProvider._settings:
            SettingsProvider.set()
        return SettingsProvider._settings

    @staticmethod
    def get_system_email_domain() -> str:
        settings = SettingsProvider.sget()
        return (settings.SYSTEM_EMAIL_DOMAIN or "").strip().lower().lstrip("@")

    @staticmethod
    def is_system_email(email: Optional[str]) -> bool:
        if not email:
            return False

        configured_domain = SettingsProvider.get_system_email_domain()
        if not configured_domain:
            return False

        normalized_email = email.strip().lower()
        if "@" not in normalized_email:
            return False

        email_domain = normalized_email.rsplit("@", 1)[-1]
        return email_domain == configured_domain

    @staticmethod
    def check_for_none(settings: Settings):
        for field in settings.__dict__.keys():
            if settings.__dict__[field] is None or settings.__dict__[field] == "":
                logger.error(f"Field: {field} is not set")
