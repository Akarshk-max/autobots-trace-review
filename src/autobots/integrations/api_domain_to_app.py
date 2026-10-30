from src.autobots.integrations.app_types import AppTypes

API_DOMAIN_TO_APP = {
    # slack
    "slack.com": AppTypes.slack.value,
    # google
    "googleapis.com": AppTypes.google.value,
    # jira
    "atlassian.net": AppTypes.jira.value,
    # zendesk
    "zendesk.com": AppTypes.zendesk.value,
    # zoho
    "zohoapis.ca": AppTypes.zoho.value,
    "zohoapis.eu": AppTypes.zoho.value,
    "zohoapis.com.cn": AppTypes.zoho.value,
    "zohoapis.com": AppTypes.zoho.value,
    "zohoapis.com.au": AppTypes.zoho.value,
    "zohoapis.in": AppTypes.zoho.value,
    "zohoapis.jp": AppTypes.zoho.value,
    "notion.com": AppTypes.notion.value,
    "plaid.com": AppTypes.plaid.value,
    # vercel
    "vercel.com": AppTypes.vercel.value,
    # datev
    "datev.de": AppTypes.datev.value,
    "api.datev.de": AppTypes.datev.value,
    "sandbox-api.datev.de": AppTypes.datev.value,
    "accounting-documents.api.datev.de": AppTypes.datev.value,
    "accounting-clients.api.datev.de": AppTypes.datev.value,
    "accounting-dataexchange.api.datev.de": AppTypes.datev.value,
    "accounting-data-exchange.api.datev.de": AppTypes.datev.value,
    "hr-exchange.api.datev.de": AppTypes.datev.value,
    # smartsheet
    "api.smartsheet.com": AppTypes.smartsheet.value,
    # fireflies
    "api.fireflies.ai": AppTypes.fireflies.value,
    # bluedot
    "config.bluedot.io": AppTypes.bluedot.value,
    # avalara
    "rest.avatax.com": AppTypes.avalara.value,
}
