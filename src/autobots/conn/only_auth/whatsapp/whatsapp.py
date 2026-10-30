# WhatsApp Business API uses the same Facebook OAuth as Meta
# This file exists for consistency but delegates to Meta connection class

from src.autobots.conn.only_auth.meta.meta import (
    Meta,
    MetaAccessToken,
    MetaAccessTokenRq,
    MetaRefreshedAccessToken,
    MetaRefreshTokenRq,
)

# Alias Meta classes for WhatsApp compatibility
WhatsAppAuthorizationCodeRq = None  # Not needed since we use Meta
WhatsAppAuthorizationCode = None  # Not needed since we use Meta
WhatsAppRefreshTokenRq = MetaRefreshTokenRq
WhatsAppAccessTokenRq = MetaAccessTokenRq
WhatsAppRefreshedAccessToken = MetaRefreshedAccessToken
WhatsAppAccessToken = MetaAccessToken


class WhatsApp(Meta):
    """
    WhatsApp Business API connection class.
    Uses the same Facebook OAuth endpoints as Meta since WhatsApp is part of Facebook.
    """

    pass
