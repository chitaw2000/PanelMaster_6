# PanelMaster External Integration Spec

This document is for the external panel developer integrating with Master Panel.

## Base URL and Auth

- Base URL: `https://dash.datthabaluu.me`
- Required header on protected endpoints:
  - `Content-Type: application/json`
  - `x-api-key: <PANELMASTER_API_KEY>`
- Do not hardcode API key in source code. Use environment variable.

Example:

```js
const headers = {
  "Content-Type": "application/json",
  "x-api-key": process.env.PANELMASTER_API_KEY
};
```

## Endpoint Compatibility

Current payload formats are backward-compatible. Existing integration can continue with the same payload structures.

### 1) Get active groups

- Method: `GET`
- Path: `/api/active-groups`
- Auth: required
- Response:

```json
{
  "success": true,
  "groups": [
    { "id": "g1", "name": "Group One", "serverCount": 2 }
  ]
}
```

### 2) Generate keys

- Method: `POST`
- Path: `/api/generate-keys`
- Auth: required
- Request:

```json
{
  "masterGroupId": "group_id",
  "userName": "username",
  "totalGB": 50,
  "expireDate": "2026-04-30"
}
```

- Response includes `keys` and `token`:

```json
{
  "success": true,
  "keys": {
    "node1": {
      "server": "1.2.3.4",
      "server_port": 10001,
      "password": "uuid",
      "method": "chacha20-ietf-poly1305",
      "prefix": "..."
    }
  },
  "token": "user_token"
}
```

### 3) Switch active server

- Method: `POST`
- Path: `/api/webhook/switch`
- Auth: required
- Request:

```json
{
  "token": "user_token",
  "activeServer": "NodeNameOrNodeId"
}
```

### 4) User action

- Method: `POST`
- Path: `/api/user-action`
- Auth: required
- Request:

```json
{
  "token": "user_token",
  "action": "suspend"
}
```

- Supported actions:
  - Suspend aliases: `suspend`, `block`, `blocked`, `pause`
  - Resume aliases: `resume`, `unblock`, `unblocked`, `unpause`
  - Delete: `delete`

### 5) Internal edit user

- Method: `POST`
- Path: `/api/internal/edit-user`
- Auth: required
- Request:

```json
{
  "username": "user1",
  "totalGB": 100,
  "usedGB": 12.5,
  "expireDate": "2026-05-01"
}
```

### 6) Internal block user

- Method: `POST`
- Path: `/api/internal/block-user`
- Auth: required
- Request:

```json
{
  "username": "user1"
}
```

### 7) Internal delete user

- Method: `POST`
- Path: `/api/internal/delete-user`
- Auth: required
- Request by username:

```json
{
  "username": "user1"
}
```

- Or request by token:

```json
{
  "token": "user_token"
}
```

## Expected HTTP Status Handling

Client should handle:

- `200`: success
- `400`: invalid payload
- `401`: unauthorized (invalid/revoked API key)
- `404`: user or group not found
- `500`: server-side error

On `401`, stop retries and alert operator to rotate/fix key.

## Master-to-External Usage Sync

Master panel sends usage sync to external panel via:

- `/api/internal/sync-user-usage`
- fallback: `/admin/api/internal/sync-user-usage`

External panel should keep either first route or both routes available.

## API Key Rotation and Revoke Notes

Master now supports per-client API keys and revocation.

When key is changed/revoked:

1. Update external panel env value for `PANELMASTER_API_KEY`
2. Restart external panel process
3. Verify with a protected test call (`/api/active-groups`)

## Cloudflare/WAF Note

If Cloudflare WAF is enabled, make sure API routes (`/api/*`) with custom header `x-api-key` are not blocked or challenged.
