from fastapi import APIRouter

from src.autobots.api.v1 import api_webhooks, v1_api_docs
from src.autobots.api.v1.api_access import api_access
from src.autobots.api.v1.api_action import api_action
from src.autobots.api.v1.api_action_graph import api_action_graphs
from src.autobots.api.v1.api_action_graph_result import api_action_graph_results
from src.autobots.api.v1.api_action_result import api_action_results
from src.autobots.api.v1.api_action_team import api_action_team_results, api_action_teams
from src.autobots.api.v1.api_app_creds import api_app_auth, api_app_creds
from src.autobots.api.v1.api_auth import (
    api_adp_auth,
    api_asana_auth,
    api_auth,
    api_auth_intuit,
    api_box_auth,
    api_datev_auth,
    api_dropbox_auth,
    api_github_auth,
    api_google_ads_auth,
    api_google_analytics_auth,
    api_google_auth,
    api_google_cloud_auth,
    api_google_drive_auth,
    api_google_gmail_auth,
    api_google_read_only_auth,
    api_hubspot_auth,
    api_icims_auth,
    api_looker_auth,
    api_meta_ads_read_only_auth,
    api_meta_auth,
    api_netsuite_auth,
    api_outlook_auth,
    api_plaid_auth,
    api_salesforce_auth,
    api_slack_auth,
    api_smartsheet_auth,
    api_spotify_auth,
    api_ukg_auth,
    api_whatsapp_auth,
    api_workday_auth,
    api_youtube_auth,
    api_zoom_auth,
)
from src.autobots.api.v1.api_batch import api_batches
from src.autobots.api.v1.api_characters import api_characters
from src.autobots.api.v1.api_datastore import api_datastore
from src.autobots.api.v1.api_daytona.api_daytona import router as daytona_router
from src.autobots.api.v1.api_db_auth import api_db_auth
from src.autobots.api.v1.api_event_results import api_event_results
from src.autobots.api.v1.api_files import api_files
from src.autobots.api.v1.api_hello import api_hello
from src.autobots.api.v1.api_ingestion import api_ingestion
from src.autobots.api.v1.api_integrations import api_integrations
from src.autobots.api.v1.api_mcp import api_mcp
from src.autobots.api.v1.api_openrouter import api_openrouter_models
from src.autobots.api.v1.api_pod import api_pod
from src.autobots.api.v1.api_run_metrics import api_run_metrics
from src.autobots.api.v1.api_schedules import api_schedules
from src.autobots.api.v1.api_server_sent_events import api_server_sent_events
from src.autobots.api.v1.api_session import api_sessions
from src.autobots.api.v1.api_share_data import api_share_data
from src.autobots.api.v1.api_skills import api_skills
from src.autobots.api.v1.api_space import api_space, api_space_invitation
from src.autobots.api.v1.api_stripe import api_stripe
from src.autobots.api.v1.api_terminal import api_terminal
from src.autobots.api.v1.api_token_usage import api_token_usage
from src.autobots.api.v1.api_tools import api_tools
from src.autobots.api.v1.api_user import api_user
from src.autobots.api.v1.api_vectorstore import api_vectorstore
from src.autobots.core.settings import SettingsProvider

# API v1 router
router = APIRouter(prefix=SettingsProvider.sget().API_v1)

router.include_router(api_hello.router)
router.include_router(api_auth.router)
router.include_router(api_action.router)
router.include_router(api_tools.router)
# router.include_router(api_action_type.router)
router.include_router(api_action_results.router)
router.include_router(api_token_usage.router)
router.include_router(api_run_metrics.router)
router.include_router(api_action_graphs.router)
router.include_router(api_action_graph_results.router)
router.include_router(api_action_teams.router)
router.include_router(api_action_team_results.router)
router.include_router(api_event_results.router)
router.include_router(api_sessions.router)

router.include_router(api_schedules.router)
router.include_router(api_webhooks.router)

router.include_router(api_skills.router)
router.include_router(api_batches.router)
router.include_router(api_pod.router)
router.include_router(api_characters.router)
# router.include_router(api_prompts.router)

# router.include_router(api_teams.router)
# router.include_router(fetch_stable_diffusion.router)
# router.include_router(api_action_chat.router)

# router.include_router(api_actions_market.router)

router.include_router(api_files.router)
router.include_router(api_datastore.router)
router.include_router(api_ingestion.router)
router.include_router(api_openrouter_models.router)
router.include_router(api_vectorstore.router)
# router.include_router(api_openai_storage.router)
# router.include_router(api_agents.router)

router.include_router(api_server_sent_events.router)
router.include_router(api_share_data.router)
router.include_router(api_terminal.router)
router.include_router(daytona_router)

router.include_router(api_space.router)
router.include_router(api_space_invitation.router)
router.include_router(api_space_invitation.invitations_router)

router.include_router(api_mcp.router)
router.include_router(api_db_auth.router)
router.include_router(api_user.router)
router.include_router(api_access.router)
router.include_router(api_stripe.router)
# auth routers
router.include_router(api_integrations.router)
router.include_router(api_app_creds.router)
router.include_router(api_app_auth.router)
router.include_router(api_auth_intuit.router)
router.include_router(api_plaid_auth.router)
router.include_router(api_google_auth.router)
router.include_router(api_google_read_only_auth.router)
router.include_router(api_looker_auth.router)
router.include_router(api_salesforce_auth.router)
router.include_router(api_outlook_auth.router)
router.include_router(api_box_auth.router)
router.include_router(api_slack_auth.router)
router.include_router(api_whatsapp_auth.router)
router.include_router(api_zoom_auth.router)
router.include_router(api_dropbox_auth.router)
router.include_router(api_google_ads_auth.router)
router.include_router(api_google_drive_auth.router)
router.include_router(api_google_gmail_auth.router)
router.include_router(api_google_analytics_auth.router)
router.include_router(api_youtube_auth.router)
router.include_router(api_google_cloud_auth.router)
router.include_router(api_meta_auth.router)
router.include_router(api_meta_ads_read_only_auth.router)
router.include_router(api_hubspot_auth.router)
router.include_router(api_github_auth.router)
router.include_router(api_adp_auth.router)
router.include_router(api_asana_auth.router)
router.include_router(api_spotify_auth.router)
router.include_router(api_netsuite_auth.router)
router.include_router(api_ukg_auth.router)
router.include_router(api_icims_auth.router)
router.include_router(api_workday_auth.router)
router.include_router(api_datev_auth.router)
router.include_router(api_smartsheet_auth.router)

# api docs router
# router_docs = APIRouter(prefix=SettingsProvider.sget().API_v1)
router.include_router(v1_api_docs.router, prefix="", tags=["docs"])
