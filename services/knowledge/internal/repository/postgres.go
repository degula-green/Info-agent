package repository

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"log"
	"sort"
	"strings"
	"time"

	"github.com/google/uuid"
	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgxpool"
	"info-agent/knowledge/internal/apperror"
	"info-agent/knowledge/internal/domain"
	"info-agent/knowledge/internal/privacy"
	"info-agent/knowledge/internal/trace"
)

type PostgresStore struct {
	pool      *pgxpool.Pool
	ephemeral *MemoryStore
}

func NewPostgresStore(ctx context.Context, databaseURL string) (*PostgresStore, error) {
	pool, err := pgxpool.New(ctx, databaseURL)
	if err != nil {
		return nil, err
	}
	if err := pool.Ping(ctx); err != nil {
		pool.Close()
		return nil, err
	}
	return &PostgresStore{pool: pool, ephemeral: NewMemoryStore()}, nil
}

func (s *PostgresStore) Close() error { s.pool.Close(); return nil }

func uniqueSorted(values []string) []string {
	seen := make(map[string]struct{}, len(values))
	out := make([]string, 0, len(values))
	for _, value := range values {
		value = strings.TrimSpace(value)
		if value != "" {
			if _, ok := seen[value]; !ok {
				seen[value] = struct{}{}
				out = append(out, value)
			}
		}
	}
	sort.Strings(out)
	return out
}

func shareFingerprint(conversationID string, messageIDs, attachmentIDs []string) string {
	raw, _ := json.Marshal(struct {
		ConversationID string   `json:"private_conversation_id"`
		MessageIDs     []string `json:"message_ids"`
		AttachmentIDs  []string `json:"attachment_ids"`
	}{conversationID, messageIDs, attachmentIDs})
	sum := sha256.Sum256(raw)
	return hex.EncodeToString(sum[:])
}

const connectorColumns = `id::text, owner_user_id::text, platform, platform_workspace_key, external_account_id,
COALESCE(display_name,''), credential_ref, COALESCE(database_ref,''), token_expires_at, COALESCE(default_organization_id::text,''),
status, COALESCE(last_error,''), created_at, updated_at`

type rowScanner interface{ Scan(dest ...any) error }

func scanConnector(row rowScanner) (*domain.ConnectorAccount, error) {
	var a domain.ConnectorAccount
	var expires *time.Time
	err := row.Scan(&a.ID, &a.OwnerUserID, &a.Platform, &a.WorkspaceKey, &a.ExternalAccountID, &a.DisplayName, &a.CredentialRef, &a.DatabaseRef, &expires, &a.DefaultOrganizationID, &a.Status, &a.LastError, &a.CreatedAt, &a.UpdatedAt)
	if err != nil {
		return nil, err
	}
	if expires != nil {
		a.TokenExpiresAt = *expires
	}
	return &a, nil
}

func (s *PostgresStore) GetWechatConfig(ctx context.Context, connectorID string) (*domain.WechatCollectionConfig, error) {
	var c domain.WechatCollectionConfig
	var raw []byte
	err := s.pool.QueryRow(ctx, `SELECT connector_account_id::text,COALESCE(selected_conversations,'[]'::jsonb),history_start_at,enabled,listen_mode,updated_at FROM knowledge.wechat_collection_configs WHERE connector_account_id=$1`, connectorID).Scan(&c.ConnectorID, &raw, &c.HistoryStartAt, &c.Enabled, &c.ListenMode, &c.UpdatedAt)
	if errors.Is(err, pgx.ErrNoRows) {
		c = domain.WechatCollectionConfig{ConnectorID: connectorID, SelectedConversations: []string{}, Enabled: true, ListenMode: "whitelist"}
		return &c, nil
	}
	if err != nil {
		return nil, dbError(err)
	}
	if err := json.Unmarshal(raw, &c.SelectedConversations); err != nil {
		return nil, dbError(err)
	}
	return &c, nil
}
func (s *PostgresStore) SaveWechatConfig(ctx context.Context, c domain.WechatCollectionConfig) (*domain.WechatCollectionConfig, error) {
	if c.ListenMode == "" {
		c.ListenMode = "whitelist"
	}
	raw, err := json.Marshal(c.SelectedConversations)
	if err != nil {
		return nil, err
	}
	err = s.pool.QueryRow(ctx, `INSERT INTO knowledge.wechat_collection_configs (connector_account_id,selected_conversations,history_start_at,enabled,listen_mode) VALUES ($1,$2::jsonb,$3,$4,$5) ON CONFLICT (connector_account_id) DO UPDATE SET selected_conversations=EXCLUDED.selected_conversations,history_start_at=EXCLUDED.history_start_at,enabled=EXCLUDED.enabled,listen_mode=EXCLUDED.listen_mode,updated_at=now() RETURNING connector_account_id::text,selected_conversations,history_start_at,enabled,listen_mode,updated_at`, c.ConnectorID, raw, c.HistoryStartAt, c.Enabled, c.ListenMode).Scan(&c.ConnectorID, &raw, &c.HistoryStartAt, &c.Enabled, &c.ListenMode, &c.UpdatedAt)
	if err != nil {
		return nil, dbError(err)
	}
	_ = json.Unmarshal(raw, &c.SelectedConversations)
	return &c, nil
}
func (s *PostgresStore) GetWechatRuntime(ctx context.Context, id string) (*domain.WechatCollectorRuntime, error) {
	var r domain.WechatCollectorRuntime
	err := s.pool.QueryRow(ctx, `SELECT connector_account_id::text,status,last_heartbeat_at,last_collected_at,COALESCE(last_error,''),stopped_at,updated_at FROM knowledge.wechat_collector_runtime WHERE connector_account_id=$1`, id).Scan(&r.ConnectorID, &r.Status, &r.LastHeartbeatAt, &r.LastCollectedAt, &r.LastError, &r.StoppedAt, &r.UpdatedAt)
	if errors.Is(err, pgx.ErrNoRows) {
		return &domain.WechatCollectorRuntime{ConnectorID: id, Status: "stopped"}, nil
	}
	if err != nil {
		return nil, dbError(err)
	}
	return &r, nil
}
func (s *PostgresStore) UpsertWechatRuntime(ctx context.Context, r domain.WechatCollectorRuntime) (*domain.WechatCollectorRuntime, error) {
	err := s.pool.QueryRow(ctx, `INSERT INTO knowledge.wechat_collector_runtime (connector_account_id,status,last_heartbeat_at,last_collected_at,last_error,stopped_at) VALUES ($1,$2,$3,$4,$5,$6) ON CONFLICT (connector_account_id) DO UPDATE SET status=EXCLUDED.status,last_heartbeat_at=EXCLUDED.last_heartbeat_at,last_collected_at=EXCLUDED.last_collected_at,last_error=EXCLUDED.last_error,stopped_at=EXCLUDED.stopped_at,updated_at=now() RETURNING connector_account_id::text,status,last_heartbeat_at,last_collected_at,COALESCE(last_error,''),stopped_at,updated_at`, r.ConnectorID, r.Status, r.LastHeartbeatAt, r.LastCollectedAt, nilString(r.LastError), r.StoppedAt).Scan(&r.ConnectorID, &r.Status, &r.LastHeartbeatAt, &r.LastCollectedAt, &r.LastError, &r.StoppedAt, &r.UpdatedAt)
	return &r, dbError(err)
}
func (s *PostgresStore) UpdateWechatRuntime(ctx context.Context, id, status, lastError string, heartbeat, collectedAt *time.Time) error {
	_, err := s.pool.Exec(ctx, `UPDATE knowledge.wechat_collector_runtime SET status=COALESCE(NULLIF($2,''),status),last_error=$3,last_heartbeat_at=COALESCE($4,last_heartbeat_at),last_collected_at=COALESCE($5,last_collected_at),updated_at=now() WHERE connector_account_id=$1`, id, status, nilString(lastError), heartbeat, collectedAt)
	return dbError(err)
}

func (s *PostgresStore) ListConnectorViews(ctx context.Context, userID string) ([]domain.ConnectorView, error) {
	rows, err := s.pool.Query(ctx, `SELECT `+connectorColumns+` FROM knowledge.connector_accounts WHERE owner_user_id=$1 AND status<>'revoked'`, userID)
	if err != nil {
		return nil, dbError(err)
	}
	defer rows.Close()
	accounts := map[string]*domain.ConnectorAccount{}
	for rows.Next() {
		a, scanErr := scanConnector(rows)
		if scanErr != nil {
			return nil, dbError(scanErr)
		}
		accounts[a.Platform] = a
	}
	platforms := []struct {
		key, name string
		available bool
	}{{domain.PlatformFeishu, "飞书", true}, {domain.PlatformWechat, "个人微信", true}, {domain.PlatformWecom, "企业微信", false}}
	views := make([]domain.ConnectorView, 0, 3)
	for _, p := range platforms {
		view := domain.ConnectorView{Platform: p.key, DisplayName: p.name, Status: domain.ConnectorUnbound, Availability: "unavailable"}
		if p.available {
			view.Availability = "available"
		}
		if a := accounts[p.key]; a != nil {
			view.Bound = true
			view.Status = a.Status
			view.AccountName = a.DisplayName
			view.AccountID = a.ID
			view.DefaultOrganizationID = a.DefaultOrganizationID
			view.LastError = a.LastError
			if p.key == domain.PlatformWechat {
				var heartbeat *time.Time
				var status string
				err := s.pool.QueryRow(ctx, `SELECT status,last_heartbeat_at FROM knowledge.wechat_collector_runtime WHERE connector_account_id=$1`, a.ID).Scan(&status, &heartbeat)
				if err == nil {
					view.LastHeartbeatAt = heartbeat
					view.AgentOnline = status == "running" && heartbeat != nil && time.Since(*heartbeat) < 2*time.Minute
				}
			}
		}
		views = append(views, view)
	}
	return views, nil
}

func (s *PostgresStore) GetConnector(ctx context.Context, userID, platformName string) (*domain.ConnectorAccount, error) {
	a, err := scanConnector(s.pool.QueryRow(ctx, `SELECT `+connectorColumns+` FROM knowledge.connector_accounts WHERE owner_user_id=$1 AND platform=$2 AND status<>'revoked' ORDER BY updated_at DESC LIMIT 1`, userID, platformName))
	if errors.Is(err, pgx.ErrNoRows) {
		return nil, apperror.New("connector_not_found", "connector is not bound", 404, false)
	}
	return a, dbError(err)
}
func (s *PostgresStore) GetConnectorForOAuth(ctx context.Context, userID, platformName string) (*domain.ConnectorAccount, error) {
	a, err := scanConnector(s.pool.QueryRow(ctx, `SELECT `+connectorColumns+` FROM knowledge.connector_accounts WHERE owner_user_id=$1 AND platform=$2 ORDER BY updated_at DESC LIMIT 1`, userID, platformName))
	if errors.Is(err, pgx.ErrNoRows) {
		return nil, apperror.New("connector_not_found", "connector is not bound", 404, false)
	}
	return a, dbError(err)
}
func (s *PostgresStore) GetConnectorByID(ctx context.Context, id string) (*domain.ConnectorAccount, error) {
	a, err := scanConnector(s.pool.QueryRow(ctx, `SELECT `+connectorColumns+` FROM knowledge.connector_accounts WHERE id=$1`, id))
	if errors.Is(err, pgx.ErrNoRows) {
		return nil, apperror.New("connector_not_found", "connector is not bound", 404, false)
	}
	return a, dbError(err)
}
func (s *PostgresStore) ListConnectorAccounts(ctx context.Context, platformName string) ([]domain.ConnectorAccount, error) {
	query := `SELECT ` + connectorColumns + ` FROM knowledge.connector_accounts WHERE status IN ('active','error')`
	args := []any{}
	if platformName != "" {
		query += ` AND platform=$1`
		args = append(args, platformName)
	}
	rows, err := s.pool.Query(ctx, query, args...)
	if err != nil {
		return nil, dbError(err)
	}
	defer rows.Close()
	out := []domain.ConnectorAccount{}
	for rows.Next() {
		account, scanErr := scanConnector(rows)
		if scanErr != nil {
			return nil, dbError(scanErr)
		}
		out = append(out, *account)
	}
	return out, dbError(rows.Err())
}
func (s *PostgresStore) FindConnectorByExternal(ctx context.Context, platformName, workspace, externalID string) (*domain.ConnectorAccount, error) {
	a, err := scanConnector(s.pool.QueryRow(ctx, `SELECT `+connectorColumns+` FROM knowledge.connector_accounts WHERE platform=$1 AND platform_workspace_key=$2 AND external_account_id=$3 AND status<>'revoked' LIMIT 1`, platformName, workspace, externalID))
	if errors.Is(err, pgx.ErrNoRows) {
		return nil, apperror.New("connector_not_found", "connector is not bound", 404, false)
	}
	return a, dbError(err)
}

func (s *PostgresStore) SaveConnector(ctx context.Context, a domain.ConnectorAccount) (*domain.ConnectorAccount, error) {
	if a.ID == "" {
		a.ID = uuid.NewString()
	}
	if a.Status == "" {
		a.Status = domain.ConnectorActive
	}
	row := s.pool.QueryRow(ctx, `INSERT INTO knowledge.connector_accounts (id,owner_user_id,platform,platform_workspace_key,external_account_id,display_name,credential_ref,database_ref,token_expires_at,default_organization_id,status,last_error)
VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12)
ON CONFLICT (id) DO UPDATE SET platform_workspace_key=EXCLUDED.platform_workspace_key,external_account_id=EXCLUDED.external_account_id,display_name=EXCLUDED.display_name,credential_ref=EXCLUDED.credential_ref,database_ref=EXCLUDED.database_ref,token_expires_at=EXCLUDED.token_expires_at,default_organization_id=EXCLUDED.default_organization_id,status=EXCLUDED.status,last_error=EXCLUDED.last_error,updated_at=now()
RETURNING `+connectorColumns, a.ID, a.OwnerUserID, a.Platform, a.WorkspaceKey, a.ExternalAccountID, a.DisplayName, a.CredentialRef, nilString(a.DatabaseRef), nilTime(a.TokenExpiresAt), nilString(a.DefaultOrganizationID), a.Status, nilString(a.LastError))
	saved, err := scanConnector(row)
	if err != nil && isUnique(err) {
		return nil, apperror.New("connector_already_bound", "external account or platform is already bound", 409, false)
	}
	return saved, dbError(err)
}

func (s *PostgresStore) ReplaceConnector(ctx context.Context, previousConnectorID string, a domain.ConnectorAccount) (*domain.ConnectorAccount, error) {
	if a.ID == "" {
		a.ID = uuid.NewString()
	}
	if a.Status == "" {
		a.Status = domain.ConnectorActive
	}
	tx, err := s.pool.Begin(ctx)
	if err != nil {
		return nil, dbError(err)
	}
	defer tx.Rollback(ctx)
	var ownerUserID, platformName string
	err = tx.QueryRow(ctx, `UPDATE knowledge.connector_accounts SET status='revoked',credential_ref='',updated_at=now()
		WHERE id=$1 AND status<>'revoked' RETURNING owner_user_id::text,platform`, previousConnectorID).Scan(&ownerUserID, &platformName)
	if errors.Is(err, pgx.ErrNoRows) {
		return nil, apperror.New("connector_not_found", "connector is not bound", 404, false)
	}
	if err != nil {
		return nil, dbError(err)
	}
	if ownerUserID != a.OwnerUserID || platformName != a.Platform {
		return nil, apperror.Clone(apperror.ErrForbidden)
	}
	if _, err = tx.Exec(ctx, `UPDATE knowledge.wechat_collector_runtime SET status='stopped',stopped_at=now(),updated_at=now() WHERE connector_account_id=$1`, previousConnectorID); err != nil {
		return nil, dbError(err)
	}
	if _, err = tx.Exec(ctx, `UPDATE knowledge.conversation_collectors SET status='unavailable',last_error='connector_replaced',updated_at=now() WHERE connector_account_id=$1 AND status<>'removed'`, previousConnectorID); err != nil {
		return nil, dbError(err)
	}
	row := tx.QueryRow(ctx, `INSERT INTO knowledge.connector_accounts (id,owner_user_id,platform,platform_workspace_key,external_account_id,display_name,credential_ref,database_ref,token_expires_at,default_organization_id,status,last_error)
		VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12) RETURNING `+connectorColumns,
		a.ID, a.OwnerUserID, a.Platform, a.WorkspaceKey, a.ExternalAccountID, a.DisplayName, a.CredentialRef, nilString(a.DatabaseRef), nilTime(a.TokenExpiresAt), nilString(a.DefaultOrganizationID), a.Status, nilString(a.LastError))
	saved, err := scanConnector(row)
	if err != nil && isUnique(err) {
		return nil, apperror.New("connector_already_bound", "external account or platform is already bound", 409, false)
	}
	if err != nil {
		return nil, dbError(err)
	}
	if err := tx.Commit(ctx); err != nil {
		return nil, dbError(err)
	}
	return saved, nil
}

func (s *PostgresStore) BindConnector(ctx context.Context, previousConnectorID string, a domain.ConnectorAccount, identity ExternalIdentityInput, now time.Time) (*domain.ConnectorAccount, error) {
	if a.ID == "" {
		a.ID = uuid.NewString()
	}
	if a.Status == "" {
		a.Status = domain.ConnectorActive
	}
	tx, err := s.pool.Begin(ctx)
	if err != nil {
		return nil, dbError(err)
	}
	defer tx.Rollback(ctx)
	if err := bindExternalIdentityTx(ctx, tx, identity); err != nil {
		return nil, err
	}
	if previousConnectorID != "" {
		var ownerUserID, platformName string
		err = tx.QueryRow(ctx, `UPDATE knowledge.connector_accounts SET status='revoked',credential_ref='',updated_at=$2
			WHERE id=$1 AND status<>'revoked' RETURNING owner_user_id::text,platform`, previousConnectorID, now).Scan(&ownerUserID, &platformName)
		if errors.Is(err, pgx.ErrNoRows) {
			return nil, apperror.New("connector_not_found", "connector is not bound", 404, false)
		}
		if err != nil {
			return nil, dbError(err)
		}
		if ownerUserID != a.OwnerUserID || platformName != a.Platform {
			return nil, apperror.Clone(apperror.ErrForbidden)
		}
		if _, err = tx.Exec(ctx, `UPDATE knowledge.wechat_collector_runtime SET status='stopped',stopped_at=$2,updated_at=$2 WHERE connector_account_id=$1`, previousConnectorID, now); err != nil {
			return nil, dbError(err)
		}
		if _, err = tx.Exec(ctx, `UPDATE knowledge.conversation_collectors SET status='unavailable',last_error='connector_replaced',updated_at=$2 WHERE connector_account_id=$1 AND status<>'removed'`, previousConnectorID, now); err != nil {
			return nil, dbError(err)
		}
	}
	row := tx.QueryRow(ctx, `INSERT INTO knowledge.connector_accounts (id,owner_user_id,platform,platform_workspace_key,external_account_id,display_name,credential_ref,database_ref,token_expires_at,default_organization_id,status,last_error)
		VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12)
		ON CONFLICT (id) DO UPDATE SET platform_workspace_key=EXCLUDED.platform_workspace_key,external_account_id=EXCLUDED.external_account_id,display_name=EXCLUDED.display_name,credential_ref=EXCLUDED.credential_ref,database_ref=EXCLUDED.database_ref,token_expires_at=EXCLUDED.token_expires_at,default_organization_id=EXCLUDED.default_organization_id,status=EXCLUDED.status,last_error=EXCLUDED.last_error,updated_at=$13
		RETURNING `+connectorColumns,
		a.ID, a.OwnerUserID, a.Platform, a.WorkspaceKey, a.ExternalAccountID, a.DisplayName, a.CredentialRef, nilString(a.DatabaseRef), nilTime(a.TokenExpiresAt), nilString(a.DefaultOrganizationID), a.Status, nilString(a.LastError), now)
	saved, err := scanConnector(row)
	if err != nil && isUnique(err) {
		return nil, apperror.New("connector_already_bound", "external account or platform is already bound", 409, false)
	}
	if err != nil {
		return nil, dbError(err)
	}
	if saved.OwnerUserID != a.OwnerUserID || saved.Platform != a.Platform {
		return nil, apperror.Clone(apperror.ErrForbidden)
	}
	if _, err = tx.Exec(ctx, `UPDATE knowledge.conversation_collectors SET status='active',last_error=NULL,next_poll_at=NULL,updated_at=$2 WHERE connector_account_id=$1 AND status='unavailable' AND last_error IN ('authorization_expired','refresh_token_invalid','connector_revoked','connector_replaced')`, saved.ID, now); err != nil {
		return nil, dbError(err)
	}
	if err := tx.Commit(ctx); err != nil {
		return nil, dbError(err)
	}
	return saved, nil
}

func (s *PostgresStore) SetConnectorDefaultOrganization(ctx context.Context, connectorID, ownerUserID, organizationID string) (*domain.ConnectorAccount, error) {
	row := s.pool.QueryRow(ctx, `UPDATE knowledge.connector_accounts
		SET default_organization_id=$3::uuid,updated_at=now()
		WHERE id=$1::uuid AND owner_user_id=$2::uuid AND status<>'revoked'
		AND (default_organization_id IS NULL OR default_organization_id=$3::uuid)
		RETURNING `+connectorColumns, connectorID, ownerUserID, organizationID)
	account, err := scanConnector(row)
	if errors.Is(err, pgx.ErrNoRows) {
		return nil, apperror.Clone(apperror.ErrForbidden)
	}
	return account, dbError(err)
}

func (s *PostgresStore) UpdateConnectorStatus(ctx context.Context, id, status, lastError string) error {
	tag, err := s.pool.Exec(ctx, `UPDATE knowledge.connector_accounts SET status=$2,last_error=$3,updated_at=now() WHERE id=$1`, id, status, nilString(lastError))
	if err != nil {
		return dbError(err)
	}
	if tag.RowsAffected() == 0 {
		return apperror.New("connector_not_found", "connector is not bound", 404, false)
	}
	// Keep collectors available during transient connector errors. They are
	// paused only when authorization is expired or the connector is revoked.
	if status == domain.ConnectorExpired || status == domain.ConnectorRevoked {
		_, _ = s.pool.Exec(ctx, `UPDATE knowledge.conversation_collectors SET status='unavailable',last_error=$2,updated_at=now() WHERE connector_account_id=$1 AND status='active'`, id, nilString(lastError))
	}
	return nil
}

func (s *PostgresStore) RestoreAuthorizationCollectors(ctx context.Context, connectorID string, now time.Time) error {
	_, err := s.pool.Exec(ctx, `UPDATE knowledge.conversation_collectors cc SET connector_account_id=$1,status='active',last_error=NULL,next_poll_at=NULL,updated_at=$2 WHERE cc.connector_account_id IN (SELECT old.id FROM knowledge.connector_accounts old JOIN knowledge.connector_accounts current ON current.owner_user_id=old.owner_user_id AND current.platform=old.platform WHERE current.id=$1 AND old.status='revoked') AND cc.status='unavailable' AND cc.last_error IN ('authorization_expired','refresh_token_invalid','connector_revoked','connector_replaced')`, connectorID, now)
	if err != nil {
		return dbError(err)
	}
	_, err = s.pool.Exec(ctx, `UPDATE knowledge.conversation_collectors SET status='active',last_error=NULL,next_poll_at=NULL,updated_at=$2 WHERE connector_account_id=$1 AND status='unavailable' AND last_error IN ('authorization_expired','refresh_token_invalid','connector_revoked','connector_replaced')`, connectorID, now)
	return dbError(err)
}
func (s *PostgresStore) RevokeConnector(ctx context.Context, userID, platformName string) error {
	tx, err := s.pool.Begin(ctx)
	if err != nil {
		return dbError(err)
	}
	defer tx.Rollback(ctx)
	var id string
	err = tx.QueryRow(ctx, `UPDATE knowledge.connector_accounts SET status='revoked',credential_ref='',updated_at=now() WHERE owner_user_id=$1 AND platform=$2 AND status<>'revoked' RETURNING id::text`, userID, platformName).Scan(&id)
	if errors.Is(err, pgx.ErrNoRows) {
		return apperror.New("connector_not_found", "connector is not bound", 404, false)
	}
	if err != nil {
		return dbError(err)
	}
	_, err = tx.Exec(ctx, `UPDATE knowledge.wechat_collector_runtime SET status='stopped',stopped_at=now(),updated_at=now() WHERE connector_account_id=$1`, id)
	if err == nil {
		_, err = tx.Exec(ctx, `UPDATE knowledge.conversation_collectors SET status='unavailable',last_error='connector_revoked',updated_at=now() WHERE connector_account_id=$1 AND status<>'removed'`, id)
	}
	if err != nil {
		return dbError(err)
	}
	return dbError(tx.Commit(ctx))
}

func (s *PostgresStore) CreatePairing(ctx context.Context, p domain.Pairing) error {
	_, err := s.pool.Exec(ctx, `INSERT INTO knowledge.wechat_pairings (id,owner_user_id,code_hash,status,expires_at,wxid,default_organization_id,created_at) VALUES ($1,$2,$3,$4,$5,$6,$7,$8)`, p.ID, p.OwnerUserID, p.CodeHash, p.Status, p.ExpiresAt, nilString(p.WXID), nilString(p.DefaultOrganizationID), p.CreatedAt)
	return dbError(err)
}
func (s *PostgresStore) GetPairing(ctx context.Context, id string) (*domain.Pairing, error) {
	var p domain.Pairing
	err := s.pool.QueryRow(ctx, `SELECT id::text,owner_user_id::text,code_hash,status,expires_at,consumed_at,COALESCE(wxid,''),COALESCE(database_ref,''),COALESCE(default_organization_id::text,''),COALESCE(device_id::text,''),COALESCE(connector_id::text,''),COALESCE(failure_code,''),created_at FROM knowledge.wechat_pairings WHERE id=$1`, id).Scan(&p.ID, &p.OwnerUserID, &p.CodeHash, &p.Status, &p.ExpiresAt, &p.ConsumedAt, &p.WXID, &p.DatabaseRef, &p.DefaultOrganizationID, &p.DeviceID, &p.ConnectorID, &p.FailureCode, &p.CreatedAt)
	if errors.Is(err, pgx.ErrNoRows) {
		return nil, apperror.New("wechat_pairing_not_found", "pairing request not found", 404, false)
	}
	return &p, dbError(err)
}
func (s *PostgresStore) ConsumePairing(ctx context.Context, id, codeHash, wxid, databaseRef string, now time.Time) (*domain.Pairing, error) {
	var p domain.Pairing
	err := s.pool.QueryRow(ctx, `UPDATE knowledge.wechat_pairings SET status='consumed',consumed_at=$4,wxid=$5,database_ref=$6 WHERE id=$1 AND code_hash=$2 AND status='pending' AND expires_at>$3 AND (wxid IS NULL OR lower(wxid)=lower($5)) RETURNING id::text,owner_user_id::text,code_hash,status,expires_at,consumed_at,COALESCE(wxid,''),COALESCE(database_ref,''),COALESCE(default_organization_id::text,''),COALESCE(device_id::text,''),COALESCE(connector_id::text,''),COALESCE(failure_code,''),created_at`, id, codeHash, now, now, wxid, nilString(databaseRef)).Scan(&p.ID, &p.OwnerUserID, &p.CodeHash, &p.Status, &p.ExpiresAt, &p.ConsumedAt, &p.WXID, &p.DatabaseRef, &p.DefaultOrganizationID, &p.DeviceID, &p.ConnectorID, &p.FailureCode, &p.CreatedAt)
	if errors.Is(err, pgx.ErrNoRows) {
		return nil, apperror.New("wechat_pairing_expired", "pairing code is invalid or expired", 400, false)
	}
	return &p, dbError(err)
}
func (s *PostgresStore) FailPairing(ctx context.Context, id, codeHash, failureCode string, now time.Time) error {
	result, err := s.pool.Exec(ctx, `UPDATE knowledge.wechat_pairings SET status='failed',failure_code=$3 WHERE id=$1 AND code_hash=$2 AND status='pending' AND expires_at>$4`, id, codeHash, failureCode, now)
	if err != nil {
		return dbError(err)
	}
	if result.RowsAffected() == 0 {
		return apperror.New("wechat_pairing_expired", "pairing code is invalid or expired", 400, false)
	}
	return nil
}
func (s *PostgresStore) CreateDevice(ctx context.Context, d domain.AgentDevice) error {
	_, err := s.pool.Exec(ctx, `INSERT INTO knowledge.agent_devices (id,connector_id,owner_user_id,key_hash,expires_at,agent_version,created_at) VALUES ($1,$2,$3,$4,$5,$6,$7)`, d.ID, d.ConnectorID, d.OwnerUserID, d.KeyHash, d.ExpiresAt, nilString(d.AgentVersion), d.CreatedAt)
	return dbError(err)
}
func (s *PostgresStore) CompletePairing(ctx context.Context, pairingID, deviceID, connectorID string) error {
	_, err := s.pool.Exec(ctx, `UPDATE knowledge.wechat_pairings SET device_id=$2,connector_id=$3 WHERE id=$1`, pairingID, deviceID, connectorID)
	return dbError(err)
}

func (s *PostgresStore) CompleteAgentPairing(ctx context.Context, input AgentPairingInput) (*AgentPairingResult, error) {
	tx, err := s.pool.Begin(ctx)
	if err != nil {
		return nil, dbError(err)
	}
	defer tx.Rollback(ctx)
	now := input.Now.UTC()
	if now.IsZero() {
		now = time.Now().UTC()
	}
	var pairing domain.Pairing
	err = tx.QueryRow(ctx, `SELECT id::text,owner_user_id::text,code_hash,status,expires_at,consumed_at,COALESCE(wxid,''),COALESCE(database_ref,''),COALESCE(default_organization_id::text,''),COALESCE(device_id::text,''),COALESCE(connector_id::text,''),COALESCE(failure_code,''),created_at FROM knowledge.wechat_pairings WHERE id=$1 FOR UPDATE`, input.PairingID).Scan(&pairing.ID, &pairing.OwnerUserID, &pairing.CodeHash, &pairing.Status, &pairing.ExpiresAt, &pairing.ConsumedAt, &pairing.WXID, &pairing.DatabaseRef, &pairing.DefaultOrganizationID, &pairing.DeviceID, &pairing.ConnectorID, &pairing.FailureCode, &pairing.CreatedAt)
	if errors.Is(err, pgx.ErrNoRows) {
		return nil, apperror.New("wechat_pairing_not_found", "pairing request not found", 404, false)
	}
	if err != nil {
		return nil, dbError(err)
	}
	if pairing.Status != "pending" || pairing.ConsumedAt != nil || pairing.ExpiresAt.Before(now) || !constantTimeEqual(pairing.CodeHash, input.CodeHash) || (pairing.WXID != "" && !strings.EqualFold(pairing.WXID, input.WXID)) {
		return nil, apperror.New("wechat_pairing_expired", "pairing code is invalid or expired", 400, false)
	}
	identity := ExternalIdentityInput{Platform: domain.PlatformWechat, ExternalUserID: input.WXID, DisplayName: "微信 · " + input.WXID, MappedUserID: pairing.OwnerUserID}
	if err := bindExternalIdentityTx(ctx, tx, identity); err != nil {
		return nil, err
	}
	connector, findErr := scanConnector(tx.QueryRow(ctx, `SELECT `+connectorColumns+` FROM knowledge.connector_accounts WHERE owner_user_id=$1 AND platform=$2 AND status<>'revoked' ORDER BY updated_at DESC LIMIT 1 FOR UPDATE`, pairing.OwnerUserID, domain.PlatformWechat))
	if findErr != nil && !errors.Is(findErr, pgx.ErrNoRows) {
		return nil, dbError(findErr)
	}
	if errors.Is(findErr, pgx.ErrNoRows) {
		connector = &domain.ConnectorAccount{ID: uuid.NewString(), OwnerUserID: pairing.OwnerUserID, Platform: domain.PlatformWechat, Status: domain.ConnectorActive, CreatedAt: now}
	} else {
		if _, err = tx.Exec(ctx, `UPDATE knowledge.agent_devices SET revoked_at=$2 WHERE connector_id=$1 AND revoked_at IS NULL`, connector.ID, now); err != nil {
			return nil, dbError(err)
		}
		if _, err = tx.Exec(ctx, `UPDATE knowledge.conversation_collectors SET status='unavailable',last_error='connector_revoked',updated_at=$2 WHERE connector_account_id=$1 AND status<>'removed'`, connector.ID, now); err != nil {
			return nil, dbError(err)
		}
	}
	connector.ExternalAccountID = input.WXID
	connector.WorkspaceKey = ""
	connector.DisplayName = "微信 · " + input.WXID
	connector.DefaultOrganizationID = pairing.DefaultOrganizationID
	connector.CredentialRef = ""
	connector.TokenExpiresAt = time.Time{}
	connector.Status = domain.ConnectorActive
	connector.LastError = ""
	connector.UpdatedAt = now
	row := tx.QueryRow(ctx, `INSERT INTO knowledge.connector_accounts (id,owner_user_id,platform,platform_workspace_key,external_account_id,display_name,credential_ref,token_expires_at,default_organization_id,status,last_error,created_at,updated_at)
		VALUES ($1,$2,$3,$4,$5,$6,$7,NULL,$8,$9,NULL,$10,$11)
		ON CONFLICT (id) DO UPDATE SET platform_workspace_key=EXCLUDED.platform_workspace_key,external_account_id=EXCLUDED.external_account_id,display_name=EXCLUDED.display_name,credential_ref='',token_expires_at=NULL,default_organization_id=EXCLUDED.default_organization_id,status=EXCLUDED.status,last_error=NULL,updated_at=EXCLUDED.updated_at
		RETURNING `+connectorColumns, connector.ID, connector.OwnerUserID, connector.Platform, connector.WorkspaceKey, connector.ExternalAccountID, connector.DisplayName, connector.CredentialRef, nilString(connector.DefaultOrganizationID), connector.Status, connector.CreatedAt, now)
	saved, err := scanConnector(row)
	if err != nil && isUnique(err) {
		return nil, apperror.New("connector_already_bound", "external account or platform is already bound", 409, false)
	}
	if err != nil {
		return nil, dbError(err)
	}
	device := domain.AgentDevice{ID: input.DeviceID, ConnectorID: saved.ID, OwnerUserID: pairing.OwnerUserID, KeyHash: input.DeviceKeyHash, ExpiresAt: input.DeviceExpiresAt.UTC(), AgentVersion: input.AgentVersion, CreatedAt: now}
	if _, err = tx.Exec(ctx, `INSERT INTO knowledge.agent_devices (id,connector_id,owner_user_id,key_hash,expires_at,agent_version,created_at) VALUES ($1,$2,$3,$4,$5,$6,$7)`, device.ID, device.ConnectorID, device.OwnerUserID, device.KeyHash, device.ExpiresAt, nilString(device.AgentVersion), device.CreatedAt); err != nil {
		return nil, apperror.Wrap("device_store_failed", "cannot store agent device", 503, true, err)
	}
	consumedAt := now
	_, err = tx.Exec(ctx, `UPDATE knowledge.wechat_pairings SET status='consumed',consumed_at=$2,wxid=$3,database_ref=$4,device_id=$5,connector_id=$6 WHERE id=$1`, pairing.ID, consumedAt, input.WXID, nilString(input.DatabaseRef), device.ID, saved.ID)
	if err != nil {
		return nil, dbError(err)
	}
	pairing.Status = "consumed"
	pairing.ConsumedAt = &consumedAt
	pairing.WXID = input.WXID
	pairing.DatabaseRef = input.DatabaseRef
	pairing.DeviceID = device.ID
	pairing.ConnectorID = saved.ID
	if err := tx.Commit(ctx); err != nil {
		return nil, dbError(err)
	}
	return &AgentPairingResult{Pairing: pairing, Connector: *saved, Device: device}, nil
}
func (s *PostgresStore) GetDeviceByHash(ctx context.Context, keyHash string) (*domain.AgentDevice, error) {
	var d domain.AgentDevice
	err := s.pool.QueryRow(ctx, `SELECT id::text,connector_id::text,owner_user_id::text,key_hash,expires_at,revoked_at,last_seen_at,COALESCE(agent_version,''),created_at FROM knowledge.agent_devices WHERE key_hash=$1`, keyHash).Scan(&d.ID, &d.ConnectorID, &d.OwnerUserID, &d.KeyHash, &d.ExpiresAt, &d.RevokedAt, &d.LastSeenAt, &d.AgentVersion, &d.CreatedAt)
	if errors.Is(err, pgx.ErrNoRows) {
		return nil, apperror.New("agent_device_invalid", "agent device is invalid", 401, false)
	}
	return &d, dbError(err)
}
func (s *PostgresStore) RevokeDevices(ctx context.Context, connectorID string) error {
	_, err := s.pool.Exec(ctx, `UPDATE knowledge.agent_devices SET revoked_at=now() WHERE connector_id=$1 AND revoked_at IS NULL`, connectorID)
	return dbError(err)
}

func (s *PostgresStore) RevokeDevice(ctx context.Context, connectorID, deviceID string) error {
	tag, err := s.pool.Exec(ctx, `UPDATE knowledge.agent_devices SET revoked_at=COALESCE(revoked_at,now()) WHERE id=$1 AND connector_id=$2`, deviceID, connectorID)
	if err != nil {
		return dbError(err)
	}
	if tag.RowsAffected() == 0 {
		return apperror.New("device_not_found", "agent device not found", 404, false)
	}
	return nil
}
func (s *PostgresStore) TouchDevice(ctx context.Context, deviceID, version string, now time.Time) error {
	_, err := s.pool.Exec(ctx, `UPDATE knowledge.agent_devices SET last_seen_at=$2,agent_version=$3 WHERE id=$1`, deviceID, now, nilString(version))
	return dbError(err)
}
func (s *PostgresStore) UpsertExternalIdentity(ctx context.Context, input ExternalIdentityInput) (string, error) {
	if strings.TrimSpace(input.Platform) == "" || strings.TrimSpace(input.ExternalUserID) == "" {
		return "", apperror.New("invalid_external_identity", "platform and external user id are required", 400, false)
	}
	var id, status string
	// Workspace keys are empty for personal WeChat. Keep the canonical empty
	// string on new rows; older databases may contain NULL, so reads and
	// relation checks below use NULL-safe comparisons as well.
	err := s.pool.QueryRow(ctx, `INSERT INTO knowledge.external_identities (platform,platform_workspace_key,external_user_id,display_name,avatar_url,mapped_user_id,mapping_status,mapped_at) VALUES ($1,$2,$3,$4,$5,$6::uuid,CASE WHEN $6::uuid IS NULL THEN 'unmapped' ELSE 'mapped' END,CASE WHEN $6::uuid IS NULL THEN NULL ELSE now() END) ON CONFLICT (platform,platform_workspace_key,external_user_id) DO UPDATE SET display_name=COALESCE(NULLIF(EXCLUDED.display_name,''),knowledge.external_identities.display_name),avatar_url=COALESCE(NULLIF(EXCLUDED.avatar_url,''),knowledge.external_identities.avatar_url),mapped_user_id=CASE WHEN knowledge.external_identities.mapped_user_id IS NULL THEN EXCLUDED.mapped_user_id WHEN EXCLUDED.mapped_user_id IS NULL OR knowledge.external_identities.mapped_user_id=EXCLUDED.mapped_user_id THEN knowledge.external_identities.mapped_user_id ELSE NULL END,mapping_status=CASE WHEN EXCLUDED.mapped_user_id IS NULL OR knowledge.external_identities.mapped_user_id IS NULL OR knowledge.external_identities.mapped_user_id=EXCLUDED.mapped_user_id THEN CASE WHEN COALESCE(knowledge.external_identities.mapped_user_id,EXCLUDED.mapped_user_id) IS NULL THEN 'unmapped' ELSE 'mapped' END ELSE 'conflict' END,mapped_at=CASE WHEN EXCLUDED.mapped_user_id IS NULL THEN knowledge.external_identities.mapped_at ELSE now() END,updated_at=now() RETURNING id::text,mapping_status`, input.Platform, input.WorkspaceKey, input.ExternalUserID, nilString(input.DisplayName), nilString(input.AvatarURL), nilString(input.MappedUserID)).Scan(&id, &status)
	if err != nil {
		return id, dbError(err)
	}
	if status == "conflict" && strings.TrimSpace(input.MappedUserID) != "" {
		return id, apperror.New("external_id_conflict", "external identity is mapped to another user", 409, false)
	}
	return id, nil
}

func (s *PostgresStore) GetExternalIdentity(ctx context.Context, platform, workspaceKey, externalUserID string) (*ExternalIdentity, error) {
	var identity ExternalIdentity
	err := s.pool.QueryRow(ctx, `SELECT id::text,platform,platform_workspace_key,external_user_id,COALESCE(display_name,''),COALESCE(avatar_url,''),COALESCE(mapped_user_id::text,''),mapping_status FROM knowledge.external_identities WHERE platform=$1 AND platform_workspace_key IS NOT DISTINCT FROM $2 AND external_user_id=$3`, platform, workspaceKey, externalUserID).Scan(&identity.ID, &identity.Platform, &identity.WorkspaceKey, &identity.ExternalUserID, &identity.DisplayName, &identity.AvatarURL, &identity.MappedUserID, &identity.MappingStatus)
	if errors.Is(err, pgx.ErrNoRows) {
		return nil, apperror.New("external_identity_not_found", "external identity was not found", 404, false)
	}
	if err != nil {
		return nil, dbError(err)
	}
	return &identity, nil
}

const contactRelationColumns = `cr.id::text,cr.owner_user_id::text,cr.connector_account_id::text,cr.status,cr.created_at,cr.updated_at,ei.id::text,ei.platform,ei.platform_workspace_key,ei.external_user_id,COALESCE(ei.display_name,''),COALESCE(ei.avatar_url,''),COALESCE(ei.mapped_user_id::text,''),ei.mapping_status`

func scanContactRelation(row rowScanner) (*ContactRelation, error) {
	var relation ContactRelation
	err := row.Scan(&relation.ID, &relation.OwnerUserID, &relation.ConnectorID, &relation.Status, &relation.CreatedAt, &relation.UpdatedAt, &relation.ExternalIdentity.ID, &relation.ExternalIdentity.Platform, &relation.ExternalIdentity.WorkspaceKey, &relation.ExternalIdentity.ExternalUserID, &relation.ExternalIdentity.DisplayName, &relation.ExternalIdentity.AvatarURL, &relation.ExternalIdentity.MappedUserID, &relation.ExternalIdentity.MappingStatus)
	return &relation, err
}

func (s *PostgresStore) ListContactRelations(ctx context.Context, userID, platform string) ([]ContactRelation, error) {
	query := `SELECT ` + contactRelationColumns + ` FROM knowledge.contact_relations cr JOIN knowledge.external_identities ei ON ei.id=cr.external_identity_id JOIN knowledge.connector_accounts ca ON ca.id=cr.connector_account_id WHERE cr.owner_user_id=$1 AND cr.status='active' AND ca.status<>'revoked'`
	args := []any{userID}
	if platform != "" {
		query += ` AND ei.platform=$2`
		args = append(args, platform)
	}
	query += ` ORDER BY cr.created_at,cr.id`
	rows, err := s.pool.Query(ctx, query, args...)
	if err != nil {
		return nil, dbError(err)
	}
	defer rows.Close()
	out := []ContactRelation{}
	for rows.Next() {
		relation, scanErr := scanContactRelation(rows)
		if scanErr != nil {
			return nil, dbError(scanErr)
		}
		out = append(out, *relation)
	}
	return out, dbError(rows.Err())
}

func (s *PostgresStore) UpsertContactRelation(ctx context.Context, input ContactRelationInput) (*ContactRelation, error) {
	if strings.TrimSpace(input.OwnerUserID) == "" || strings.TrimSpace(input.ConnectorID) == "" || strings.TrimSpace(input.ExternalIdentityID) == "" {
		return nil, apperror.New("invalid_contact", "owner, connector, and external identity are required", 400, false)
	}
	var relation *ContactRelation
	err := func() error {
		row := s.pool.QueryRow(ctx, `WITH upserted AS (INSERT INTO knowledge.contact_relations (owner_user_id,connector_account_id,external_identity_id,status) SELECT $1,ca.id,$3,'active' FROM knowledge.connector_accounts ca JOIN knowledge.external_identities ei ON ei.id=$3 WHERE ca.id=$2 AND ca.owner_user_id=$1 AND ca.status<>'revoked' AND ca.platform=ei.platform AND ca.platform_workspace_key IS NOT DISTINCT FROM ei.platform_workspace_key ON CONFLICT (owner_user_id,external_identity_id) DO UPDATE SET connector_account_id=EXCLUDED.connector_account_id,status='active',updated_at=now() RETURNING id) SELECT `+contactRelationColumns+` FROM knowledge.contact_relations cr JOIN knowledge.external_identities ei ON ei.id=cr.external_identity_id WHERE cr.id=(SELECT id FROM upserted)`, input.OwnerUserID, input.ConnectorID, input.ExternalIdentityID)
		var scanErr error
		relation, scanErr = scanContactRelation(row)
		return scanErr
	}()
	if errors.Is(err, pgx.ErrNoRows) {
		return nil, apperror.New("contact_not_allowed", "contact identity does not belong to this connector", 403, false)
	}
	if err != nil {
		return nil, dbError(err)
	}
	return relation, nil
}

func (s *PostgresStore) DeleteContactRelation(ctx context.Context, userID, relationID string) error {
	tag, err := s.pool.Exec(ctx, `UPDATE knowledge.contact_relations SET status='removed',updated_at=now() WHERE id=$1 AND owner_user_id=$2 AND status='active'`, relationID, userID)
	if err != nil {
		return dbError(err)
	}
	if tag.RowsAffected() == 0 {
		return apperror.New("contact_not_found", "contact relation was not found", 404, false)
	}
	return nil
}

func bindExternalIdentityTx(ctx context.Context, tx pgx.Tx, input ExternalIdentityInput) error {
	if strings.TrimSpace(input.Platform) == "" || strings.TrimSpace(input.ExternalUserID) == "" {
		return apperror.New("invalid_external_identity", "platform and external user id are required", 400, false)
	}
	var currentMappedUserID string
	err := tx.QueryRow(ctx, `SELECT COALESCE(mapped_user_id::text,'') FROM knowledge.external_identities WHERE platform=$1 AND platform_workspace_key IS NOT DISTINCT FROM $2 AND external_user_id=$3 FOR UPDATE`, input.Platform, input.WorkspaceKey, input.ExternalUserID).Scan(&currentMappedUserID)
	if err != nil && !errors.Is(err, pgx.ErrNoRows) {
		return dbError(err)
	}
	if err == nil && input.MappedUserID != "" && currentMappedUserID != "" && currentMappedUserID != input.MappedUserID {
		return apperror.New("external_id_conflict", "external identity is mapped to another user", 409, false)
	}
	_, err = tx.Exec(ctx, `INSERT INTO knowledge.external_identities (platform,platform_workspace_key,external_user_id,display_name,avatar_url,mapped_user_id,mapping_status,mapped_at)
		VALUES ($1,$2,$3,$4,$5,$6::uuid,CASE WHEN $6::uuid IS NULL THEN 'unmapped' ELSE 'mapped' END,CASE WHEN $6::uuid IS NULL THEN NULL ELSE now() END)
		ON CONFLICT (platform,platform_workspace_key,external_user_id) DO UPDATE SET
			display_name=COALESCE(NULLIF(EXCLUDED.display_name,''),knowledge.external_identities.display_name),
			avatar_url=COALESCE(NULLIF(EXCLUDED.avatar_url,''),knowledge.external_identities.avatar_url),
			mapped_user_id=COALESCE(knowledge.external_identities.mapped_user_id,EXCLUDED.mapped_user_id),
			mapping_status=CASE WHEN COALESCE(knowledge.external_identities.mapped_user_id,EXCLUDED.mapped_user_id) IS NULL THEN 'unmapped' ELSE 'mapped' END,
			mapped_at=CASE WHEN knowledge.external_identities.mapped_user_id IS NULL AND EXCLUDED.mapped_user_id IS NOT NULL THEN now() ELSE knowledge.external_identities.mapped_at END,
			updated_at=now()`, input.Platform, input.WorkspaceKey, input.ExternalUserID, nilString(input.DisplayName), nilString(input.AvatarURL), nilString(input.MappedUserID))
	return dbError(err)
}

func upsertMembershipRows(ctx context.Context, tx pgx.Tx, conversationID, platform, workspace string, members []domain.AvailableMember) error {
	seen := make(map[string]struct{}, len(members))
	for _, member := range members {
		externalID := strings.TrimSpace(member.ExternalUserID)
		if externalID == "" {
			continue
		}
		seen[externalID] = struct{}{}
		var identityID string
		if err := tx.QueryRow(ctx, `INSERT INTO knowledge.external_identities (platform,platform_workspace_key,external_user_id,display_name,mapping_status) VALUES ($1,$2,$3,$4,'unmapped') ON CONFLICT (platform,platform_workspace_key,external_user_id) DO UPDATE SET display_name=COALESCE(NULLIF(EXCLUDED.display_name,''),knowledge.external_identities.display_name),updated_at=now() RETURNING id::text`, platform, workspace, externalID, nilString(strings.TrimSpace(member.DisplayName))).Scan(&identityID); err != nil {
			return dbError(err)
		}
		if _, err := tx.Exec(ctx, `INSERT INTO knowledge.conversation_memberships (conversation_ingestion_id,external_identity_id,member_role,status,joined_at,last_seen_at) VALUES ($1,$2,$3,'active',now(),now()) ON CONFLICT (conversation_ingestion_id,external_identity_id) DO UPDATE SET member_role=EXCLUDED.member_role,status='active',left_at=NULL,last_seen_at=now(),updated_at=now()`, conversationID, identityID, nilString(strings.TrimSpace(member.MemberRole))); err != nil {
			return dbError(err)
		}
	}
	if len(seen) == 0 {
		return nil
	}
	rows, err := tx.Query(ctx, `SELECT cm.external_identity_id::text,ei.external_user_id FROM knowledge.conversation_memberships cm JOIN knowledge.external_identities ei ON ei.id=cm.external_identity_id WHERE cm.conversation_ingestion_id=$1 AND cm.status='active'`, conversationID)
	if err != nil {
		return dbError(err)
	}
	leftIDs := make([]string, 0)
	for rows.Next() {
		var identityID, externalID string
		if scanErr := rows.Scan(&identityID, &externalID); scanErr != nil {
			rows.Close()
			return dbError(scanErr)
		}
		if _, ok := seen[externalID]; ok {
			continue
		}
		leftIDs = append(leftIDs, identityID)
	}
	rows.Close()
	if err := rows.Err(); err != nil {
		return dbError(err)
	}
	for _, identityID := range leftIDs {
		if _, updateErr := tx.Exec(ctx, `UPDATE knowledge.conversation_memberships SET status='left',left_at=COALESCE(left_at,now()),updated_at=now() WHERE conversation_ingestion_id=$1 AND external_identity_id=$2 AND status='active'`, conversationID, identityID); updateErr != nil {
			return dbError(updateErr)
		}
	}
	return nil
}

func (s *PostgresStore) UpsertConversationMemberships(ctx context.Context, conversationID string, members []domain.AvailableMember) error {
	if len(members) == 0 {
		return nil
	}
	tx, err := s.pool.Begin(ctx)
	if err != nil {
		return dbError(err)
	}
	defer tx.Rollback(ctx)
	var platform, workspace string
	if err := tx.QueryRow(ctx, `SELECT platform,platform_workspace_key FROM knowledge.conversation_ingestions WHERE id=$1`, conversationID).Scan(&platform, &workspace); errors.Is(err, pgx.ErrNoRows) {
		return apperror.New("conversation_not_found", "conversation not found", 404, false)
	} else if err != nil {
		return dbError(err)
	}
	if err := upsertMembershipRows(ctx, tx, conversationID, platform, workspace, members); err != nil {
		return err
	}
	return dbError(tx.Commit(ctx))
}

func (s *PostgresStore) ListConversationMemberships(ctx context.Context, conversationID string) ([]domain.ConversationMembership, error) {
	rows, err := s.pool.Query(ctx, `SELECT cm.id::text,cm.conversation_ingestion_id::text,cm.external_identity_id::text,ei.external_user_id,COALESCE(ei.display_name,''),COALESCE(cm.member_role,''),cm.status,cm.joined_at,cm.left_at,cm.last_seen_at FROM knowledge.conversation_memberships cm JOIN knowledge.external_identities ei ON ei.id=cm.external_identity_id WHERE cm.conversation_ingestion_id=$1 ORDER BY cm.joined_at NULLS LAST,cm.id`, conversationID)
	if err != nil {
		return nil, dbError(err)
	}
	defer rows.Close()
	out := make([]domain.ConversationMembership, 0)
	for rows.Next() {
		var membership domain.ConversationMembership
		if scanErr := rows.Scan(&membership.ID, &membership.ConversationID, &membership.ExternalIdentityID, &membership.ExternalUserID, &membership.DisplayName, &membership.MemberRole, &membership.Status, &membership.JoinedAt, &membership.LeftAt, &membership.LastSeenAt); scanErr != nil {
			return nil, dbError(scanErr)
		}
		out = append(out, membership)
	}
	return out, dbError(rows.Err())
}

func (s *PostgresStore) CheckConversationMembership(ctx context.Context, conversationID, platform, workspaceKey, userID string) (bool, bool, error) {
	var exists bool
	if err := s.pool.QueryRow(ctx, `SELECT EXISTS (SELECT 1 FROM knowledge.conversation_ingestions WHERE id=$1)`, conversationID).Scan(&exists); err != nil {
		return false, false, dbError(err)
	}
	if !exists {
		return false, false, apperror.New("conversation_not_found", "conversation not found", 404, false)
	}
	var known, member bool
	if err := s.pool.QueryRow(ctx, `SELECT EXISTS (SELECT 1 FROM knowledge.conversation_memberships cm WHERE cm.conversation_ingestion_id=$1 AND cm.status='active'), EXISTS (SELECT 1 FROM knowledge.conversation_memberships cm JOIN knowledge.external_identities ei ON ei.id=cm.external_identity_id WHERE cm.conversation_ingestion_id=$1 AND cm.status='active' AND ei.platform=$2 AND ei.platform_workspace_key=$3 AND ei.mapped_user_id=$4 AND ei.mapping_status='mapped')`, conversationID, platform, workspaceKey, userID).Scan(&known, &member); err != nil {
		return false, false, dbError(err)
	}
	return known, member, nil
}

func (s *PostgresStore) SaveDiscovery(ctx context.Context, d domain.Discovery) error {
	return s.ephemeral.SaveDiscovery(ctx, d)
}
func (s *PostgresStore) GetDiscovery(ctx context.Context, id, userID, connectorID string) (*domain.Discovery, error) {
	return s.ephemeral.GetDiscovery(ctx, id, userID, connectorID)
}
func (s *PostgresStore) ListDiscoveries(ctx context.Context, userID, connectorID string) ([]domain.Discovery, error) {
	return s.ephemeral.ListDiscoveries(ctx, userID, connectorID)
}

const conversationColumns = `id::text,platform,platform_workspace_key,external_conversation_id,conversation_type,COALESCE(name,''),COALESCE(avatar_url,''),COALESCE(knowledge_base_id::text,''),ingestion_scope,COALESCE(owner_user_id::text,''),COALESCE(organization_id::text,''),created_by_user_id::text,requested_start_at,effective_start_at,status,COALESCE(pause_reason,''),last_synced_at,detached_at,created_at,updated_at`

func scanConversation(row rowScanner) (*domain.ConversationIngestion, error) {
	var c domain.ConversationIngestion
	err := row.Scan(&c.ID, &c.Platform, &c.WorkspaceKey, &c.ExternalConversationID, &c.ConversationType, &c.Name, &c.AvatarURL, &c.KnowledgeBaseID, &c.IngestionScope, &c.OwnerUserID, &c.OrganizationID, &c.CreatedByUserID, &c.RequestedStartAt, &c.EffectiveStartAt, &c.Status, &c.PauseReason, &c.LastSyncedAt, &c.DetachedAt, &c.CreatedAt, &c.UpdatedAt)
	return &c, err
}
func (s *PostgresStore) AttachConversation(ctx context.Context, input AttachInput) (*domain.ConversationIngestion, error) {
	id := uuid.NewString()
	tx, err := s.pool.Begin(ctx)
	if err != nil {
		return nil, dbError(err)
	}
	defer tx.Rollback(ctx)
	if input.PrimaryConnectorID != "" {
		var ownerUserID, platformName, status string
		err = tx.QueryRow(ctx, `SELECT owner_user_id::text,platform,status FROM knowledge.connector_accounts WHERE id=$1 FOR SHARE`, input.PrimaryConnectorID).Scan(&ownerUserID, &platformName, &status)
		if errors.Is(err, pgx.ErrNoRows) || status == domain.ConnectorRevoked {
			return nil, apperror.New("connector_not_found", "connector is not bound", 404, false)
		}
		if err != nil {
			return nil, dbError(err)
		}
		if ownerUserID != input.UserID || platformName != input.Platform {
			return nil, apperror.Clone(apperror.ErrForbidden)
		}
		if status == domain.ConnectorExpired {
			return nil, apperror.New("reauthorization_required", "connector authorization is required", 401, false)
		}
	}
	scope := "private"
	owner := any(input.UserID)
	org := any(nil)
	if input.ConversationType == "group" {
		scope = "organization"
		owner = nil
		org = input.OrganizationID
	}
	baseID, err := ensureConversationKnowledgeBase(ctx, tx, input, scope, owner, org)
	if err != nil {
		return nil, err
	}
	permissionGroupKey := any(nil)
	if input.ConversationType == "group" {
		permissionGroupKey = "conversation:" + id
	}
	row := tx.QueryRow(ctx, `INSERT INTO knowledge.conversation_ingestions (id,platform,platform_workspace_key,external_conversation_id,conversation_type,name,avatar_url,knowledge_base_id,ingestion_scope,owner_user_id,organization_id,created_by_user_id,requested_start_at,effective_start_at,permission_group_key,status) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$13,$14,'active') RETURNING `+conversationColumns, id, input.Platform, input.WorkspaceKey, input.ExternalConversationID, input.ConversationType, nilString(input.Name), nilString(input.AvatarURL), baseID, scope, owner, org, input.UserID, input.RequestedStartAt, permissionGroupKey)
	c, err := scanConversation(row)
	if isUnique(err) {
		return nil, apperror.New("conversation_already_attached", "conversation is already attached", 409, false)
	}
	if err != nil {
		return nil, dbError(err)
	}
	if err := upsertMembershipRows(ctx, tx, id, input.Platform, input.WorkspaceKey, input.Members); err != nil {
		return nil, err
	}
	if len(input.Members) > 0 {
		var member bool
		if err := tx.QueryRow(ctx, `SELECT EXISTS (SELECT 1 FROM knowledge.conversation_memberships cm JOIN knowledge.external_identities ei ON ei.id=cm.external_identity_id WHERE cm.conversation_ingestion_id=$1 AND cm.status='active' AND ei.platform=$2 AND ei.platform_workspace_key=$3 AND ei.mapped_user_id=$4 AND ei.mapping_status='mapped')`, id, input.Platform, input.WorkspaceKey, input.UserID).Scan(&member); err != nil {
			return nil, dbError(err)
		}
		if !member {
			return nil, apperror.Clone(apperror.ErrForbidden)
		}
	}
	var primary *domain.Collector
	if input.PrimaryConnectorID != "" {
		primary, err = scanCollector(tx.QueryRow(ctx, `INSERT INTO knowledge.conversation_collectors (id,conversation_ingestion_id,connector_account_id,collector_user_id,collector_role,status) VALUES ($1,$2,$3,$4,$5,'active') RETURNING `+collectorColumns, uuid.NewString(), id, input.PrimaryConnectorID, input.UserID, domain.CollectorPrimary))
		if err != nil && isUnique(err) {
			return nil, apperror.New("collector_already_exists", "collector already exists or primary collector is occupied", 409, false)
		}
		if err != nil {
			return nil, dbError(err)
		}
	}
	if err := tx.Commit(ctx); err != nil {
		return nil, dbError(err)
	}
	if len(input.Members) > 0 {
		c.Memberships, _ = s.ListConversationMemberships(ctx, id)
	}
	if primary != nil {
		c.Collectors = []domain.Collector{*primary}
	}
	return c, nil
}

func ensureConversationKnowledgeBase(ctx context.Context, tx pgx.Tx, input AttachInput, scope string, owner, org any) (string, error) {
	baseType := "private_conversation"
	if input.ConversationType == "group" {
		baseType = "organization_conversation"
	}
	name := strings.TrimSpace(input.Name)
	if name == "" {
		name = input.ExternalConversationID
	}
	sourceKey := conversationSourceKey(input)
	var id string
	var err error
	if input.ConversationType == "group" {
		err = tx.QueryRow(ctx, `WITH inserted AS (
			INSERT INTO knowledge.knowledge_bases (knowledge_scope,base_type,name,owner_user_id,organization_id,source_key,status)
			VALUES ($1,$2,$3,NULL,$4::uuid,$5,'active')
			ON CONFLICT DO NOTHING
			RETURNING id::text
		)
		SELECT id FROM inserted
		UNION ALL
		SELECT id::text FROM knowledge.knowledge_bases WHERE knowledge_scope=$1 AND base_type=$2 AND organization_id=$4::uuid AND source_key=$5 AND status='active'
		LIMIT 1`, scope, baseType, name, org, sourceKey).Scan(&id)
	} else {
		err = tx.QueryRow(ctx, `WITH inserted AS (
			INSERT INTO knowledge.knowledge_bases (knowledge_scope,base_type,name,owner_user_id,organization_id,source_key,status)
			VALUES ($1,$2,$3,$4::uuid,NULL,$5,'active')
			ON CONFLICT DO NOTHING
			RETURNING id::text
		)
		SELECT id FROM inserted
		UNION ALL
		SELECT id::text FROM knowledge.knowledge_bases WHERE knowledge_scope=$1 AND base_type=$2 AND owner_user_id=$4::uuid AND source_key=$5 AND status='active'
		LIMIT 1`, scope, baseType, name, owner, sourceKey).Scan(&id)
	}
	if errors.Is(err, pgx.ErrNoRows) {
		return "", apperror.New("knowledge_base_not_found", "conversation knowledge base cannot be created", 503, true)
	}
	return id, dbError(err)
}

func conversationSourceKey(input AttachInput) string {
	sum := sha256.Sum256([]byte(input.Platform + "\x00" + input.WorkspaceKey + "\x00" + input.ExternalConversationID))
	return "conversation:" + hex.EncodeToString(sum[:])
}

func (s *PostgresStore) GetConversation(ctx context.Context, id string) (*domain.ConversationIngestion, error) {
	c, err := scanConversation(s.pool.QueryRow(ctx, `SELECT `+conversationColumns+` FROM knowledge.conversation_ingestions WHERE id=$1`, id))
	if errors.Is(err, pgx.ErrNoRows) {
		return nil, apperror.New("conversation_not_found", "conversation not found", 404, false)
	}
	if err != nil {
		return nil, dbError(err)
	}
	collectors, err := s.ListCollectors(ctx, id)
	c.Collectors = collectors
	if err == nil {
		var membershipErr error
		c.Memberships, membershipErr = s.ListConversationMemberships(ctx, id)
		if membershipErr != nil {
			err = membershipErr
		}
	}
	if err == nil {
		err = s.populateConversationCounts(ctx, c)
	}
	return c, err
}

func (s *PostgresStore) FindConversationByExternal(ctx context.Context, platformName, workspaceKey, externalConversationID string) (*domain.ConversationIngestion, error) {
	c, err := scanConversation(s.pool.QueryRow(ctx, `SELECT `+conversationColumns+` FROM knowledge.conversation_ingestions WHERE platform=$1 AND platform_workspace_key=$2 AND external_conversation_id=$3 AND conversation_type='group' AND status IN ('active','paused') LIMIT 1`, platformName, workspaceKey, externalConversationID))
	if errors.Is(err, pgx.ErrNoRows) {
		return nil, apperror.New("conversation_not_found", "conversation not found", 404, false)
	}
	if err != nil {
		return nil, dbError(err)
	}
	c.Collectors, err = s.ListCollectors(ctx, c.ID)
	if err == nil {
		c.Memberships, err = s.ListConversationMemberships(ctx, c.ID)
	}
	if err == nil {
		err = s.populateConversationCounts(ctx, c)
	}
	return c, err
}

func (s *PostgresStore) ListConversations(ctx context.Context, userID, platformName string) ([]domain.ConversationIngestion, error) {
	// Keep the qualified projection explicit. A string replacement against the
	// unqualified projection would also rewrite `knowledge_base_id::text` and
	// produce invalid SQL.
	rows, err := s.pool.Query(ctx, `SELECT DISTINCT ci.id::text,ci.platform,ci.platform_workspace_key,ci.external_conversation_id,ci.conversation_type,COALESCE(ci.name,''),COALESCE(ci.avatar_url,''),COALESCE(ci.knowledge_base_id::text,''),ci.ingestion_scope,COALESCE(ci.owner_user_id::text,''),COALESCE(ci.organization_id::text,''),ci.created_by_user_id::text,ci.requested_start_at,ci.effective_start_at,ci.status,COALESCE(ci.pause_reason,''),ci.last_synced_at,ci.detached_at,ci.created_at,ci.updated_at FROM knowledge.conversation_ingestions ci LEFT JOIN knowledge.conversation_collectors cc ON cc.conversation_ingestion_id=ci.id AND cc.status<>'removed' WHERE ci.platform=$2 AND (ci.owner_user_id=$1 OR cc.collector_user_id=$1) ORDER BY ci.updated_at DESC`, userID, platformName)
	if err != nil {
		return nil, dbError(err)
	}
	defer rows.Close()
	out := []domain.ConversationIngestion{}
	for rows.Next() {
		c, scanErr := scanConversation(rows)
		if scanErr != nil {
			return nil, dbError(scanErr)
		}
		c.Collectors, _ = s.ListCollectors(ctx, c.ID)
		c.Memberships, _ = s.ListConversationMemberships(ctx, c.ID)
		if err := s.populateConversationCounts(ctx, c); err != nil {
			return nil, err
		}
		out = append(out, *c)
	}
	return out, dbError(rows.Err())
}

func (s *PostgresStore) populateConversationCounts(ctx context.Context, c *domain.ConversationIngestion) error {
	if err := s.pool.QueryRow(ctx, `SELECT COUNT(*) FROM knowledge.messages m WHERE m.conversation_ingestion_id=$1 AND btrim(COALESCE(m.normalized_content,'')) <> '' AND NOT (m.message_type IN ('image','file') AND EXISTS (SELECT 1 FROM knowledge.attachments a WHERE a.message_id=m.id))`, c.ID).Scan(&c.MessageCount); err != nil {
		return dbError(err)
	}
	if err := s.pool.QueryRow(ctx, `SELECT COUNT(*) FROM knowledge.attachments WHERE conversation_ingestion_id=$1`, c.ID).Scan(&c.AttachmentCount); err != nil {
		return dbError(err)
	}
	return nil
}

const collectorColumns = `id::text,conversation_ingestion_id::text,connector_account_id::text,collector_user_id::text,collector_role,status,COALESCE(last_cursor,''),last_success_at,last_attempt_at,next_poll_at,consecutive_failures,COALESCE(last_error,''),joined_at,removed_at,(SELECT last_heartbeat_at FROM knowledge.wechat_collector_runtime wr WHERE wr.connector_account_id=conversation_collectors.connector_account_id)`

func scanCollector(row rowScanner) (*domain.Collector, error) {
	var c domain.Collector
	var heartbeat *time.Time
	err := row.Scan(&c.ID, &c.ConversationID, &c.ConnectorAccountID, &c.CollectorUserID, &c.CollectorRole, &c.Status, &c.LastCursor, &c.LastSuccessAt, &c.LastAttemptAt, &c.NextPollAt, &c.ConsecutiveFailures, &c.LastError, &c.JoinedAt, &c.RemovedAt, &heartbeat)
	c.LastHeartbeatAt = heartbeat
	c.AgentOnline = heartbeat != nil && !heartbeat.After(time.Now().UTC()) && time.Since(*heartbeat) < 2*time.Minute
	return &c, err
}
func (s *PostgresStore) AddCollector(ctx context.Context, input CollectorInput) (*domain.Collector, error) {
	id := uuid.NewString()
	c, err := scanCollector(s.pool.QueryRow(ctx, `INSERT INTO knowledge.conversation_collectors (id,conversation_ingestion_id,connector_account_id,collector_user_id,collector_role,status) VALUES ($1,$2,$3,$4,$5,'active') RETURNING `+collectorColumns, id, input.ConversationID, input.ConnectorAccountID, input.CollectorUserID, input.Role))
	if isUnique(err) {
		return nil, apperror.New("collector_already_exists", "collector already exists or primary collector is occupied", 409, false)
	}
	return c, dbError(err)
}
func (s *PostgresStore) GetCollector(ctx context.Context, id string) (*domain.Collector, error) {
	c, err := scanCollector(s.pool.QueryRow(ctx, `SELECT `+collectorColumns+` FROM knowledge.conversation_collectors WHERE id=$1`, id))
	if errors.Is(err, pgx.ErrNoRows) {
		return nil, apperror.New("collector_not_found", "collector not found", 404, false)
	}
	return c, dbError(err)
}
func (s *PostgresStore) ListCollectors(ctx context.Context, conversationID string) ([]domain.Collector, error) {
	rows, err := s.pool.Query(ctx, `SELECT `+collectorColumns+` FROM knowledge.conversation_collectors WHERE conversation_ingestion_id=$1 ORDER BY joined_at`, conversationID)
	if err != nil {
		return nil, dbError(err)
	}
	defer rows.Close()
	out := []domain.Collector{}
	for rows.Next() {
		c, scanErr := scanCollector(rows)
		if scanErr != nil {
			return nil, dbError(scanErr)
		}
		out = append(out, *c)
	}
	return out, dbError(rows.Err())
}
func (s *PostgresStore) ListCollectorsByConnector(ctx context.Context, connectorID string) ([]domain.Collector, error) {
	rows, err := s.pool.Query(ctx, `SELECT `+collectorColumns+` FROM knowledge.conversation_collectors WHERE connector_account_id=$1 AND status='active' ORDER BY joined_at`, connectorID)
	if err != nil {
		return nil, dbError(err)
	}
	defer rows.Close()
	out := []domain.Collector{}
	for rows.Next() {
		collector, scanErr := scanCollector(rows)
		if scanErr != nil {
			return nil, dbError(scanErr)
		}
		out = append(out, *collector)
	}
	return out, dbError(rows.Err())
}
func (s *PostgresStore) RemoveCollector(ctx context.Context, conversationID, collectorID string) error {
	tx, err := s.pool.Begin(ctx)
	if err != nil {
		return dbError(err)
	}
	defer tx.Rollback(ctx)
	tag, err := tx.Exec(ctx, `UPDATE knowledge.conversation_collectors SET status='removed',removed_at=now(),updated_at=now() WHERE id=$1 AND conversation_ingestion_id=$2`, collectorID, conversationID)
	if err != nil {
		return dbError(err)
	}
	if tag.RowsAffected() == 0 {
		return apperror.New("collector_not_found", "collector not found", 404, false)
	}
	_, err = tx.Exec(ctx, `UPDATE knowledge.conversation_ingestions SET status='paused',pause_reason='no_available_collector',updated_at=now() WHERE id=$1 AND NOT EXISTS (SELECT 1 FROM knowledge.conversation_collectors WHERE conversation_ingestion_id=$1 AND status='active')`, conversationID)
	if err != nil {
		return dbError(err)
	}
	return dbError(tx.Commit(ctx))
}
func (s *PostgresStore) SetConversationStatus(ctx context.Context, id, status, reason string) error {
	// Cast the status parameter explicitly because PostgreSQL otherwise sees it
	// as both a varchar assignment and a text comparison in the CASE expression.
	tag, err := s.pool.Exec(ctx, `UPDATE knowledge.conversation_ingestions SET status=$2::varchar,pause_reason=$3::text,detached_at=CASE WHEN $2::varchar='detached' THEN now() ELSE detached_at END,updated_at=now() WHERE id=$1`, id, status, nilString(reason))
	if err != nil {
		return dbError(err)
	}
	if tag.RowsAffected() == 0 {
		return apperror.New("conversation_not_found", "conversation not found", 404, false)
	}
	return nil
}

func (s *PostgresStore) IngestMessage(ctx context.Context, input IngestMessageInput) (*IngestResult, error) {
	prepared, discard, err := PrepareRepositoryInput(input)
	if err != nil {
		return nil, err
	}
	if discard {
		return &IngestResult{Discarded: true}, nil
	}
	input = prepared
	traceID := trace.TraceID(ctx)
	if traceID == "" {
		traceID = uuid.NewString()
	}
	tx, err := s.pool.Begin(ctx)
	if err != nil {
		return nil, dbError(err)
	}
	defer tx.Rollback(ctx)
	var conversationID, externalConversationID, platformName, workspace, scope, org, owner, knowledgeBaseID string
	err = tx.QueryRow(ctx, `SELECT cc.conversation_ingestion_id::text,ci.external_conversation_id,ci.platform,ci.platform_workspace_key,ci.ingestion_scope,COALESCE(ci.organization_id::text,''),COALESCE(ci.owner_user_id::text,''),COALESCE(ci.knowledge_base_id::text,'') FROM knowledge.conversation_collectors cc JOIN knowledge.conversation_ingestions ci ON ci.id=cc.conversation_ingestion_id WHERE cc.id=$1 AND cc.status='active' AND ci.status='active' FOR UPDATE`, input.CollectorID).Scan(&conversationID, &externalConversationID, &platformName, &workspace, &scope, &org, &owner, &knowledgeBaseID)
	if errors.Is(err, pgx.ErrNoRows) {
		return nil, apperror.New("collector_revoked", "collector is not active", 403, false)
	}
	if err != nil {
		return nil, dbError(err)
	}
	if input.ExternalConversationID != externalConversationID {
		return nil, apperror.New("conversation_mismatch", "external conversation does not match collector", 409, false)
	}
	var existingSourceHash, existingSourceContentHash, existingSourceType, existingSourceContent string
	sourceErr := tx.QueryRow(ctx, `SELECT ms.payload_hash,m.content_hash,m.message_type,COALESCE(m.normalized_content,'') FROM knowledge.message_sources ms JOIN knowledge.messages m ON m.id=ms.message_id WHERE ms.collector_id=$1 AND ms.external_message_id=$2`, input.CollectorID, input.ExternalMessageID).Scan(&existingSourceHash, &existingSourceContentHash, &existingSourceType, &existingSourceContent)
	if sourceErr == nil && !strings.EqualFold(existingSourceHash, SourcePayloadHash(input)) && !strings.EqualFold(existingSourceContentHash, input.ContentHash) && !canReclassifyLegacyFile(existingSourceType, input.MessageType, existingSourceContent, input.Content, input.Attachments) {
		return nil, apperror.New("external_id_conflict", "external message id has conflicting payload", 409, false)
	}
	if sourceErr != nil && !errors.Is(sourceErr, pgx.ErrNoRows) {
		return nil, dbError(sourceErr)
	}
	var identity any
	senderDisplayName := strings.TrimSpace(input.SenderDisplayName)
	if input.SenderExternalID != "" {
		var identityID string
		err = tx.QueryRow(ctx, `INSERT INTO knowledge.external_identities (platform,platform_workspace_key,external_user_id,display_name) VALUES ($1,$2,$3,$4) ON CONFLICT (platform,platform_workspace_key,external_user_id) DO UPDATE SET display_name=COALESCE(NULLIF(EXCLUDED.display_name,''),knowledge.external_identities.display_name),updated_at=now() RETURNING id::text,COALESCE(display_name,'')`, platformName, workspace, input.SenderExternalID, nilString(senderDisplayName)).Scan(&identityID, &senderDisplayName)
		if err != nil {
			return nil, dbError(err)
		}
		identity = identityID
	}
	normalizedContent := any(nil)
	classificationStatus := "pending"
	if scope == "private" {
		normalizedContent = nilString(input.Content)
		classificationStatus = "succeeded"
	}
	messageID := uuid.NewString()
	tag, err := tx.Exec(ctx, `INSERT INTO knowledge.messages (id,conversation_ingestion_id,external_message_id,sender_identity_id,sender_display_name,message_type,normalized_content,content_hash,sent_at,sensitive,classification_status) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,FALSE,$10) ON CONFLICT (conversation_ingestion_id,external_message_id) DO NOTHING`, messageID, conversationID, input.ExternalMessageID, identity, nilString(senderDisplayName), input.MessageType, normalizedContent, input.ContentHash, input.SentAt, classificationStatus)
	if err != nil {
		return nil, dbError(err)
	}
	duplicate := tag.RowsAffected() == 0
	legacyTypeCorrection := false
	legacyAttachmentCleanup := false
	if duplicate {
		var existingHash, existingType, existingContent string
		err = tx.QueryRow(ctx, `SELECT id::text,content_hash,message_type,COALESCE(normalized_content,'') FROM knowledge.messages WHERE conversation_ingestion_id=$1 AND external_message_id=$2`, conversationID, input.ExternalMessageID).Scan(&messageID, &existingHash, &existingType, &existingContent)
		if err != nil {
			return nil, dbError(err)
		}
		if input.SenderExternalID != "" {
			if _, err = tx.Exec(ctx, `UPDATE knowledge.messages SET sender_identity_id=$2,sender_display_name=$3 WHERE id=$1`, messageID, identity, nilString(senderDisplayName)); err != nil {
				return nil, dbError(err)
			}
		}
		// The provider can improve group-sender parsing without changing the
		// message identity. Preserve the original body in that case, but allow
		// its verified sender identity to be corrected during replay.
		if !strings.EqualFold(existingHash, input.ContentHash) {
			if !canReclassifyLegacyFile(existingType, input.MessageType, existingContent, input.Content, input.Attachments) {
				return nil, apperror.New("external_id_conflict", "external message id has conflicting content", 409, false)
			}
			legacyTypeCorrection = true
			legacyAttachmentCleanup = strings.EqualFold(existingType, "file") && strings.EqualFold(input.MessageType, "text")
			if _, err = tx.Exec(ctx, `UPDATE knowledge.messages SET message_type=$2,normalized_content=$3,content_hash=$4 WHERE id=$1`, messageID, input.MessageType, nilString(input.Content), input.ContentHash); err != nil {
				return nil, dbError(err)
			}
		} else if existingType != input.MessageType {
			if !canReclassifyLegacyFile(existingType, input.MessageType, existingContent, input.Content, input.Attachments) {
				return nil, apperror.New("external_id_conflict", "external message id has conflicting content", 409, false)
			}
			legacyTypeCorrection = true
			legacyAttachmentCleanup = strings.EqualFold(existingType, "file") && strings.EqualFold(input.MessageType, "text")
			if _, err = tx.Exec(ctx, `UPDATE knowledge.messages SET message_type=$2 WHERE id=$1`, messageID, input.MessageType); err != nil {
				return nil, dbError(err)
			}
		}
	}
	if legacyTypeCorrection && legacyAttachmentCleanup {
		if _, err = tx.Exec(ctx, `DELETE FROM knowledge.outbox_events WHERE event_type IN ('document.processing.requested','attachment.processing.requested') AND aggregate_id IN (SELECT id FROM knowledge.attachments WHERE message_id=$1 AND content_status='pending' AND object_ref IS NULL)`, messageID); err != nil {
			return nil, dbError(err)
		}
		if _, err = tx.Exec(ctx, `DELETE FROM knowledge.attachments WHERE message_id=$1 AND content_status='pending' AND object_ref IS NULL`, messageID); err != nil {
			return nil, dbError(err)
		}
	}
	sourceID := uuid.NewString()
	collectedAt := input.CollectedAt.UTC()
	if collectedAt.IsZero() {
		collectedAt = time.Now().UTC()
	}
	_, err = tx.Exec(ctx, `INSERT INTO knowledge.message_sources (id,message_id,collector_id,external_message_id,payload_hash,ingest_cursor,collected_at) VALUES ($1,$2,$3,$4,$5,$6,$7) ON CONFLICT (collector_id,external_message_id) DO UPDATE SET payload_hash=EXCLUDED.payload_hash,ingest_cursor=COALESCE(NULLIF(EXCLUDED.ingest_cursor,''),knowledge.message_sources.ingest_cursor)`, sourceID, messageID, input.CollectorID, input.ExternalMessageID, SourcePayloadHash(input), nilString(input.Cursor), collectedAt)
	if err != nil {
		return nil, dbError(err)
	}
	if strings.TrimSpace(input.Content) != "" {
		if _, err = ensureMessageKnowledgeItem(ctx, tx, knowledgeItemSource{
			ConversationID: conversationID, ExternalConversationID: externalConversationID,
			KnowledgeBaseID: knowledgeBaseID, Scope: scope, OrganizationID: org, OwnerUserID: owner,
		}, messageID, input, traceID); err != nil {
			return nil, err
		}
	}
	attachments := []domain.Attachment{}
	for _, a := range input.Attachments {
		attachmentID := uuid.NewString()
		// Conversation membership has already been checked before content is opened.
		// Only attachments identified as sensitive require an additional approval.
		accessRequired := false
		var saved domain.Attachment
		inserted := true
		sensitiveAttachment := privacy.SensitiveAttachmentName(a.FileName)
		accessRequired = sensitiveAttachment
		err = tx.QueryRow(ctx, `INSERT INTO knowledge.attachments (id,conversation_ingestion_id,message_id,external_attachment_id,file_name,mime_type,size_bytes,content_hash,organization_id,access_scope,content_access_required,preview_capability,sensitive,classification_status) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,'conversation_members',$10,$11,$12,'succeeded') ON CONFLICT (conversation_ingestion_id,external_attachment_id) DO NOTHING RETURNING id::text,conversation_ingestion_id::text,COALESCE(message_id::text,''),external_attachment_id,file_name,COALESCE(mime_type,''),size_bytes,COALESCE(object_ref,''),COALESCE(content_hash,''),content_version,content_status,access_scope,content_access_required,COALESCE(preview_capability,''),COALESCE(last_error,''),created_at,updated_at,sensitive,classification_status`, attachmentID, conversationID, messageID, a.ExternalAttachmentID, a.FileName, nilString(a.MIMEType), a.SizeBytes, nilString(a.ContentHash), nilString(org), accessRequired, previewCapability(a.MIMEType), sensitiveAttachment).Scan(&saved.ID, &saved.ConversationID, &saved.MessageID, &saved.ExternalAttachmentID, &saved.FileName, &saved.MIMEType, &saved.SizeBytes, &saved.ObjectRef, &saved.ContentHash, &saved.ContentVersion, &saved.ContentStatus, &saved.AccessScope, &saved.ContentAccessRequired, &saved.PreviewCapability, &saved.LastError, &saved.CreatedAt, &saved.UpdatedAt, &saved.Sensitive, &saved.ClassificationStatus)
		if errors.Is(err, pgx.ErrNoRows) {
			inserted = false
			err = tx.QueryRow(ctx, `SELECT `+attachmentColumns+` FROM knowledge.attachments WHERE conversation_ingestion_id=$1 AND external_attachment_id=$2`, conversationID, a.ExternalAttachmentID).Scan(&saved.ID, &saved.ConversationID, &saved.MessageID, &saved.ExternalAttachmentID, &saved.FileName, &saved.MIMEType, &saved.SizeBytes, &saved.ObjectRef, &saved.ContentHash, &saved.ContentVersion, &saved.ContentStatus, &saved.AccessScope, &saved.ContentAccessRequired, &saved.PreviewCapability, &saved.LastError, &saved.CreatedAt, &saved.UpdatedAt, &saved.Sensitive, &saved.ClassificationStatus)
		}
		if err != nil {
			return nil, dbError(err)
		}
		// Provider-declared sizes are not stable for WeChat resources (the
		// same file may be reported before/after download). A verified hash
		// is the reliable identity check; tolerate size-only corrections so
		// replay can reconcile older incomplete metadata.
		if !inserted && saved.ContentHash != "" && a.ContentHash != "" && !strings.EqualFold(saved.ContentHash, a.ContentHash) {
			return nil, apperror.New("external_id_conflict", "external attachment id has conflicting metadata", 409, false)
		}
		attachments = append(attachments, saved)
		if _, err = ensureAttachmentKnowledgeItem(ctx, tx, knowledgeItemSource{
			ConversationID: conversationID, ExternalConversationID: externalConversationID,
			KnowledgeBaseID: knowledgeBaseID, Scope: scope, OrganizationID: org, OwnerUserID: owner,
		}, messageID, saved, traceID); err != nil {
			return nil, err
		}
		if inserted {
			payload, _ := json.Marshal(map[string]any{"message_id": messageID, "attachment_id": saved.ID, "content_version": 1})
			_, err = tx.Exec(ctx, `INSERT INTO knowledge.outbox_events (id,aggregate_type,aggregate_id,event_type,trace_id,organization_id,payload) VALUES ($1,'attachment',$2,'document.processing.requested',$3,$4,$5)`, uuid.NewString(), saved.ID, traceID, nilString(org), payload)
			if err != nil {
				return nil, dbError(err)
			}
		}
	}
	if !duplicate {
		if _, err = tx.Exec(ctx, `INSERT INTO knowledge.message_private_content (message_id,content) VALUES ($1,$2)`, messageID, input.Content); err != nil {
			return nil, dbError(err)
		}
		if scope != "private" && strings.TrimSpace(input.Content) != "" {
			payload, _ := json.Marshal(map[string]any{"message_id": messageID, "content_version": 1})
			_, err = tx.Exec(ctx, `INSERT INTO knowledge.outbox_events (id,aggregate_type,aggregate_id,event_type,trace_id,organization_id,payload) VALUES ($1,'message',$2,'privacy.scan.requested',$3,$4,$5)`, uuid.NewString(), messageID, traceID, nilString(org), payload)
			if err != nil {
				return nil, dbError(err)
			}
		}
	}
	if err = tx.Commit(ctx); err != nil {
		return nil, dbError(err)
	}
	now := collectedAt
	message := domain.Message{ID: messageID, ConversationID: conversationID, ExternalMessageID: input.ExternalMessageID, SenderDisplayName: input.SenderDisplayName, MessageType: input.MessageType, Content: "", Sensitive: false, ClassificationStatus: "pending", ContentHash: input.ContentHash, ContentVersion: 1, SentAt: input.SentAt, CollectedAt: now, LifecycleStatus: "active", VectorStatus: "pending", Attachments: attachments, CreatedAt: now}
	return &IngestResult{Message: message, Attachments: attachments, Duplicate: duplicate, CursorUpdated: false}, nil
}

func (s *PostgresStore) SharePrivateResources(ctx context.Context, input PrivateShareInput) (*PrivateShareResult, error) {
	messageIDs, attachmentIDs := uniqueSorted(input.MessageIDs), uniqueSorted(input.AttachmentIDs)
	if strings.TrimSpace(input.RequestID) == "" || strings.TrimSpace(input.RequesterUserID) == "" || strings.TrimSpace(input.PrivateConversationID) == "" || strings.TrimSpace(input.OrganizationID) == "" {
		return nil, apperror.New("invalid_request", "request, conversation and organization are required", 400, false)
	}
	if len(messageIDs) == 0 && len(attachmentIDs) == 0 {
		return nil, apperror.New("invalid_request", "at least one message or attachment is required", 400, false)
	}
	fingerprint := shareFingerprint(input.PrivateConversationID, messageIDs, attachmentIDs)
	traceID := strings.TrimSpace(input.TraceID)
	if traceID == "" {
		traceID = trace.TraceID(ctx)
	}
	if traceID == "" {
		traceID = uuid.NewString()
	}
	now := input.Now.UTC()
	if now.IsZero() {
		now = time.Now().UTC()
	}
	tx, err := s.pool.Begin(ctx)
	if err != nil {
		return nil, dbError(err)
	}
	defer tx.Rollback(ctx)
	var owner, conversationType string
	if err = tx.QueryRow(ctx, `SELECT COALESCE(owner_user_id::text,''),conversation_type FROM knowledge.conversation_ingestions WHERE id=$1 AND status<>'detached' FOR UPDATE`, input.PrivateConversationID).Scan(&owner, &conversationType); err != nil {
		if errors.Is(err, pgx.ErrNoRows) {
			return nil, apperror.New("conversation_not_found", "private conversation not found", 404, false)
		}
		return nil, dbError(err)
	}
	if conversationType != "private" || owner != input.RequesterUserID {
		return nil, apperror.Clone(apperror.ErrForbidden)
	}
	var priorFingerprint, priorBatch, priorStatus, priorConversation, priorOrganization string
	var priorMessages, priorAttachments int
	priorErr := tx.QueryRow(ctx, `SELECT request_fingerprint,share_batch_id::text,status,shared_message_count,shared_attachment_count,private_conversation_id::text,organization_id::text FROM knowledge.private_share_requests WHERE requester_user_id=$1 AND request_id=$2`, input.RequesterUserID, input.RequestID).Scan(&priorFingerprint, &priorBatch, &priorStatus, &priorMessages, &priorAttachments, &priorConversation, &priorOrganization)
	if priorErr == nil {
		if priorFingerprint != fingerprint {
			return nil, apperror.New("idempotency_conflict", "request_id was already used with a different selection", 409, false)
		}
		status := "already_processed"
		if priorStatus != "completed" {
			status = "accepted"
		}
		return &PrivateShareResult{RequestID: input.RequestID, ShareBatchID: priorBatch, PrivateConversationID: priorConversation, OrganizationID: priorOrganization, Status: status, SharedMessageCount: priorMessages, SharedAttachmentCount: priorAttachments}, nil
	}
	if !errors.Is(priorErr, pgx.ErrNoRows) {
		return nil, dbError(priorErr)
	}
	var req domain.PrivateShareRequest
	err = tx.QueryRow(ctx, `INSERT INTO knowledge.private_share_requests (id,requester_user_id,request_id,request_fingerprint,private_conversation_id,organization_id,share_batch_id,status) VALUES ($1,$2,$3,$4,$5,$6,$7,'processing') ON CONFLICT (requester_user_id,request_id) DO NOTHING RETURNING id::text,requester_user_id::text,request_id,request_fingerprint,private_conversation_id::text,organization_id::text,share_batch_id::text,status,shared_message_count,shared_attachment_count,COALESCE(last_error,''),created_at,updated_at,completed_at`, uuid.NewString(), input.RequesterUserID, input.RequestID, fingerprint, input.PrivateConversationID, input.OrganizationID, uuid.NewString()).Scan(&req.ID, &req.RequesterUserID, &req.RequestID, &req.RequestFingerprint, &req.PrivateConversationID, &req.OrganizationID, &req.ShareBatchID, &req.Status, &req.SharedMessageCount, &req.SharedAttachmentCount, &req.LastError, &req.CreatedAt, &req.UpdatedAt, &req.CompletedAt)
	if errors.Is(err, pgx.ErrNoRows) {
		if err = tx.QueryRow(ctx, `SELECT request_fingerprint,share_batch_id::text,status,shared_message_count,shared_attachment_count,private_conversation_id::text,organization_id::text FROM knowledge.private_share_requests WHERE requester_user_id=$1 AND request_id=$2`, input.RequesterUserID, input.RequestID).Scan(&req.RequestFingerprint, &req.ShareBatchID, &req.Status, &req.SharedMessageCount, &req.SharedAttachmentCount, &req.PrivateConversationID, &req.OrganizationID); err != nil {
			return nil, dbError(err)
		}
		if req.RequestFingerprint != fingerprint {
			return nil, apperror.New("idempotency_conflict", "request_id was already used with a different selection", 409, false)
		}
		status := "already_processed"
		if req.Status != "completed" {
			status = "accepted"
		}
		return &PrivateShareResult{RequestID: input.RequestID, ShareBatchID: req.ShareBatchID, PrivateConversationID: req.PrivateConversationID, OrganizationID: req.OrganizationID, Status: status, SharedMessageCount: req.SharedMessageCount, SharedAttachmentCount: req.SharedAttachmentCount}, nil
	}
	if err != nil {
		return nil, dbError(err)
	}
	participants := []string{owner}
	participantRows, participantErr := tx.Query(ctx, `SELECT DISTINCT ei.mapped_user_id::text FROM knowledge.conversation_memberships cm JOIN knowledge.external_identities ei ON ei.id=cm.external_identity_id WHERE cm.conversation_ingestion_id=$1 AND cm.status='active' AND ei.mapping_status='mapped' AND ei.mapped_user_id IS NOT NULL`, input.PrivateConversationID)
	if participantErr != nil {
		return nil, dbError(participantErr)
	}
	for participantRows.Next() {
		var participant string
		if scanErr := participantRows.Scan(&participant); scanErr != nil {
			participantRows.Close()
			return nil, dbError(scanErr)
		}
		participants = append(participants, participant)
	}
	if participantErr = participantRows.Err(); participantErr != nil {
		participantRows.Close()
		return nil, dbError(participantErr)
	}
	participantRows.Close()
	participants = uniqueSorted(participants)
	shareKey := input.PrivateConversationID
	if len(participants) >= 2 {
		shareKey = "pair:" + hashText(strings.Join(participants, "|"))
	}
	baseID := ""
	if err = tx.QueryRow(ctx, `WITH inserted AS (INSERT INTO knowledge.knowledge_bases (knowledge_scope,base_type,name,owner_user_id,organization_id,source_key,status) VALUES ('organization','organization_conversation',$1,NULL,$2::uuid,$3,'active') ON CONFLICT DO NOTHING RETURNING id::text) SELECT id FROM inserted UNION ALL SELECT id::text FROM knowledge.knowledge_bases WHERE knowledge_scope='organization' AND base_type='organization_conversation' AND organization_id=$2::uuid AND source_key=$3 AND status='active' LIMIT 1`, "Shared private conversation", input.OrganizationID, "shared_private:"+shareKey).Scan(&baseID); err != nil {
		return nil, dbError(err)
	}
	if err = tx.QueryRow(ctx, `SELECT id::text FROM knowledge.knowledge_bases WHERE id=$1 FOR UPDATE`, baseID).Scan(&baseID); err != nil {
		return nil, dbError(err)
	}
	var existingSharer string
	err = tx.QueryRow(ctx, `SELECT COALESCE(shared_by_user_id::text,'') FROM knowledge.knowledge_items WHERE knowledge_base_id=$1 AND source_type='shared_private_item' AND lifecycle_status='active' LIMIT 1`, baseID).Scan(&existingSharer)
	if err != nil && !errors.Is(err, pgx.ErrNoRows) {
		return nil, dbError(err)
	}
	if err == nil && existingSharer != input.RequesterUserID {
		return nil, apperror.New("private_conversation_already_shared", "private conversation has already been shared", 409, false)
	}
	createShared := func(resourceType, resourceID string) error {
		var sourceItemID, sourceMessageID, sourceAttachmentID, contentType, contentRef, originalRef, contentHash, contentVisibility, securityStatus, sensitivity string
		var version int
		var originalAccess, contentSaved, securityReady bool
		if resourceType == "message" {
			err = tx.QueryRow(ctx, `SELECT ki.id::text,COALESCE(ki.source_message_id::text,''),COALESCE(ki.source_attachment_id::text,''),ki.content_type,ki.content_ref,COALESCE(ki.original_content_ref,''),ki.content_hash,ki.content_version,ki.content_visibility,ki.original_access_required,ki.security_status,COALESCE(ki.sensitivity,''),ki.content_saved,ki.security_ready FROM knowledge.knowledge_items ki JOIN knowledge.messages m ON m.id=ki.source_message_id WHERE m.id=$1 AND ki.conversation_ingestion_id=$2 AND ki.source_type='private_conversation' AND ki.source_attachment_id IS NULL`, resourceID, input.PrivateConversationID).Scan(&sourceItemID, &sourceMessageID, &sourceAttachmentID, &contentType, &contentRef, &originalRef, &contentHash, &version, &contentVisibility, &originalAccess, &securityStatus, &sensitivity, &contentSaved, &securityReady)
		} else {
			err = tx.QueryRow(ctx, `SELECT ki.id::text,COALESCE(ki.source_message_id::text,''),COALESCE(ki.source_attachment_id::text,''),ki.content_type,ki.content_ref,COALESCE(ki.original_content_ref,''),ki.content_hash,ki.content_version,ki.content_visibility,ki.original_access_required,ki.security_status,COALESCE(ki.sensitivity,''),ki.content_saved,ki.security_ready FROM knowledge.knowledge_items ki JOIN knowledge.attachments a ON a.id=ki.source_attachment_id WHERE a.id=$1 AND ki.conversation_ingestion_id=$2 AND ki.source_type='private_conversation'`, resourceID, input.PrivateConversationID).Scan(&sourceItemID, &sourceMessageID, &sourceAttachmentID, &contentType, &contentRef, &originalRef, &contentHash, &version, &contentVisibility, &originalAccess, &securityStatus, &sensitivity, &contentSaved, &securityReady)
		}
		if errors.Is(err, pgx.ErrNoRows) {
			return apperror.New("resource_not_found", "selected resource is not part of the private conversation", 404, false)
		}
		if err != nil {
			return dbError(err)
		}
		if !contentSaved || !securityReady {
			return apperror.New("resource_not_ready", "selected resource is not ready", 409, true)
		}
		var refID string
		err = tx.QueryRow(ctx, `INSERT INTO knowledge.private_share_references (id,organization_id,source_private_resource_id,source_resource_type,source_content_version,share_batch_id,share_request_id,created_by_user_id,status,sensitive,content_access_required) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,'ready',$9,TRUE) ON CONFLICT (organization_id,source_private_resource_id,source_resource_type,source_content_version) DO NOTHING RETURNING id::text`, uuid.NewString(), input.OrganizationID, resourceID, resourceType, version, req.ShareBatchID, input.RequestID, input.RequesterUserID, originalAccess).Scan(&refID)
		if errors.Is(err, pgx.ErrNoRows) {
			return apperror.New("private_conversation_already_shared", "private resource has already been shared", 409, false)
		}
		if err != nil {
			return dbError(err)
		}
		sharedID := uuid.NewString()
		sharedAt := now
		_, err = tx.Exec(ctx, `INSERT INTO knowledge.knowledge_items (id,knowledge_base_id,knowledge_scope,access_scope,owner_user_id,organization_id,conversation_ingestion_id,source_type,source_message_id,source_attachment_id,source_private_item_id,share_request_id,share_batch_id,shared_by_user_id,shared_at,content_type,content_ref,original_content_ref,content_hash,content_version,content_visibility,original_access_required,security_status,sensitivity,content_saved,ownership_ready,security_ready,permission_ready,acl_version,acl_sync_status,processing_status,lifecycle_status) VALUES ($1,$2,'organization','organization_members',NULL,$3,$4,'shared_private_item',NULLIF($5,''),NULLIF($6,''),$7,$8,$9,$10,$11,$12,$13,$14,$15,$16,$17,$18,'not_required',COALESCE(NULLIF($19,''),'internal'),TRUE,TRUE,TRUE,FALSE,0,'pending','pending','active')`, sharedID, baseID, input.OrganizationID, input.PrivateConversationID, sourceMessageID, sourceAttachmentID, sourceItemID, input.RequestID, req.ShareBatchID, input.RequesterUserID, sharedAt, contentType, contentRef, originalRef, contentHash, version, contentVisibility, originalAccess, sensitivity)
		if err != nil {
			return dbError(err)
		}
		payload, _ := json.Marshal(map[string]any{"knowledge_item_id": sharedID, "content_version": version})
		_, err = tx.Exec(ctx, `INSERT INTO knowledge.outbox_events (id,aggregate_type,aggregate_id,event_type,event_version,schema_version,organization_id,trace_id,payload,status,retry_count,available_at) VALUES ($1,'knowledge_item',$2,'permission.sync.requested',$3,1,$4,$5,$6,'pending',0,now()) ON CONFLICT DO NOTHING`, uuid.NewString(), sharedID, version, input.OrganizationID, traceID, payload)
		return dbError(err)
	}
	for _, id := range messageIDs {
		if err = createShared("message", id); err != nil {
			return nil, err
		}
	}
	for _, id := range attachmentIDs {
		if err = createShared("attachment", id); err != nil {
			return nil, err
		}
	}
	if _, err = tx.Exec(ctx, `UPDATE knowledge.private_share_requests SET status='completed',shared_message_count=$2,shared_attachment_count=$3,updated_at=$4,completed_at=$4 WHERE id=$1`, req.ID, len(messageIDs), len(attachmentIDs), now); err != nil {
		return nil, dbError(err)
	}
	if err = tx.Commit(ctx); err != nil {
		return nil, dbError(err)
	}
	return &PrivateShareResult{RequestID: input.RequestID, ShareBatchID: req.ShareBatchID, PrivateConversationID: input.PrivateConversationID, OrganizationID: input.OrganizationID, Status: "accepted", SharedMessageCount: len(messageIDs), SharedAttachmentCount: len(attachmentIDs)}, nil
}

func (s *PostgresStore) CreatePrivateAccessRequest(ctx context.Context, input PrivateAccessRequestInput) (*domain.PrivateAccessRequest, error) {
	if strings.TrimSpace(input.RequesterUserID) == "" || strings.TrimSpace(input.ShareReferenceID) == "" || strings.TrimSpace(input.ResourceID) == "" {
		return nil, apperror.New("invalid_request", "resource and share reference are required", 400, false)
	}
	if input.RequestedAction != "view" && input.RequestedAction != "download" {
		return nil, apperror.New("invalid_request", "requested_action must be view or download", 400, false)
	}
	var sourceID, sourceType, status string
	var required bool
	if err := s.pool.QueryRow(ctx, `SELECT source_private_resource_id::text,source_resource_type,status,content_access_required FROM knowledge.private_share_references WHERE id=$1`, input.ShareReferenceID).Scan(&sourceID, &sourceType, &status, &required); err != nil {
		if errors.Is(err, pgx.ErrNoRows) {
			return nil, apperror.New("resource_not_found", "share reference not found", 404, false)
		}
		return nil, dbError(err)
	}
	if status != "ready" || sourceID != input.ResourceID || sourceType != input.ResourceType {
		return nil, apperror.New("resource_mismatch", "share reference does not match resource", 409, false)
	}
	if !required {
		return nil, apperror.New("approval_not_required", "resource does not require approval", 400, false)
	}
	var out domain.PrivateAccessRequest
	err := s.pool.QueryRow(ctx, `INSERT INTO knowledge.private_access_requests (id,requester_user_id,share_reference_id,resource_id,resource_type,requested_action,reason,status) VALUES ($1,$2,$3,$4,$5,$6,$7,'pending') ON CONFLICT (requester_user_id,share_reference_id,resource_id,requested_action) WHERE status='pending' DO UPDATE SET reason=EXCLUDED.reason RETURNING id::text,requester_user_id::text,share_reference_id::text,resource_id::text,resource_type,requested_action,COALESCE(reason,''),status,COALESCE(reviewed_by_user_id::text,''),COALESCE(review_note,''),created_at,reviewed_at`, uuid.NewString(), input.RequesterUserID, input.ShareReferenceID, input.ResourceID, input.ResourceType, input.RequestedAction, nilString(input.Reason)).Scan(&out.ID, &out.RequesterUserID, &out.ShareReferenceID, &out.ResourceID, &out.ResourceType, &out.RequestedAction, &out.Reason, &out.Status, &out.ReviewedByUserID, &out.ReviewNote, &out.CreatedAt, &out.ReviewedAt)
	return &out, dbError(err)
}

func (s *PostgresStore) ReviewPrivateAccessRequest(ctx context.Context, requestID, reviewerUserID, status, note string, now time.Time) (*domain.PrivateAccessRequest, error) {
	if status != "approved" && status != "rejected" {
		return nil, apperror.New("invalid_request", "review status must be approved or rejected", 400, false)
	}
	var owner string
	if err := s.pool.QueryRow(ctx, `SELECT ci.owner_user_id::text FROM knowledge.private_access_requests r JOIN knowledge.messages m ON r.resource_type='message' AND m.id=r.resource_id JOIN knowledge.conversation_ingestions ci ON ci.id=m.conversation_ingestion_id WHERE r.id=$1 UNION ALL SELECT ci.owner_user_id::text FROM knowledge.private_access_requests r JOIN knowledge.attachments a ON r.resource_type='attachment' AND a.id=r.resource_id JOIN knowledge.conversation_ingestions ci ON ci.id=a.conversation_ingestion_id WHERE r.id=$1`, requestID).Scan(&owner); err != nil {
		if errors.Is(err, pgx.ErrNoRows) {
			return nil, apperror.New("request_not_found", "access request not found", 404, false)
		}
		return nil, dbError(err)
	}
	if owner != reviewerUserID {
		return nil, apperror.Clone(apperror.ErrForbidden)
	}
	var out domain.PrivateAccessRequest
	err := s.pool.QueryRow(ctx, `UPDATE knowledge.private_access_requests SET status=$2,reviewed_by_user_id=$3,review_note=$4,reviewed_at=$5 WHERE id=$1 AND status='pending' RETURNING id::text,requester_user_id::text,share_reference_id::text,resource_id::text,resource_type,requested_action,COALESCE(reason,''),status,COALESCE(reviewed_by_user_id::text,''),COALESCE(review_note,''),created_at,reviewed_at`, requestID, status, reviewerUserID, nilString(note), now).Scan(&out.ID, &out.RequesterUserID, &out.ShareReferenceID, &out.ResourceID, &out.ResourceType, &out.RequestedAction, &out.Reason, &out.Status, &out.ReviewedByUserID, &out.ReviewNote, &out.CreatedAt, &out.ReviewedAt)
	if errors.Is(err, pgx.ErrNoRows) {
		return nil, apperror.New("request_not_found", "access request is not pending", 404, false)
	}
	return &out, dbError(err)
}

type knowledgeItemSource struct {
	ConversationID         string
	ExternalConversationID string
	KnowledgeBaseID        string
	Scope                  string
	OrganizationID         string
	OwnerUserID            string
}

func knowledgeItemOwnership(source knowledgeItemSource) (scope, access, sourceType string, owner, organization any) {
	if source.Scope == "private" {
		return "private", "owner_only", "private_conversation", nilString(source.OwnerUserID), nil
	}
	return "organization", "conversation_members", "platform_conversation", nil, nilString(source.OrganizationID)
}

func ensureMessageKnowledgeItem(ctx context.Context, tx pgx.Tx, source knowledgeItemSource, messageID string, input IngestMessageInput, traceID string) (string, error) {
	var existing string
	err := tx.QueryRow(ctx, `SELECT id::text FROM knowledge.knowledge_items WHERE source_message_id=$1 AND source_attachment_id IS NULL AND source_type<>'shared_private_item'`, messageID).Scan(&existing)
	if err == nil {
		return existing, nil
	}
	if !errors.Is(err, pgx.ErrNoRows) {
		return "", dbError(err)
	}
	scope, access, sourceType, owner, organization := knowledgeItemOwnership(source)
	itemID := uuid.NewString()
	var classified bool
	var sensitive bool
	if err = tx.QueryRow(ctx, `SELECT classification_status='succeeded',sensitive FROM knowledge.messages WHERE id=$1`, messageID).Scan(&classified, &sensitive); err != nil {
		return "", dbError(err)
	}
	securityStatus, sensitivity, visibility := "pending", any(nil), "original"
	if scope == "private" {
		classified, sensitive = true, false
		securityStatus, sensitivity = "not_required", "internal"
	} else if classified {
		securityStatus, sensitivity = "classified", "internal"
		if sensitive {
			sensitivity, visibility = "restricted", "masked"
		}
	}
	err = tx.QueryRow(ctx, `INSERT INTO knowledge.knowledge_items
		(id,knowledge_base_id,knowledge_scope,access_scope,owner_user_id,organization_id,conversation_ingestion_id,source_type,source_message_id,content_type,content_ref,original_content_ref,content_hash,content_version,content_visibility,original_access_required,security_status,sensitivity,content_saved,ownership_ready,security_ready,permission_ready,acl_version,acl_sync_status,processing_status,lifecycle_status)
		VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,'text',$10,$11,$12,1,$13,$14,$15,$16,TRUE,TRUE,$17,FALSE,0,'pending','pending','active')
		ON CONFLICT DO NOTHING RETURNING id::text`, itemID, nilString(source.KnowledgeBaseID), scope, access, owner, organization, source.ConversationID, sourceType, messageID, "message:"+messageID+":display", "message:"+messageID+":original", input.ContentHash, visibility, sensitive, securityStatus, sensitivity, classified).Scan(&itemID)
	inserted := err == nil
	if errors.Is(err, pgx.ErrNoRows) {
		err = tx.QueryRow(ctx, `SELECT id::text FROM knowledge.knowledge_items WHERE source_message_id=$1 AND source_attachment_id IS NULL AND source_type<>'shared_private_item'`, messageID).Scan(&itemID)
	}
	if err != nil {
		return "", dbError(err)
	}
	if inserted {
		payload, _ := json.Marshal(map[string]any{"knowledge_item_id": itemID, "content_version": 1})
		if _, err = tx.Exec(ctx, `INSERT INTO knowledge.outbox_events (id,aggregate_type,aggregate_id,event_type,event_version,trace_id,organization_id,payload) VALUES ($1,'knowledge_item',$2,'permission.sync.requested',1,$3,$4,$5) ON CONFLICT DO NOTHING`, uuid.NewString(), itemID, traceID, nilString(source.OrganizationID), payload); err != nil {
			return "", dbError(err)
		}
	}
	return itemID, nil
}

func ensureAttachmentKnowledgeItem(ctx context.Context, tx pgx.Tx, source knowledgeItemSource, messageID string, attachment domain.Attachment, traceID string) (string, error) {
	var existing string
	err := tx.QueryRow(ctx, `SELECT id::text FROM knowledge.knowledge_items WHERE source_attachment_id=$1 AND source_type<>'shared_private_item'`, attachment.ID).Scan(&existing)
	if err == nil {
		return existing, nil
	}
	if !errors.Is(err, pgx.ErrNoRows) {
		return "", dbError(err)
	}
	scope, access, sourceType, owner, organization := knowledgeItemOwnership(source)
	itemID := uuid.NewString()
	contentType := "file"
	if strings.HasPrefix(strings.ToLower(attachment.MIMEType), "image/") {
		contentType = "image"
	}
	contentHash := attachment.ContentHash
	if contentHash == "" {
		contentHash = hashText(attachment.FileName)
	}
	visibility, sensitivity := "original", "internal"
	if attachment.Sensitive {
		visibility, sensitivity = "metadata_only", "restricted"
	}
	securityStatus := "classified"
	if scope == "private" {
		securityStatus = "not_required"
	}
	contentRef := "attachment:" + attachment.ID
	if attachment.ObjectRef != "" {
		contentRef = attachment.ObjectRef
	}
	err = tx.QueryRow(ctx, `INSERT INTO knowledge.knowledge_items
		(id,knowledge_base_id,knowledge_scope,access_scope,owner_user_id,organization_id,conversation_ingestion_id,source_type,source_message_id,source_attachment_id,content_type,content_ref,original_content_ref,content_hash,content_version,content_visibility,original_access_required,security_status,sensitivity,content_saved,ownership_ready,security_ready,permission_ready,acl_version,acl_sync_status,processing_status,lifecycle_status)
		VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$12,$13,$14,$15,$16,$17,$18,$19,TRUE,TRUE,FALSE,0,'pending','pending','active')
		ON CONFLICT DO NOTHING RETURNING id::text`, itemID, nilString(source.KnowledgeBaseID), scope, access, owner, organization, source.ConversationID, sourceType, messageID, attachment.ID, contentType, contentRef, contentHash, attachment.ContentVersion, visibility, attachment.Sensitive, securityStatus, sensitivity, attachment.ContentStatus == "ready").Scan(&itemID)
	inserted := err == nil
	if errors.Is(err, pgx.ErrNoRows) {
		err = tx.QueryRow(ctx, `SELECT id::text FROM knowledge.knowledge_items WHERE source_attachment_id=$1 AND source_type<>'shared_private_item'`, attachment.ID).Scan(&itemID)
	}
	if err != nil {
		return "", dbError(err)
	}
	if inserted {
		payload, _ := json.Marshal(map[string]any{"knowledge_item_id": itemID, "content_version": attachment.ContentVersion})
		if _, err = tx.Exec(ctx, `INSERT INTO knowledge.outbox_events (id,aggregate_type,aggregate_id,event_type,event_version,trace_id,organization_id,payload) VALUES ($1,'knowledge_item',$2,'permission.sync.requested',$3,$4,$5,$6) ON CONFLICT DO NOTHING`, uuid.NewString(), itemID, attachment.ContentVersion, traceID, nilString(source.OrganizationID), payload); err != nil {
			return "", dbError(err)
		}
	}
	return itemID, nil
}

func (s *PostgresStore) Heartbeat(ctx context.Context, collectorID string, _ time.Time) (*domain.Collector, error) {
	current, err := s.GetCollector(ctx, collectorID)
	if err != nil {
		return nil, err
	}
	// Heartbeat proves Agent liveness only. Collection success is recorded by
	// AdvanceCursor after messages and attachments have been persisted.
	return current, nil
}

func (s *PostgresStore) RecordCollectorFailure(ctx context.Context, collectorID, lastError string, nextPollAt, now time.Time) error {
	tag, err := s.pool.Exec(ctx, `UPDATE knowledge.conversation_collectors SET last_attempt_at=$2,next_poll_at=$3,consecutive_failures=consecutive_failures+1,last_error=$4 WHERE id=$1 AND status<>'removed'`, collectorID, now, nilTime(nextPollAt), nilString(lastError))
	if err != nil {
		return dbError(err)
	}
	if tag.RowsAffected() == 0 {
		return apperror.New("collector_not_found", "collector not found", 404, false)
	}
	return nil
}

func (s *PostgresStore) RecordCursorReceipt(ctx context.Context, collectorID, cursor string, now time.Time) error {
	cursor = strings.TrimSpace(cursor)
	if cursor == "" {
		return apperror.New("invalid_cursor", "cursor is required", 400, false)
	}
	tag, err := s.pool.Exec(ctx, `INSERT INTO knowledge.collector_cursor_receipts (collector_id,cursor,created_at)
		SELECT id,$2,$3 FROM knowledge.conversation_collectors WHERE id=$1 AND status='active'
		ON CONFLICT (collector_id,cursor) DO NOTHING`, collectorID, cursor, now)
	if err != nil {
		return dbError(err)
	}
	if tag.RowsAffected() == 0 {
		var exists bool
		if err := s.pool.QueryRow(ctx, `SELECT EXISTS (SELECT 1 FROM knowledge.collector_cursor_receipts WHERE collector_id=$1 AND cursor=$2)`, collectorID, cursor).Scan(&exists); err != nil {
			return dbError(err)
		}
		if !exists {
			return apperror.New("collector_revoked", "collector is not active", 403, false)
		}
	}
	return nil
}

func (s *PostgresStore) AdvanceCursor(ctx context.Context, collectorID, cursor string, now time.Time) error {
	tx, err := s.pool.Begin(ctx)
	if err != nil {
		return dbError(err)
	}
	defer tx.Rollback(ctx)
	if strings.TrimSpace(cursor) == "" {
		return apperror.New("cursor_unverified", "cursor has no successful message and attachment receipt", 409, false)
	}
	var receipt bool
	err = tx.QueryRow(ctx, `SELECT EXISTS (
		SELECT 1 FROM knowledge.collector_cursor_receipts cr
		WHERE cr.collector_id=$1 AND cr.cursor=$2
	) OR (EXISTS (
		SELECT 1 FROM knowledge.message_sources ms
		WHERE ms.collector_id=$1 AND COALESCE(NULLIF(ms.ingest_cursor,''),ms.raw_payload_ref)=$2
	) AND NOT EXISTS (
		SELECT 1
		FROM knowledge.message_sources ms
		JOIN knowledge.attachments a ON a.message_id=ms.message_id
		WHERE ms.collector_id=$1 AND COALESCE(NULLIF(ms.ingest_cursor,''),ms.raw_payload_ref)=$2 AND a.content_status <> 'ready'
	))`, collectorID, cursor).Scan(&receipt)
	if err != nil {
		return dbError(err)
	}
	if !receipt {
		return apperror.New("cursor_unverified", "cursor has no successful message and attachment receipt", 409, false)
	}
	var conversationID, current string
	err = tx.QueryRow(ctx, `SELECT conversation_ingestion_id::text,COALESCE(last_cursor,'') FROM knowledge.conversation_collectors WHERE id=$1 AND status='active' FOR UPDATE`, collectorID).Scan(&conversationID, &current)
	if errors.Is(err, pgx.ErrNoRows) {
		return apperror.New("collector_revoked", "collector is not active", 403, false)
	}
	if err != nil {
		return dbError(err)
	}
	if !cursorShouldAdvance(current, cursor) {
		cursor = current
	}
	if _, err = tx.Exec(ctx, `UPDATE knowledge.conversation_collectors SET last_cursor=$2,last_attempt_at=$3,last_success_at=$3,next_poll_at=NULL,consecutive_failures=0,last_error=NULL,updated_at=$3 WHERE id=$1 AND status='active'`, collectorID, nilString(cursor), now); err != nil {
		return dbError(err)
	}
	if _, err = tx.Exec(ctx, `UPDATE knowledge.conversation_ingestions SET last_synced_at=$2,updated_at=$2 WHERE id=$1`, conversationID, now); err != nil {
		return dbError(err)
	}
	return dbError(tx.Commit(ctx))
}

const attachmentColumns = `id::text,conversation_ingestion_id::text,COALESCE(message_id::text,''),external_attachment_id,file_name,COALESCE(mime_type,''),size_bytes,COALESCE(object_ref,''),COALESCE(content_hash,''),content_version,content_status,access_scope,content_access_required,COALESCE(preview_capability,''),COALESCE(last_error,''),created_at,updated_at,sensitive,classification_status`

func scanAttachment(row rowScanner) (*domain.Attachment, error) {
	var a domain.Attachment
	err := row.Scan(&a.ID, &a.ConversationID, &a.MessageID, &a.ExternalAttachmentID, &a.FileName, &a.MIMEType, &a.SizeBytes, &a.ObjectRef, &a.ContentHash, &a.ContentVersion, &a.ContentStatus, &a.AccessScope, &a.ContentAccessRequired, &a.PreviewCapability, &a.LastError, &a.CreatedAt, &a.UpdatedAt, &a.Sensitive, &a.ClassificationStatus)
	return &a, err
}
func (s *PostgresStore) GetAttachment(ctx context.Context, id string) (*domain.Attachment, error) {
	a, err := scanAttachment(s.pool.QueryRow(ctx, `SELECT `+attachmentColumns+` FROM knowledge.attachments WHERE id=$1`, id))
	if errors.Is(err, pgx.ErrNoRows) {
		return nil, apperror.New("attachment_not_found", "attachment not found", 404, false)
	}
	return a, dbError(err)
}
func (s *PostgresStore) CompleteAttachment(ctx context.Context, id, objectRef, contentHash string, size int64, status string) (*domain.Attachment, error) {
	tx, err := s.pool.Begin(ctx)
	if err != nil {
		return nil, dbError(err)
	}
	defer tx.Rollback(ctx)
	a, err := scanAttachment(tx.QueryRow(ctx, `UPDATE knowledge.attachments SET object_ref=$2,content_hash=$3,size_bytes=$4,content_status=$5,last_error=NULL,updated_at=now() WHERE id=$1 AND content_status<>'ready' AND (content_hash IS NULL OR content_hash=$3) RETURNING `+attachmentColumns, id, objectRef, contentHash, size, status))
	if errors.Is(err, pgx.ErrNoRows) {
		existing, lookupErr := scanAttachment(tx.QueryRow(ctx, `SELECT `+attachmentColumns+` FROM knowledge.attachments WHERE id=$1`, id))
		if lookupErr == nil && existing.ContentStatus == "ready" && strings.EqualFold(existing.ContentHash, contentHash) {
			return existing, nil
		}
		return nil, apperror.New("attachment_hash_mismatch", "attachment hash does not match metadata", 400, false)
	}
	if err != nil {
		return nil, dbError(err)
	}
	if status == "ready" {
		if _, updateErr := tx.Exec(ctx, `UPDATE knowledge.knowledge_items SET content_saved=TRUE,content_ref=$2,original_content_ref=$2,content_hash=$3,last_error=NULL,updated_at=now() WHERE source_attachment_id=$1`, a.ID, objectRef, contentHash); updateErr != nil {
			return nil, dbError(updateErr)
		}
	}
	if err := tx.Commit(ctx); err != nil {
		return nil, dbError(err)
	}
	return a, nil
}

func (s *PostgresStore) ListPendingMessages(ctx context.Context, limit int) ([]PendingMessage, error) {
	if limit <= 0 || limit > 200 {
		limit = 200
	}
	rows, err := s.pool.Query(ctx, `SELECT m.id::text,m.conversation_ingestion_id::text,m.external_message_id,COALESCE(m.sender_identity_id::text,''),COALESCE(m.sender_display_name,''),m.message_type,COALESCE(m.normalized_content_ref,''),COALESCE(m.normalized_content,''),m.content_hash,m.content_version,m.sent_at,COALESCE((SELECT MIN(ms.collected_at) FROM knowledge.message_sources ms WHERE ms.message_id=m.id),m.created_at),m.lifecycle_status,m.vector_status,m.created_at,m.sensitive,m.classification_status,p.content FROM knowledge.messages m JOIN knowledge.message_private_content p ON p.message_id=m.id WHERE m.classification_status='pending' ORDER BY m.created_at LIMIT $1`, limit)
	if err != nil {
		return nil, dbError(err)
	}
	defer rows.Close()
	out := []PendingMessage{}
	for rows.Next() {
		var m domain.Message
		var raw string
		if err := rows.Scan(&m.ID, &m.ConversationID, &m.ExternalMessageID, &m.SenderIdentityID, &m.SenderDisplayName, &m.MessageType, &m.NormalizedContentRef, &m.Content, &m.ContentHash, &m.ContentVersion, &m.SentAt, &m.CollectedAt, &m.LifecycleStatus, &m.VectorStatus, &m.CreatedAt, &m.Sensitive, &m.ClassificationStatus, &raw); err != nil {
			return nil, dbError(err)
		}
		out = append(out, PendingMessage{Message: m, OriginalContent: raw})
	}
	return out, dbError(rows.Err())
}

func (s *PostgresStore) CompleteMessageClassification(ctx context.Context, messageID, displayContent string, sensitive bool) error {
	tx, err := s.pool.Begin(ctx)
	if err != nil {
		return dbError(err)
	}
	defer tx.Rollback(ctx)
	var conversationID string
	if err = tx.QueryRow(ctx, `UPDATE knowledge.messages SET normalized_content=$2,sensitive=$3,classification_status='succeeded' WHERE id=$1 AND classification_status='pending' RETURNING conversation_ingestion_id::text`, messageID, displayContent, sensitive).Scan(&conversationID); err != nil {
		if errors.Is(err, pgx.ErrNoRows) {
			return apperror.New("message_not_found", "message is not pending", 404, false)
		}
		return dbError(err)
	}
	visibility, sensitivity := "original", "internal"
	if sensitive {
		visibility, sensitivity = "masked", "restricted"
	}
	if _, err = tx.Exec(ctx, `UPDATE knowledge.knowledge_items SET security_status='classified',security_ready=TRUE,sensitivity=$2,content_visibility=$3,original_access_required=$4,last_error=NULL,updated_at=now() WHERE source_message_id=$1 AND source_attachment_id IS NULL`, messageID, sensitivity, visibility, sensitive); err != nil {
		return dbError(err)
	}
	return dbError(tx.Commit(ctx))
}
func (s *PostgresStore) FailAttachment(ctx context.Context, id, message string) error {
	_, err := s.pool.Exec(ctx, `UPDATE knowledge.attachments SET content_status='failed',last_error=$2,updated_at=now() WHERE id=$1`, id, message)
	return dbError(err)
}
func (s *PostgresStore) ListMessages(ctx context.Context, conversationID string, limit int, before string) ([]domain.Message, error) {
	if limit <= 0 || limit > 200 {
		limit = 200
	}
	before = strings.TrimSpace(before)
	cutoff, parseErr := parseBeforeTime(before)
	var cutoffID string
	if parseErr != nil && before != "" {
		lookupErr := s.pool.QueryRow(ctx, `SELECT sent_at,id::text FROM knowledge.messages WHERE conversation_ingestion_id=$1 AND (id::text=$2 OR external_message_id=$2) LIMIT 1`, conversationID, before).Scan(&cutoff, &cutoffID)
		if errors.Is(lookupErr, pgx.ErrNoRows) {
			return nil, parseErr
		}
		if lookupErr != nil {
			return nil, dbError(lookupErr)
		}
	}
	query := `SELECT m.id::text,m.conversation_ingestion_id::text,m.external_message_id,COALESCE(m.sender_identity_id::text,''),COALESCE(NULLIF(ei.display_name,''),NULLIF(m.sender_display_name,''),''),m.message_type,COALESCE(m.normalized_content_ref,''),COALESCE(m.normalized_content,''),m.content_hash,m.content_version,m.sent_at,COALESCE((SELECT MIN(ms.collected_at) FROM knowledge.message_sources ms WHERE ms.message_id=m.id),m.created_at),m.lifecycle_status,m.vector_status,m.created_at,m.sensitive,m.classification_status FROM knowledge.messages m LEFT JOIN knowledge.external_identities ei ON ei.id=m.sender_identity_id WHERE m.conversation_ingestion_id=$1`
	args := []any{conversationID}
	if !cutoff.IsZero() {
		if cutoffID != "" {
			query += ` AND (sent_at < $2 OR (sent_at = $2 AND id::text < $3))`
			args = append(args, cutoff, cutoffID)
		} else {
			query += ` AND sent_at < $2`
			args = append(args, cutoff)
		}
	}
	query += fmt.Sprintf(` ORDER BY sent_at DESC,id DESC LIMIT $%d`, len(args)+1)
	args = append(args, limit)
	rows, err := s.pool.Query(ctx, query, args...)
	if err != nil {
		return nil, dbError(err)
	}
	defer rows.Close()
	out := []domain.Message{}
	for rows.Next() {
		var m domain.Message
		if err := rows.Scan(&m.ID, &m.ConversationID, &m.ExternalMessageID, &m.SenderIdentityID, &m.SenderDisplayName, &m.MessageType, &m.NormalizedContentRef, &m.Content, &m.ContentHash, &m.ContentVersion, &m.SentAt, &m.CollectedAt, &m.LifecycleStatus, &m.VectorStatus, &m.CreatedAt, &m.Sensitive, &m.ClassificationStatus); err != nil {
			return nil, dbError(err)
		}
		attachments, attachmentErr := s.ListAttachmentsForMessage(ctx, m.ID)
		if attachmentErr != nil {
			return nil, attachmentErr
		}
		m.Attachments = attachments
		out = append(out, m)
	}
	if err := rows.Err(); err != nil {
		return nil, dbError(err)
	}
	// The query takes the newest window, while the public response remains in
	// chronological order for the conversation UI and for deterministic tests.
	for left, right := 0, len(out)-1; left < right; left, right = left+1, right-1 {
		out[left], out[right] = out[right], out[left]
	}
	return out, nil
}
func (s *PostgresStore) ListAttachmentsForMessage(ctx context.Context, messageID string) ([]domain.Attachment, error) {
	rows, err := s.pool.Query(ctx, `SELECT `+attachmentColumns+` FROM knowledge.attachments WHERE message_id=$1 ORDER BY created_at`, messageID)
	if err != nil {
		return nil, dbError(err)
	}
	defer rows.Close()
	out := []domain.Attachment{}
	for rows.Next() {
		a, scanErr := scanAttachment(rows)
		if scanErr != nil {
			return nil, dbError(scanErr)
		}
		out = append(out, *a)
	}
	return out, dbError(rows.Err())
}
func (s *PostgresStore) ListAttachments(ctx context.Context, conversationID string) ([]domain.Attachment, error) {
	rows, err := s.pool.Query(ctx, `SELECT `+attachmentColumns+` FROM knowledge.attachments WHERE conversation_ingestion_id=$1 ORDER BY created_at`, conversationID)
	if err != nil {
		return nil, dbError(err)
	}
	defer rows.Close()
	out := []domain.Attachment{}
	for rows.Next() {
		a, scanErr := scanAttachment(rows)
		if scanErr != nil {
			return nil, dbError(scanErr)
		}
		out = append(out, *a)
	}
	return out, dbError(rows.Err())
}

func (s *PostgresStore) ListContactIdentities(ctx context.Context, userID, platform string) ([]ExternalIdentity, error) {
	query := `SELECT id::text,platform,platform_workspace_key,external_user_id,COALESCE(display_name,''),COALESCE(avatar_url,''),COALESCE(mapped_user_id::text,''),mapping_status FROM knowledge.external_identities WHERE mapped_user_id=$1`
	args := []any{userID}
	if platform != "" {
		query += ` AND platform=$2`
		args = append(args, platform)
	}
	rows, err := s.pool.Query(ctx, query, args...)
	if err != nil {
		return nil, dbError(err)
	}
	defer rows.Close()
	out := []ExternalIdentity{}
	for rows.Next() {
		var v ExternalIdentity
		if err := rows.Scan(&v.ID, &v.Platform, &v.WorkspaceKey, &v.ExternalUserID, &v.DisplayName, &v.AvatarURL, &v.MappedUserID, &v.MappingStatus); err != nil {
			return nil, dbError(err)
		}
		out = append(out, v)
	}
	return out, dbError(rows.Err())
}

func (s *PostgresStore) ListContactMemberships(ctx context.Context, userID string) ([]ContactMembership, error) {
	rows, err := s.pool.Query(ctx, `SELECT ei.id::text,ei.platform,ei.platform_workspace_key,ei.external_user_id,COALESCE(ei.display_name,''),COALESCE(ei.avatar_url,''),COALESCE(ei.mapped_user_id::text,''),ei.mapping_status,ci.id::text FROM knowledge.conversation_memberships cm JOIN knowledge.external_identities ei ON ei.id=cm.external_identity_id JOIN knowledge.conversation_ingestions ci ON ci.id=cm.conversation_ingestion_id LEFT JOIN knowledge.conversation_collectors cc ON cc.conversation_ingestion_id=ci.id AND cc.collector_user_id=$1 AND cc.status <> 'removed' WHERE cm.status='active' AND (ci.owner_user_id=$1 OR cc.id IS NOT NULL)`, userID)
	if err != nil {
		return nil, dbError(err)
	}
	defer rows.Close()
	out := []ContactMembership{}
	for rows.Next() {
		var v ContactMembership
		if err := rows.Scan(&v.Identity.ID, &v.Identity.Platform, &v.Identity.WorkspaceKey, &v.Identity.ExternalUserID, &v.Identity.DisplayName, &v.Identity.AvatarURL, &v.Identity.MappedUserID, &v.Identity.MappingStatus, &v.ConversationID); err != nil {
			return nil, dbError(err)
		}
		out = append(out, v)
	}
	return out, dbError(rows.Err())
}

const knowledgeItemColumns = `ki.id::text,COALESCE(ki.knowledge_base_id::text,''),ki.knowledge_scope,ki.access_scope,COALESCE(ki.owner_user_id::text,''),COALESCE(ki.organization_id::text,''),COALESCE(ki.conversation_ingestion_id::text,''),COALESCE(ci.external_conversation_id,''),ki.source_type,COALESCE(ki.source_message_id::text,''),COALESCE(ki.source_attachment_id::text,''),COALESCE(ki.source_private_item_id::text,''),COALESCE(ki.share_request_id,''),COALESCE(ki.share_batch_id::text,''),COALESCE(ki.shared_by_user_id::text,''),ki.shared_at,ki.content_type,ki.content_ref,COALESCE(ki.original_content_ref,''),ki.content_hash,ki.content_version,ki.content_visibility,ki.original_access_required,ki.security_status,COALESCE(ki.sensitivity,''),ki.content_saved,ki.ownership_ready,ki.security_ready,ki.permission_ready,ki.acl_version,ki.acl_sync_status,ki.processing_status,ki.lifecycle_status,ki.original_access_required,COALESCE(ki.last_error,''),ki.created_at,ki.updated_at`

func scanKnowledgeItem(row rowScanner) (*domain.KnowledgeItem, error) {
	var item domain.KnowledgeItem
	err := row.Scan(&item.ID, &item.KnowledgeBaseID, &item.KnowledgeScope, &item.AccessScope, &item.OwnerUserID, &item.OrganizationID, &item.ConversationID, &item.ExternalConversationID, &item.SourceType, &item.SourceMessageID, &item.SourceAttachmentID, &item.SourcePrivateItemID, &item.ShareRequestID, &item.ShareBatchID, &item.SharedByUserID, &item.SharedAt, &item.ContentType, &item.ContentRef, &item.OriginalContentRef, &item.ContentHash, &item.ContentVersion, &item.ContentVisibility, &item.OriginalAccessRequired, &item.SecurityStatus, &item.Sensitivity, &item.ContentSaved, &item.OwnershipReady, &item.SecurityReady, &item.PermissionReady, &item.ACLVersion, &item.ACLSyncStatus, &item.ProcessingStatus, &item.LifecycleStatus, &item.ContentAccessRequired, &item.LastError, &item.CreatedAt, &item.UpdatedAt)
	return &item, err
}

func (s *PostgresStore) ListPendingKnowledgePermissions(ctx context.Context, limit int) ([]domain.KnowledgeItem, error) {
	if limit <= 0 || limit > 200 {
		limit = 200
	}
	rows, err := s.pool.Query(ctx, `SELECT `+knowledgeItemColumns+` FROM knowledge.knowledge_items ki LEFT JOIN knowledge.conversation_ingestions ci ON ci.id=ki.conversation_ingestion_id WHERE ki.lifecycle_status='active' AND ((ki.permission_ready=FALSE AND ki.acl_sync_status IN ('pending','failed')) OR (ki.permission_ready=TRUE AND ki.acl_sync_status='synced' AND ki.processing_status='pending')) ORDER BY ki.updated_at LIMIT $1`, limit)
	if err != nil {
		return nil, dbError(err)
	}
	defer rows.Close()
	out := make([]domain.KnowledgeItem, 0)
	for rows.Next() {
		item, scanErr := scanKnowledgeItem(rows)
		if scanErr != nil {
			return nil, dbError(scanErr)
		}
		out = append(out, *item)
	}
	return out, dbError(rows.Err())
}

func (s *PostgresStore) ListKnowledgePermissionSubjects(ctx context.Context, id string) ([]string, error) {
	rows, err := s.pool.Query(ctx, `SELECT DISTINCT subject FROM (SELECT ei.mapped_user_id::text AS subject FROM knowledge.knowledge_items ki JOIN knowledge.conversation_memberships cm ON cm.conversation_ingestion_id=ki.conversation_ingestion_id AND cm.status='active' JOIN knowledge.external_identities ei ON ei.id=cm.external_identity_id AND ei.mapping_status='mapped' AND ei.mapped_user_id IS NOT NULL WHERE ki.id=$1 UNION ALL SELECT ci.owner_user_id::text FROM knowledge.knowledge_items ki JOIN knowledge.conversation_ingestions ci ON ci.id=ki.conversation_ingestion_id WHERE ki.id=$1 AND ci.owner_user_id IS NOT NULL UNION ALL SELECT shared_by_user_id::text FROM knowledge.knowledge_items WHERE id=$1 AND shared_by_user_id IS NOT NULL) subjects ORDER BY subject`, id)
	if err != nil {
		return nil, dbError(err)
	}
	defer rows.Close()
	out := make([]string, 0)
	for rows.Next() {
		var userID string
		if err := rows.Scan(&userID); err != nil {
			return nil, dbError(err)
		}
		out = append(out, userID)
	}
	return out, dbError(rows.Err())
}

func (s *PostgresStore) MarkKnowledgePermissionSynced(ctx context.Context, id string, aclVersion int64) error {
	if aclVersion < 1 {
		return apperror.New("invalid_acl_version", "acl_version must be positive", 400, false)
	}
	tx, err := s.pool.Begin(ctx)
	if err != nil {
		return dbError(err)
	}
	defer tx.Rollback(ctx)
	tag, err := tx.Exec(ctx, `UPDATE knowledge.knowledge_items SET permission_ready=TRUE,acl_version=$2,acl_sync_status='synced',last_error=NULL,updated_at=now() WHERE id=$1`, id, aclVersion)
	if err != nil {
		return dbError(err)
	}
	if tag.RowsAffected() == 0 {
		return apperror.New("knowledge_not_found", "knowledge item not found", 404, false)
	}
	if _, err = tx.Exec(ctx, `UPDATE knowledge.outbox_events SET status='published',published_at=COALESCE(published_at,now()),last_error=NULL WHERE aggregate_id=$1 AND event_type='permission.sync.requested'`, id); err != nil {
		return dbError(err)
	}
	return dbError(tx.Commit(ctx))
}

func (s *PostgresStore) MarkKnowledgePermissionFailed(ctx context.Context, id, failure string) error {
	tx, err := s.pool.Begin(ctx)
	if err != nil {
		return dbError(err)
	}
	defer tx.Rollback(ctx)
	tag, err := tx.Exec(ctx, `UPDATE knowledge.knowledge_items SET permission_ready=FALSE,acl_sync_status='failed',last_error=$2,updated_at=now() WHERE id=$1`, id, safeError(failure))
	if err != nil {
		return dbError(err)
	}
	if tag.RowsAffected() == 0 {
		return apperror.New("knowledge_not_found", "knowledge item not found", 404, false)
	}
	if _, err = tx.Exec(ctx, `UPDATE knowledge.outbox_events SET status='failed',retry_count=retry_count+1,last_error=$2,available_at=now()+interval '30 seconds',published_at=NULL WHERE aggregate_id=$1 AND event_type='permission.sync.requested'`, id, safeError(failure)); err != nil {
		return dbError(err)
	}
	return dbError(tx.Commit(ctx))
}

func (s *PostgresStore) TryMarkKnowledgeReady(ctx context.Context, id, traceID string) (bool, error) {
	if strings.TrimSpace(traceID) == "" {
		traceID = trace.TraceID(ctx)
	}
	if strings.TrimSpace(traceID) == "" {
		traceID = uuid.NewString()
	}
	tx, err := s.pool.Begin(ctx)
	if err != nil {
		return false, dbError(err)
	}
	defer tx.Rollback(ctx)
	item, err := scanKnowledgeItem(tx.QueryRow(ctx, `SELECT `+knowledgeItemColumns+` FROM knowledge.knowledge_items ki LEFT JOIN knowledge.conversation_ingestions ci ON ci.id=ki.conversation_ingestion_id WHERE ki.id=$1 FOR UPDATE OF ki`, id))
	if errors.Is(err, pgx.ErrNoRows) {
		return false, apperror.New("knowledge_not_found", "knowledge item not found", 404, false)
	}
	if err != nil {
		return false, dbError(err)
	}
	if item.ProcessingStatus == "processing" || item.ProcessingStatus == "ready" {
		return false, nil
	}
	if !item.ContentSaved || !item.OwnershipReady || !item.SecurityReady || !item.PermissionReady || item.ACLSyncStatus != "synced" {
		return false, nil
	}
	resourceType := "message"
	if item.SourceAttachmentID != "" {
		resourceType = "attachment"
	}
	payload, _ := json.Marshal(map[string]any{
		"resource_type": resourceType, "resource_id": item.ID, "knowledge_item_id": item.ID,
		"source_message_id": item.SourceMessageID, "source_attachment_id": item.SourceAttachmentID,
		"content_version": item.ContentVersion, "acl_version": item.ACLVersion,
		"content_variant": "display", "content_access_required": item.ContentAccessRequired,
	})
	if _, err = tx.Exec(ctx, `UPDATE knowledge.knowledge_items SET processing_status='ready',last_error=NULL,updated_at=now() WHERE id=$1`, id); err != nil {
		return false, dbError(err)
	}
	tag, err := tx.Exec(ctx, `INSERT INTO knowledge.outbox_events (id,aggregate_type,aggregate_id,event_type,event_version,schema_version,organization_id,trace_id,payload,status,retry_count,available_at) VALUES ($1,'knowledge_item',$2,'knowledge.ready',$3,1,$4,$5,$6,'pending',0,now()) ON CONFLICT DO NOTHING`, uuid.NewString(), id, item.ContentVersion, nilString(item.OrganizationID), traceID, payload)
	if err != nil {
		return false, dbError(err)
	}
	if err = tx.Commit(ctx); err != nil {
		return false, dbError(err)
	}
	return tag.RowsAffected() == 1, nil
}

func (s *PostgresStore) GetKnowledgeItem(ctx context.Context, id string) (*domain.KnowledgeItem, error) {
	item, err := scanKnowledgeItem(s.pool.QueryRow(ctx, `SELECT `+knowledgeItemColumns+` FROM knowledge.knowledge_items ki LEFT JOIN knowledge.conversation_ingestions ci ON ci.id=ki.conversation_ingestion_id WHERE ki.id=$1`, id))
	if errors.Is(err, pgx.ErrNoRows) {
		return nil, apperror.New("knowledge_not_found", "knowledge item not found", 404, false)
	}
	if err != nil {
		return nil, dbError(err)
	}
	if item.SourceMessageID != "" {
		var message domain.Message
		err = s.pool.QueryRow(ctx, `SELECT m.id::text,m.conversation_ingestion_id::text,m.external_message_id,COALESCE(m.sender_identity_id::text,''),COALESCE(NULLIF(ei.display_name,''),NULLIF(m.sender_display_name,''),''),m.message_type,COALESCE(m.normalized_content_ref,''),COALESCE(m.normalized_content,''),m.content_hash,m.content_version,m.sent_at,COALESCE(MIN(ms.collected_at),m.created_at),m.lifecycle_status,m.vector_status,m.created_at,m.sensitive,m.classification_status FROM knowledge.messages m LEFT JOIN knowledge.external_identities ei ON ei.id=m.sender_identity_id LEFT JOIN knowledge.message_sources ms ON ms.message_id=m.id WHERE m.id=$1 GROUP BY m.id,ei.display_name`, item.SourceMessageID).Scan(&message.ID, &message.ConversationID, &message.ExternalMessageID, &message.SenderIdentityID, &message.SenderDisplayName, &message.MessageType, &message.NormalizedContentRef, &message.Content, &message.ContentHash, &message.ContentVersion, &message.SentAt, &message.CollectedAt, &message.LifecycleStatus, &message.VectorStatus, &message.CreatedAt, &message.Sensitive, &message.ClassificationStatus)
		if err != nil && !errors.Is(err, pgx.ErrNoRows) {
			return nil, dbError(err)
		}
		if err == nil {
			item.Message = &message
		}
	}
	if item.SourceAttachmentID != "" {
		attachment, attachmentErr := s.GetAttachment(ctx, item.SourceAttachmentID)
		if attachmentErr != nil {
			return nil, attachmentErr
		}
		item.Attachment = attachment
	}
	return item, nil
}

func (s *PostgresStore) GetKnowledgeItemByMessage(ctx context.Context, messageID string) (*domain.KnowledgeItem, error) {
	var id string
	err := s.pool.QueryRow(ctx, `SELECT id::text FROM knowledge.knowledge_items WHERE source_message_id=$1 AND source_attachment_id IS NULL AND source_type<>'shared_private_item'`, messageID).Scan(&id)
	if errors.Is(err, pgx.ErrNoRows) {
		return nil, apperror.New("knowledge_not_found", "knowledge item not found", 404, false)
	}
	if err != nil {
		return nil, dbError(err)
	}
	return s.GetKnowledgeItem(ctx, id)
}

func (s *PostgresStore) GetKnowledgeItemByAttachment(ctx context.Context, attachmentID string) (*domain.KnowledgeItem, error) {
	var id string
	err := s.pool.QueryRow(ctx, `SELECT id::text FROM knowledge.knowledge_items WHERE source_attachment_id=$1 ORDER BY CASE WHEN source_type='shared_private_item' THEN 0 ELSE 1 END LIMIT 1`, attachmentID).Scan(&id)
	if errors.Is(err, pgx.ErrNoRows) {
		return nil, apperror.New("knowledge_not_found", "knowledge item not found", 404, false)
	}
	if err != nil {
		return nil, dbError(err)
	}
	return s.GetKnowledgeItem(ctx, id)
}

func (s *PostgresStore) GetKnowledgeContent(ctx context.Context, id string) (*domain.KnowledgeContent, error) {
	var content domain.KnowledgeContent
	err := s.pool.QueryRow(ctx, `SELECT ki.id::text,ki.content_version,'display',ki.content_hash,COALESCE(m.normalized_content,'') FROM knowledge.knowledge_items ki JOIN knowledge.messages m ON m.id=ki.source_message_id WHERE ki.id=$1 AND ki.source_attachment_id IS NULL`, id).Scan(&content.KnowledgeItemID, &content.ContentVersion, &content.ContentVariant, &content.ContentHash, &content.Text)
	if errors.Is(err, pgx.ErrNoRows) {
		return nil, apperror.New("knowledge_content_not_found", "knowledge content not found", 404, false)
	}
	return &content, dbError(err)
}

func (s *PostgresStore) GetOutbox(ctx context.Context, limit int) ([]domain.OutboxEvent, error) {
	if limit <= 0 || limit > 100 {
		limit = 100
	}
	rows, err := s.pool.Query(ctx, `SELECT id::text,event_type,schema_version,created_at,COALESCE(trace_id,''),COALESCE(organization_id::text,''),'module-2',payload,retry_count,COALESCE(last_error,''),available_at,published_at FROM knowledge.outbox_events WHERE event_type='knowledge.ready' AND published_at IS NULL AND status IN ('pending','failed') AND available_at<=now() ORDER BY created_at LIMIT $1`, limit)
	if err != nil {
		return nil, dbError(err)
	}
	defer rows.Close()
	out := []domain.OutboxEvent{}
	for rows.Next() {
		var e domain.OutboxEvent
		var raw []byte
		if err := rows.Scan(&e.ID, &e.EventType, &e.SchemaVersion, &e.OccurredAt, &e.TraceID, &e.OrganizationID, &e.Producer, &raw, &e.RetryCount, &e.LastError, &e.AvailableAt, &e.PublishedAt); err != nil {
			return nil, dbError(err)
		}
		_ = json.Unmarshal(raw, &e.Payload)
		out = append(out, e)
	}
	sort.Slice(out, func(i, j int) bool { return out[i].OccurredAt.Before(out[j].OccurredAt) })
	return out, dbError(rows.Err())
}
func (s *PostgresStore) MarkOutboxPublished(ctx context.Context, id string, publishedAt time.Time) error {
	_, err := s.pool.Exec(ctx, `UPDATE knowledge.outbox_events SET status='published',published_at=$2,last_error=NULL WHERE id=$1 AND event_type='knowledge.ready'`, id, publishedAt)
	return dbError(err)
}

func (s *PostgresStore) MarkOutboxFailed(ctx context.Context, id, failure string, availableAt time.Time) error {
	_, err := s.pool.Exec(ctx, `UPDATE knowledge.outbox_events SET status='failed',retry_count=retry_count+1,publish_attempts=COALESCE(publish_attempts,0)+1,last_error=$2,available_at=$3 WHERE id=$1 AND event_type='knowledge.ready' AND published_at IS NULL`, id, safeError(failure), availableAt.UTC())
	return dbError(err)
}

func dbError(err error) error {
	if err == nil {
		return nil
	}
	var appErr *apperror.Error
	if errors.As(err, &appErr) {
		return appErr
	}
	log.Printf("knowledge database error: %v", err)
	return apperror.Wrap("database_error", "knowledge database operation failed", 503, true, err)
}
func isUnique(err error) bool { return err != nil && strings.Contains(err.Error(), "SQLSTATE 23505") }
func nilString(value string) any {
	if strings.TrimSpace(value) == "" {
		return nil
	}
	return value
}
func nilTime(value time.Time) any {
	if value.IsZero() {
		return nil
	}
	return value
}

var _ = fmt.Sprintf
