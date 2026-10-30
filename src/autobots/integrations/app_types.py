from enum import Enum


class ExtendedEnum(str, Enum):
    @classmethod
    def list(cls):
        return list(map(lambda c: c.value, cls))

    @classmethod
    def set(cls):
        return set(map(lambda c: c.value, cls))


class AppTypes(ExtendedEnum):
    anon = "anon"
    custom = "custom"
    slack = "slack"
    aws = "aws"
    google = "google"
    google_read_only = "google_read_only"
    google_ads = "google_ads"
    google_drive = "google_drive"
    google_gmail = "google_gmail"
    google_analytics = "google_analytics"
    youtube = "youtube"
    google_cloud = "google_cloud"
    notion = "notion"
    jira = "jira"
    openai = "openai"
    gemini = "gemini"
    claude = "claude"
    zoho = "zoho"
    zendesk = "zendesk"
    intuit = "intuit"
    databricks = "databricks"
    snowflake = "snowflake"
    salesforce = "salesforce"
    airtable = "airtable"
    box = "box"
    firecrawl = "firecrawl"
    outlook = "outlook"
    dropbox = "dropbox"
    superset = "superset"
    meta = "meta"
    meta_ads_read_only = "meta_ads_read_only"
    looker = "looker"
    grok = "grok"
    deepgram = "deepgram"
    cartesia = "cartesia"
    slack_user = "slack_user"
    sap = "sap"
    hubspot = "hubspot"
    stripe = "stripe"
    github = "github"
    netsuite = "netsuite"
    nango = "nango"
    assemblyai = "assemblyai"
    replicate_flux = "replicate_flux"
    exa = "exa"
    opus = "opus"
    google_serp = "google_serp"
    adp = "adp"
    asana = "asana"
    unstructuredio = "unstructuredio"
    whatsapp = "whatsapp"
    zoom = "zoom"
    plaid = "plaid"
    spotify = "spotify"
    ukg = "ukg"
    icims = "icims"
    workday = "workday"
    deepseek = "deepseek"
    muso = "muso"
    vercel = "vercel"
    datev = "datev"
    grafana = "grafana"
    flyio = "flyio"
    gamma_ai = "gamma_ai"
    thesys = "thesys"
    remotion = "remotion"
    tipalti = "tipalti"
    northflank = "northflank"
    chargebee = "chargebee"
    datadog = "datadog"
    ordway = "ordway"
    sigma_computing = "sigma_computing"
    extend_ai = "extend_ai"
    reducto = "reducto"
    daytona = "daytona"
    perplexity = "perplexity"
    bluedot = "bluedot"
    smartsheet = "smartsheet"
    fireflies = "fireflies"
    avalara = "avalara"
    airflow = "airflow"
    vertex_ai = "vertex_ai"
    azure_openai = "azure_openai"
    openrouter = "openrouter"
    retell = "retell"
    autobots = "autobots"
    temporal = "temporal"


# Google OAuth integrations that share tokens under the same Google Cloud project.
# Revoking a token for any one of these invalidates tokens for all others in the set.
# Used by UserIntegration.delete to decide whether provider-side revocation is safe.
GOOGLE_APP_TYPES = {
    AppTypes.google,
    AppTypes.google_read_only,
    AppTypes.google_ads,
    AppTypes.google_drive,
    AppTypes.google_gmail,
    AppTypes.google_analytics,
    AppTypes.youtube,
    AppTypes.google_cloud,
}


"""
AppTypes mapped to:
1. api file top level keys
2. app creds
3. app_auth (using api creds)

When a tool/action of API_CALL is run:
1. also take in app_type
2. so then using user_id and app_type I can fetch App_Auth
"""
