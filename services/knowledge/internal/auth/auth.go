package auth

import (
	"context"
	"crypto/rsa"
	"crypto/x509"
	"encoding/pem"
	"errors"
	"net/http"
	"strings"

	"github.com/golang-jwt/jwt/v5"
	"info-agent/knowledge/internal/apperror"
	"info-agent/knowledge/internal/config"
)

type contextKey string

const principalKey contextKey = "knowledge-principal"

type Principal struct {
	UserID         string
	OrganizationID string
	Claims         jwt.MapClaims
}

type Validator struct {
	cfg       config.Config
	publicKey *rsa.PublicKey
}

func NewValidator(cfg config.Config) (*Validator, error) {
	v := &Validator{cfg: cfg}
	if strings.TrimSpace(cfg.JWTPublicKey) != "" {
		block, _ := pem.Decode([]byte(strings.ReplaceAll(cfg.JWTPublicKey, `\n`, "\n")))
		if block == nil {
			return nil, errors.New("invalid jwt public key PEM")
		}
		parsed, err := x509.ParsePKIXPublicKey(block.Bytes)
		if err != nil {
			if cert, certErr := x509.ParseCertificate(block.Bytes); certErr == nil {
				parsed = cert.PublicKey
			} else {
				return nil, err
			}
		}
		key, ok := parsed.(*rsa.PublicKey)
		if !ok {
			return nil, errors.New("jwt public key must be RSA")
		}
		v.publicKey = key
	}
	return v, nil
}

func (v *Validator) Middleware() func(*http.Request) (*Principal, *apperror.Error) {
	return func(req *http.Request) (*Principal, *apperror.Error) {
		devPrincipal, devOK := v.devPrincipal(req)
		value := strings.TrimSpace(req.Header.Get("Authorization"))
		if value == "" {
			if devOK {
				return devPrincipal, nil
			}
			return nil, apperror.Clone(apperror.ErrUnauthorized)
		}
		if !strings.HasPrefix(strings.ToLower(value), "bearer ") {
			if devOK {
				return devPrincipal, nil
			}
			return nil, apperror.Clone(apperror.ErrUnauthorized)
		}
		tokenString := strings.TrimSpace(value[len("Bearer "):])
		if tokenString == "" {
			if devOK {
				return devPrincipal, nil
			}
			return nil, apperror.Clone(apperror.ErrUnauthorized)
		}
		keyFunc := func(token *jwt.Token) (any, error) {
			method := token.Method.Alg()
			if strings.HasPrefix(method, "RS") {
				if v.publicKey == nil {
					return nil, errors.New("jwt RSA key is not configured")
				}
				return v.publicKey, nil
			}
			if strings.HasPrefix(method, "HS") && v.cfg.JWTSecret != "" {
				return []byte(v.cfg.JWTSecret), nil
			}
			return nil, errors.New("jwt algorithm is not allowed")
		}
		options := []jwt.ParserOption{jwt.WithValidMethods([]string{"RS256", "RS384", "RS512", "HS256", "HS384", "HS512"})}
		if v.cfg.JWTIssuer != "" {
			options = append(options, jwt.WithIssuer(v.cfg.JWTIssuer))
		}
		if v.cfg.JWTAudience != "" {
			options = append(options, jwt.WithAudience(v.cfg.JWTAudience))
		}
		token, err := jwt.Parse(tokenString, keyFunc, options...)
		if err != nil || !token.Valid {
			if devOK {
				return devPrincipal, nil
			}
			return nil, apperror.Clone(apperror.ErrUnauthorized)
		}
		claims, ok := token.Claims.(jwt.MapClaims)
		if !ok {
			if devOK {
				return devPrincipal, nil
			}
			return nil, apperror.Clone(apperror.ErrUnauthorized)
		}
		userID := stringClaim(claims, "sub")
		if userID == "" {
			if devOK {
				return devPrincipal, nil
			}
			return nil, apperror.Clone(apperror.ErrUnauthorized)
		}
		return &Principal{UserID: userID, OrganizationID: firstClaim(claims, "organization_id", "org_id"), Claims: claims}, nil
	}
}

func (v *Validator) devPrincipal(req *http.Request) (*Principal, bool) {
	if !v.cfg.AllowDevAuth {
		return nil, false
	}
	userID := strings.TrimSpace(req.Header.Get("X-User-ID"))
	if userID == "" {
		userID = v.cfg.DevUserID
	}
	org := strings.TrimSpace(req.Header.Get("X-Organization-ID"))
	if org == "" {
		org = v.cfg.DevOrganizationID
	}
	return &Principal{UserID: userID, OrganizationID: org}, true
}

func WithPrincipal(ctx context.Context, principal *Principal) context.Context {
	return context.WithValue(ctx, principalKey, principal)
}
func PrincipalFromContext(ctx context.Context) (*Principal, bool) {
	principal, ok := ctx.Value(principalKey).(*Principal)
	return principal, ok
}
func stringClaim(claims jwt.MapClaims, key string) string {
	if value, ok := claims[key].(string); ok {
		return strings.TrimSpace(value)
	}
	return ""
}
func firstClaim(claims jwt.MapClaims, keys ...string) string {
	for _, key := range keys {
		if value := stringClaim(claims, key); value != "" {
			return value
		}
	}
	return ""
}
