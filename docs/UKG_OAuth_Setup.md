# UKG Authentication Setup Guide

This guide explains how to set up authentication for UKG (Ultimate Kronos Group) integration.

## Overview

UKG supports two authentication methods:

1. **Client Credentials Flow** (RECOMMENDED) - Simpler, machine-to-machine authentication
   - ✅ No user authorization required
   - ✅ Direct token generation
   - ✅ Easier to set up
   - 🎯 Use this for most API access

2. **Authorization Code Flow** (OPTIONAL) - For user-specific permissions
   - Requires user to log in and authorize
   - Only needed for specific user-delegated scopes
   - More complex setup

**Most users should use Client Credentials Flow.**

## Prerequisites

Before setting up the integration, you need to obtain credentials from your UKG environment:

1. **Client ID** - Your UKG OAuth application client ID
2. **Client Secret** - Your UKG OAuth application client secret
3. **Base URL** - Your customer-specific UKG URL (e.g., `https://customer.ultipro.com`)

## Obtaining UKG Credentials

### For UKG Pro HCM:

1. Log in to your UKG Pro account
2. Navigate to: **System Configuration** > **Security** > **Developer Console**
3. On the Developer Console page, note the following:
   - **Organization ID** (e.g., `org_c5DJ9CCKN4WUuP98`)
   - **Generate Token URL** (your base URL)
   - **Audience** (e.g., `api.ukg.net`)
4. Copy your:
   - **Client ID**
   - **Client Secret**

### For UKG Pro WFM:

1. Log in to your UKG Pro account
2. Navigate to: **Administration** > **Developer Console**
3. Follow the same steps as above to retrieve your credentials

### For UKG Ready:

1. Sign in to your UKG Ready account (e.g., `https://secure.us.rdy.ukg/ta/company.login`)
2. Navigate to: **Settings** > **Global Setup** > **Company Setup**
3. Locate the **OAuth Applications** widget
4. Create a new OAuth application:
   - Choose the **Machine to Machine** type
   - Generate a **Client ID** and **Client Secret**

## System Configuration

As a system administrator, you need to configure the following environment variables:

### Required Settings

Add these to your `.env` file or environment:

```bash
# UKG OAuth Settings
UKG_REDIRECT_URL=https://yourdomain.com/v1/auth/ukg/oauth-callback
UKG_SCOPES="openid profile email"
```

### Optional Settings

The following settings are NOT required in the system settings because they are user-specific and stored in app_creds:

- ~~`UKG_CLIENT_ID`~~ - This is user-specific, stored in app_creds
- ~~`UKG_CLIENT_SECRET`~~ - This is user-specific, stored in app_creds

## Authentication Methods

### Method 1: Client Credentials Flow (RECOMMENDED)

This is the simpler method that works for most use cases.

#### Step 1: Create App Credentials

Each user must first create their app credentials with their UKG-specific information:

**Endpoint:** `POST /v1/app-creds`

**Request Body:**
```json
{
  "app_name": "ukg",
  "creds": {
    "client_id": "your_ukg_client_id",
    "client_secret": "your_ukg_client_secret",
    "base_url": "https://your-company.ultipro.com"
  }
}
```

**Required Fields:**
- `client_id` - Your UKG OAuth client ID
- `client_secret` - Your UKG OAuth client secret
- `base_url` - Your customer-specific UKG base URL

#### Step 2: Create App Auth

The system automatically gets a token using client credentials:

**Endpoint:** `POST /v1/app-auth`

**Request Body:**
```json
{
  "app_name": "ukg"
}
```

**What happens:**
1. System reads your app_creds (client_id, client_secret, base_url)
2. Automatically calls UKG token endpoint with `grant_type=client_credentials`
3. Stores the access token in app_auth
4. You're ready to make API calls!

**That's it!** No OAuth URLs, no redirects, no user authorization needed.

---

### Method 2: Authorization Code Flow (OPTIONAL)

Only use this if you need user-specific authorization. Most users should skip this.

#### Step 1: Create App Credentials

Same as Method 1 above.

#### Step 2: Get OAuth URL

After creating app credentials, get the OAuth authorization URL:

**Endpoint:** `GET /v1/auth/ukg/oauth-url`

**Headers:**
```
Authorization: Bearer <user_access_token>
```

**Response:**
```json
{
  "url": "https://your-company.ultipro.com/identity/connect/authorize?response_type=code&client_id=...&redirect_uri=...&scope=..."
}
```

#### Step 3: User Authorization

1. Redirect the user to the OAuth URL from Step 2
2. User logs in to their UKG account and grants permissions
3. UKG redirects back to the callback URL with an authorization code

#### Step 4: OAuth Callback (Automatic)

The system automatically handles the callback at:

**Endpoint:** `GET /v1/auth/ukg/oauth-callback`

**Query Parameters:**
- `code` - Authorization code from UKG
- `state` - State parameter (optional)
- `error` - Error message if authorization failed

This endpoint will:
1. Exchange the authorization code for an access token
2. Store the OAuth tokens in the user's app_auth
3. Redirect the user to the application homepage

## Token Management

### Client Credentials Flow

- Access tokens expire after 1 hour (3600 seconds)
- System automatically gets a new token when expired (no refresh token needed)
- Just re-authenticates with client_id + client_secret

### Authorization Code Flow

- Access tokens expire after 1 hour (3600 seconds)
- Uses refresh tokens to get new access tokens
- Falls back to client credentials if refresh fails

## API Usage

Once authentication is set up (either method), the system automatically:

1. Injects the correct authorization header: `Authorization: Bearer <access_token>`
2. Updates URLs to use the correct UKG base URL
3. Refreshes/renews tokens when needed

The authentication is transparent - you just make API calls and the system handles auth.

## Which Method Should I Use?

### Use Client Credentials Flow if:
- ✅ You need API access for automation/integration
- ✅ You have a service account or API credentials
- ✅ You want simple setup
- ✅ You don't need user-specific permissions

### Use Authorization Code Flow if:
- 🔒 UKG requires user consent for specific scopes
- 👤 You need access to user-specific data that requires user authorization
- 📋 UKG documentation specifically requires this flow for your use case

**90% of integrations should use Client Credentials Flow.**

## Code Structure

### Files Modified/Created

1. **`src/autobots/integrations/integration_type/ukg_auth.py`**
   - UKG authentication handler
   - Defines `UKGCreds` model with user-specific credentials
   - Implements both client credentials and authorization code flows
   - Automatic token creation, refresh, and validation

2. **`src/autobots/conn/only_auth/ukg/ukg_client.py`**
   - UKG OAuth client implementation
   - Supports client credentials flow (simple, recommended)
   - Supports authorization code flow (optional)
   - Manages all token requests

3. **`src/autobots/api/v1/api_auth/api_ukg_auth.py`** (OPTIONAL)
   - OAuth URL generation endpoint
   - OAuth callback handler
   - Only needed for authorization code flow

4. **`src/autobots/integrations/app_type_to_integration_class.py`**
   - Registered UKG auth handler

5. **`src/autobots/api/v1/v1.py`**
   - Registered UKG OAuth routes

## Security Considerations

### Client Credentials

- **NEVER** expose client_id and client_secret in public repositories
- Store credentials securely in the database
- Each user has their own separate credentials

### Access Tokens

- Access tokens are short-lived (1 hour)
- Automatically refreshed by the system
- Stored securely in MongoDB

### HTTPS Required

- All OAuth flows must use HTTPS
- Redirect URLs must use HTTPS in production

## Troubleshooting

### "UKG credentials not found" Error

**Problem:** User hasn't created app_creds yet

**Solution:** User must first create app_creds with their UKG client_id, client_secret, and base_url

### "UKG base_url is required" Error

**Problem:** base_url not provided in app_creds

**Solution:** Ensure the app_creds includes the customer-specific base_url

### "Unable to refresh token" Error

**Problem:** Refresh token is invalid or expired

**Solution:** User needs to re-authorize through the OAuth flow

### Invalid Client Error

**Problem:** client_id or client_secret is incorrect

**Solution:** Verify credentials in UKG Developer Console

## Testing

### Test Client Credentials Flow (Recommended)

1. Create app_creds with your UKG credentials
2. Create app_auth (POST /v1/app-auth with app_name="ukg")
3. Verify token is automatically created
4. Make test API calls
5. Wait for token to expire and verify auto-renewal

### Test Authorization Code Flow (Optional)

1. Create app credentials with sandbox UKG environment
2. Generate OAuth URL
3. Complete authorization flow
4. Verify tokens are stored correctly
5. Test API calls with the stored tokens

### Test Token Refresh/Renewal

1. Wait for token to near expiration (or manually set created_at)
2. Make an API call
3. Verify token is automatically refreshed/renewed

## References

- [UKG Pro Platform Developer Documentation](https://developer.ukg.com/proplatform/docs)
- [UKG OAuth Authentication Flow](https://developer.ukg.com/hrsd/docs/ukg-hr-service-delivery-api-oauth-authentication-flow)
- [UKG Ready Integration Guide](https://support.allvoices.co/hc/en-us/articles/40001564986907-UKG-Ready-Integration)

## Support

For issues or questions:
1. Check UKG Developer Console for credential accuracy
2. Review logs for detailed error messages
3. Verify base_url format matches your UKG environment
4. Ensure all required credentials are provided in app_creds
