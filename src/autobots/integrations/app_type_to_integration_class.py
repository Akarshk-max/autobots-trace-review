from src.autobots.integrations.app_types import AppTypes
from src.autobots.integrations.integration_type.adp_auth import ADPAuth
from src.autobots.integrations.integration_type.airflow_auth import AirflowAuth
from src.autobots.integrations.integration_type.airtable_auth import AirtableAuth
from src.autobots.integrations.integration_type.asana_auth import AsanaAuth
from src.autobots.integrations.integration_type.assemblyai_auth import AssemblyAiAuth
from src.autobots.integrations.integration_type.autobots_auth import AutobotsAuth
from src.autobots.integrations.integration_type.avalara_auth import AvalaraAuth
from src.autobots.integrations.integration_type.aws_auth import AwsAuth
from src.autobots.integrations.integration_type.azure_openai_auth import AzureOpenAIAuth
from src.autobots.integrations.integration_type.bluedot_auth import BluedotAuth
from src.autobots.integrations.integration_type.box_auth import BoxAuth
from src.autobots.integrations.integration_type.cartesia_auth import CartesiaAuth
from src.autobots.integrations.integration_type.chargebee_auth import ChargebeeAuth
from src.autobots.integrations.integration_type.claude_auth import ClaudeAuth
from src.autobots.integrations.integration_type.custom_auth import CustomAuth
from src.autobots.integrations.integration_type.databricks_auth import DatabricksAuth
from src.autobots.integrations.integration_type.datadog_auth import DatadogAuth
from src.autobots.integrations.integration_type.datev_auth import DatevAuth
from src.autobots.integrations.integration_type.daytona_auth import DaytonaAuth
from src.autobots.integrations.integration_type.deepgram_auth import DeepGramAuth
from src.autobots.integrations.integration_type.deepseek_auth import DeepseekAuth
from src.autobots.integrations.integration_type.dropbox_auth import DropboxAuth
from src.autobots.integrations.integration_type.exa_auth import ExaAuth
from src.autobots.integrations.integration_type.extend_ai_auth import ExtendAIAuth
from src.autobots.integrations.integration_type.firecrawl_auth import FirecrawlAuth
from src.autobots.integrations.integration_type.fireflies_auth import FirefliesAuth
from src.autobots.integrations.integration_type.flyio_auth import FlyioAuth
from src.autobots.integrations.integration_type.gamma_auth import GammaAiAuth
from src.autobots.integrations.integration_type.gemini_auth import GeminiAuth
from src.autobots.integrations.integration_type.github_auth import GitHubAuth
from src.autobots.integrations.integration_type.google_ads_auth import GoogleAdsAuth
from src.autobots.integrations.integration_type.google_analytics_auth import GoogleAnalyticsAuth
from src.autobots.integrations.integration_type.google_auth import GoogleAuth
from src.autobots.integrations.integration_type.google_cloud_auth import GoogleCloudAuth
from src.autobots.integrations.integration_type.google_drive_auth import GoogleDriveAuth
from src.autobots.integrations.integration_type.google_gmail_auth import GoogleGmailAuth
from src.autobots.integrations.integration_type.google_read_only_auth import GoogleReadOnlyAuth
from src.autobots.integrations.integration_type.google_serp_auth import GoogleSerpAuth
from src.autobots.integrations.integration_type.grafana_auth import GrafanaAuth
from src.autobots.integrations.integration_type.grok_auth import GrokAuth
from src.autobots.integrations.integration_type.hubspot_auth import HubSpotAuth
from src.autobots.integrations.integration_type.icims_auth import ICIMSAuth
from src.autobots.integrations.integration_type.intuit_auth import IntuitAuth
from src.autobots.integrations.integration_type.jira_auth import JiraAuth
from src.autobots.integrations.integration_type.looker_auth import LookerAuth
from src.autobots.integrations.integration_type.meta_ads_read_only_auth import (
    MetaAdsReadOnlyAuth,
)
from src.autobots.integrations.integration_type.meta_auth import MetaAuth
from src.autobots.integrations.integration_type.muso_auth import MusoAuth
from src.autobots.integrations.integration_type.nango_auth import NangoAuth
from src.autobots.integrations.integration_type.netsuite_auth import NetSuiteAuth
from src.autobots.integrations.integration_type.northflank_auth import NorthflankAuth
from src.autobots.integrations.integration_type.notion_auth import NotionAuth
from src.autobots.integrations.integration_type.openai_auth import OpenaiAuth
from src.autobots.integrations.integration_type.openrouter_auth import OpenRouterAuth
from src.autobots.integrations.integration_type.opus_auth import OpusAuth
from src.autobots.integrations.integration_type.ordway_auth import OrdwayAuth
from src.autobots.integrations.integration_type.outlook_auth import OutlookAuth
from src.autobots.integrations.integration_type.perplexity_auth import PerplexityAuth
from src.autobots.integrations.integration_type.plaid_auth import PlaidAuth
from src.autobots.integrations.integration_type.reducto_auth import ReductoAuth
from src.autobots.integrations.integration_type.remotion_auth import RemotionAuth
from src.autobots.integrations.integration_type.replicate_flux_auth import ReplicateFluxAuth
from src.autobots.integrations.integration_type.retell_auth import RetellAuth
from src.autobots.integrations.integration_type.salesforce_auth import SalesforceAuth
from src.autobots.integrations.integration_type.sap_auth import SAPAuth
from src.autobots.integrations.integration_type.sigma_computing_auth import SigmaComputingAuth
from src.autobots.integrations.integration_type.slack_auth import SlackAuth
from src.autobots.integrations.integration_type.slack_user_auth import SlackUserAuth
from src.autobots.integrations.integration_type.smartsheet_auth import SmartsheetAuth
from src.autobots.integrations.integration_type.snowflake_auth import SnowflakeAuth
from src.autobots.integrations.integration_type.spotify_auth import SpotifyAuth
from src.autobots.integrations.integration_type.stripe_auth import StripeAuth
from src.autobots.integrations.integration_type.superset_auth import SupersetAuth
from src.autobots.integrations.integration_type.temporal_auth import TemporalAuth
from src.autobots.integrations.integration_type.thesys_auth import ThesysAuth
from src.autobots.integrations.integration_type.tipalti_auth import TipaltiAuth
from src.autobots.integrations.integration_type.ukg_auth import UKGAuth
from src.autobots.integrations.integration_type.unstructured_io_auth import UnstructuredIOAuth
from src.autobots.integrations.integration_type.vercel_auth import VercelAuth
from src.autobots.integrations.integration_type.vertex_ai_auth import VertexAIAuth
from src.autobots.integrations.integration_type.whatsapp_auth import WhatsAppAuth
from src.autobots.integrations.integration_type.workday_auth import WorkdayAuth
from src.autobots.integrations.integration_type.youtube_auth import YouTubeAuth
from src.autobots.integrations.integration_type.zendesk_auth import ZendeskAuth
from src.autobots.integrations.integration_type.zoho_auth import ZohoAuth
from src.autobots.integrations.integration_type.zoom_auth import ZoomAuth

AppTypeToIntegrationClass = {
    AppTypes.google: GoogleAuth,
    AppTypes.google_read_only: GoogleReadOnlyAuth,
    AppTypes.google_ads: GoogleAdsAuth,
    AppTypes.google_drive: GoogleDriveAuth,
    AppTypes.google_gmail: GoogleGmailAuth,
    AppTypes.google_analytics: GoogleAnalyticsAuth,
    AppTypes.youtube: YouTubeAuth,
    AppTypes.google_cloud: GoogleCloudAuth,
    AppTypes.jira: JiraAuth,
    AppTypes.slack: SlackAuth,
    AppTypes.zendesk: ZendeskAuth,
    AppTypes.zoho: ZohoAuth,
    AppTypes.notion: NotionAuth,
    AppTypes.openai: OpenaiAuth,
    AppTypes.gemini: GeminiAuth,
    AppTypes.claude: ClaudeAuth,
    AppTypes.intuit: IntuitAuth,
    AppTypes.databricks: DatabricksAuth,
    AppTypes.snowflake: SnowflakeAuth,
    AppTypes.aws: AwsAuth,
    AppTypes.salesforce: SalesforceAuth,
    AppTypes.airtable: AirtableAuth,
    AppTypes.box: BoxAuth,
    AppTypes.firecrawl: FirecrawlAuth,
    AppTypes.outlook: OutlookAuth,
    AppTypes.dropbox: DropboxAuth,
    AppTypes.superset: SupersetAuth,
    AppTypes.meta: MetaAuth,
    AppTypes.meta_ads_read_only: MetaAdsReadOnlyAuth,
    AppTypes.looker: LookerAuth,
    AppTypes.grok: GrokAuth,
    AppTypes.deepgram: DeepGramAuth,
    AppTypes.cartesia: CartesiaAuth,
    AppTypes.chargebee: ChargebeeAuth,
    AppTypes.slack_user: SlackUserAuth,
    AppTypes.sap: SAPAuth,
    AppTypes.hubspot: HubSpotAuth,
    AppTypes.stripe: StripeAuth,
    AppTypes.github: GitHubAuth,
    AppTypes.netsuite: NetSuiteAuth,
    AppTypes.nango: NangoAuth,
    AppTypes.assemblyai: AssemblyAiAuth,
    AppTypes.replicate_flux: ReplicateFluxAuth,
    AppTypes.exa: ExaAuth,
    AppTypes.opus: OpusAuth,
    AppTypes.google_serp: GoogleSerpAuth,
    AppTypes.adp: ADPAuth,
    AppTypes.asana: AsanaAuth,
    AppTypes.unstructuredio: UnstructuredIOAuth,
    AppTypes.whatsapp: WhatsAppAuth,
    AppTypes.zoom: ZoomAuth,
    AppTypes.plaid: PlaidAuth,
    AppTypes.spotify: SpotifyAuth,
    AppTypes.ukg: UKGAuth,
    AppTypes.icims: ICIMSAuth,
    AppTypes.workday: WorkdayAuth,
    AppTypes.deepseek: DeepseekAuth,
    AppTypes.muso: MusoAuth,
    AppTypes.vercel: VercelAuth,
    AppTypes.grafana: GrafanaAuth,
    AppTypes.flyio: FlyioAuth,
    AppTypes.gamma_ai: GammaAiAuth,
    AppTypes.thesys: ThesysAuth,
    AppTypes.remotion: RemotionAuth,
    AppTypes.tipalti: TipaltiAuth,
    AppTypes.datev: DatevAuth,
    AppTypes.datadog: DatadogAuth,
    AppTypes.ordway: OrdwayAuth,
    AppTypes.northflank: NorthflankAuth,
    AppTypes.sigma_computing: SigmaComputingAuth,
    AppTypes.extend_ai: ExtendAIAuth,
    AppTypes.reducto: ReductoAuth,
    AppTypes.daytona: DaytonaAuth,
    AppTypes.perplexity : PerplexityAuth,
    AppTypes.bluedot: BluedotAuth,
    AppTypes.smartsheet: SmartsheetAuth,
    AppTypes.fireflies: FirefliesAuth,
    AppTypes.avalara: AvalaraAuth,
    AppTypes.airflow: AirflowAuth,
    AppTypes.vertex_ai: VertexAIAuth,
    AppTypes.azure_openai: AzureOpenAIAuth,
    AppTypes.openrouter: OpenRouterAuth,
    AppTypes.retell: RetellAuth,
    AppTypes.autobots: AutobotsAuth,
    AppTypes.temporal: TemporalAuth,
    AppTypes.custom: CustomAuth,
}
