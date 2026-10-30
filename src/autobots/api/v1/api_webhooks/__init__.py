from fastapi import APIRouter

from src.autobots.api.v1.api_webhooks import api_webhook_siri, api_webhook_slack, api_webhooks

router = APIRouter()

router.include_router(api_webhooks.router)
router.include_router(api_webhook_slack.router)
router.include_router(api_webhook_siri.router)
