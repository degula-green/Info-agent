# Core Service

Core owns IAM authentication and authorization. The authentication code is split into:

```text
internal/domain          authentication entities and rules
internal/application     login, refresh, logout, and access-token verification
internal/repository      persistence interfaces
internal/infrastructure  PostgreSQL, Redis, bcrypt, and RS256 implementations
internal/httpapi         Gin handlers, optional middleware, and Principal context helpers
internal/server          dependency wiring
```

`domain` and `application` do not depend on Gin, PostgreSQL, or Redis.

## Authentication endpoints

The current Core routes are:

```text
POST /auth/login
POST /auth/refresh
POST /auth/logout
GET  /internal/auth/verify
```

Through the current gateway prefix, the public auth routes are under `/api/core/auth/*`.
The verify route is reserved for a future Nginx `auth_request` integration. That gateway integration and protection of existing business routes are intentionally not enabled yet.

Login returns an RS256 access token in the JSON body and writes the opaque refresh token to a Secure, HttpOnly cookie. Refresh rotates that cookie. Logout revokes the current refresh session but does not revoke an already issued access token.

Successful verification returns the trusted `X-Actor-ID` and `X-Auth-Session-ID` response headers. A future gateway must remove client-supplied copies of these headers before forwarding the values returned by Core.

## Runtime requirements

Copy the settings from `.env.example` into the deployment environment. PostgreSQL and Redis must be reachable when Core starts. The RSA private/public key pair must be mounted as files and referenced by `CORE_JWT_PRIVATE_KEY_FILE` and `CORE_JWT_PUBLIC_KEY_FILE`; key material must not be committed to the repository.

The default refresh cookie path assumes requests go through `/api/core`. For direct HTTP development against Core, override `CORE_REFRESH_COOKIE_PATH=/auth`. Only set `CORE_REFRESH_COOKIE_SECURE=false` for local plain-HTTP development.

Run `go test ./...` for unit tests. Set `CORE_TEST_DATABASE_URL` to a migrated disposable PostgreSQL database and/or `CORE_TEST_REDIS_URL` to a disposable Redis database to include the real-adapter integration tests.
