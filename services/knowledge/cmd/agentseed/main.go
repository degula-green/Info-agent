// Command agentseed seeds one ready group message plus a calendar authorization
// so the Agent's stage-2b contract can be verified against a real stack.
//
// It is a development tool: run it with the same environment as the service.
// It prints the identifiers the Agent test needs as JSON.
package main

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"flag"
	"fmt"
	"os"
	"strings"
	"time"

	"github.com/google/uuid"
	"info-agent/knowledge/internal/apperror"
	"info-agent/knowledge/internal/config"
	"info-agent/knowledge/internal/crypto"
	"info-agent/knowledge/internal/domain"
	"info-agent/knowledge/internal/kv"
	"info-agent/knowledge/internal/repository"
	"info-agent/knowledge/internal/vault"
)

type seedResult struct {
	KnowledgeItemID         string `json:"knowledge_item_id"`
	ConversationIngestionID string `json:"conversation_ingestion_id"`
	MessageID               string `json:"source_message_id"`
	OwnerUserID             string `json:"owner_user_id"`
	ExcludedExternalUserID  string `json:"excluded_external_user_id"`
	Platform                string `json:"platform"`
	Text                    string `json:"text"`
}

func main() {
	ownerFlag := flag.String("owner", "", "map the seeded member to this existing user uuid (e.g. the signed-in user)")
	textFlag := flag.String("text", "", "message text to seed (default: a schedule-looking sentence)")
	flag.Parse()
	if err := run(*ownerFlag, *textFlag); err != nil {
		fmt.Fprintln(os.Stderr, "agentseed failed:", err)
		os.Exit(1)
	}
}

func run(ownerFlag, textFlag string) error {
	ctx := context.Background()
	cfg := config.Load()
	if strings.TrimSpace(cfg.DatabaseURL) == "" {
		return fmt.Errorf("KNOWLEDGE_DATABASE_URL is required")
	}
	repo, err := repository.NewPostgresStore(ctx, cfg.DatabaseURL)
	if err != nil {
		return err
	}
	store := kv.Store(kv.NewMemory())
	if cfg.RedisURL != "" {
		redisStore, err := kv.NewRedis(cfg.RedisURL)
		if err != nil {
			return err
		}
		store = redisStore
	}
	keyring, err := buildKeyring(cfg)
	if err != nil {
		return err
	}
	vaultStore := vault.New(store, keyring)

	now := time.Now().UTC()
	ownerUserID := uuid.NewString()
	seedOwnerUserID := uuid.NewString()
	organizationID := uuid.NewString()
	runID := now.Format("20060102150405")
	// A caller supplied owner (the signed-in user) gets its own external id so
	// repeated seeding never tries to remap an identity to a second user.
	mappedExternalID := "agent-seed-mapped"
	if strings.TrimSpace(ownerFlag) != "" {
		ownerUserID = strings.TrimSpace(ownerFlag)
		mappedExternalID = "agent-seed-ui-" + strings.ReplaceAll(ownerUserID, "-", "")[:8]
	}

	account := domain.ConnectorAccount{
		OwnerUserID: seedOwnerUserID, Platform: domain.PlatformFeishu,
		ExternalAccountID: "agent-seed-account", WorkspaceKey: "agent-seed-workspace",
		DisplayName: "agent seed", Status: domain.ConnectorActive,
	}
	savedAccount, err := repo.FindConnectorByExternal(ctx, account.Platform, account.WorkspaceKey, account.ExternalAccountID)
	if err != nil {
		if apperror.From(err).Code != "connector_not_found" {
			return err
		}
		savedAccount, err = repo.SaveConnector(ctx, account)
		if err != nil {
			return err
		}
	}
	// Re-runs must reuse the same owner, otherwise the connector owner check
	// rejects attaching the seeded conversation again.
	seedOwnerUserID = savedAccount.OwnerUserID

	conversation, err := repo.FindConversationByExternal(ctx, domain.PlatformFeishu, savedAccount.WorkspaceKey, "agent-seed-chat")
	if err != nil {
		if apperror.From(err).Code != "conversation_not_found" {
			return err
		}
		conversation, err = repo.AttachConversation(ctx, repository.AttachInput{
			UserID: seedOwnerUserID, Platform: domain.PlatformFeishu, WorkspaceKey: savedAccount.WorkspaceKey,
			ExternalConversationID: "agent-seed-chat",
			ConversationType: "group", Name: "agent seed group", RequestedStartAt: &now,
			PrimaryConnectorID: savedAccount.ID, OrganizationID: organizationID,
		})
		if err != nil {
			return err
		}
	}

	// A stable external id keeps repeated runs from piling up left members.
	excludedExternalID := "agent-seed-unmapped"
	if err := repo.UpsertConversationMemberships(ctx, conversation.ID, []domain.AvailableMember{
		{ExternalUserID: mappedExternalID, DisplayName: "已映射成员"},
		{ExternalUserID: excludedExternalID, DisplayName: "未映射成员"},
	}); err != nil {
		return err
	}
	// Keep the mapped owner stable across runs: re-mapping the same external id
	// to a different user is a conflict by design.
	if strings.TrimSpace(ownerFlag) == "" {
		if existing, err := repo.GetExternalIdentity(ctx, domain.PlatformFeishu, savedAccount.WorkspaceKey, mappedExternalID); err == nil && existing != nil {
			if strings.TrimSpace(existing.MappedUserID) != "" {
				ownerUserID = existing.MappedUserID
			}
		}
	}
	if _, err := repo.UpsertExternalIdentity(ctx, repository.ExternalIdentityInput{
		Platform: domain.PlatformFeishu, WorkspaceKey: savedAccount.WorkspaceKey,
		ExternalUserID: mappedExternalID, DisplayName: "已映射成员", MappedUserID: ownerUserID,
	}); err != nil {
		return err
	}

	text := "明天晚上八点开个评审会，会议室 A"
	if strings.TrimSpace(textFlag) != "" {
		text = strings.TrimSpace(textFlag)
	}
	sum := sha256.Sum256([]byte(text))
	input := repository.IngestMessageInput{
		CollectorID:            collectorID(conversation),
		ExternalConversationID: conversation.ExternalConversationID,
		ExternalMessageID:      "agent-seed-message-" + runID,
		MessageType:            "text",
		Content:                text,
		ContentHash:            hex.EncodeToString(sum[:]),
		SentAt:                 now,
	}
	input.PayloadHash, _ = repository.CalculatePayloadHash(input)
	ingested, err := repo.IngestMessage(ctx, input)
	if err != nil {
		return err
	}
	if err := repo.CompleteMessageClassification(ctx, ingested.Message.ID, text, false); err != nil {
		return err
	}
	item, err := repo.GetKnowledgeItemByMessage(ctx, ingested.Message.ID)
	if err != nil {
		return err
	}
	if err := repo.MarkKnowledgePermissionSynced(ctx, item.ID, 2); err != nil {
		return err
	}
	if _, err := repo.TryMarkKnowledgeReady(ctx, item.ID, "agent-seed"); err != nil {
		return err
	}

	credentialRef := "agent-seed-calendar-" + runID
	token := vault.TokenSet{AccessToken: "seed-access-token", RefreshToken: "seed-refresh-token", ExpiresAt: now.Add(24 * time.Hour)}
	if err := vaultStore.Put(ctx, credentialRef, token, vault.CredentialTTL(token, now)); err != nil {
		return err
	}
	if _, err := repo.UpsertCalendarAuthorization(ctx, domain.CalendarAuthorization{
		OwnerUserID: ownerUserID, Provider: domain.PlatformFeishu, CredentialRef: credentialRef,
		ExternalAccountID: "agent-seed-account", Status: domain.CalendarAuthorizationActive,
	}, now); err != nil {
		return err
	}

	return json.NewEncoder(os.Stdout).Encode(seedResult{
		KnowledgeItemID:         item.ID,
		ConversationIngestionID: conversation.ID,
		MessageID:               ingested.Message.ID,
		OwnerUserID:             ownerUserID,
		ExcludedExternalUserID:  excludedExternalID,
		Platform:                domain.PlatformFeishu,
		Text:                    text,
	})
}

func collectorID(conversation *domain.ConversationIngestion) string {
	if conversation == nil || len(conversation.Collectors) == 0 {
		return ""
	}
	return conversation.Collectors[0].ID
}

func buildKeyring(cfg config.Config) (*crypto.Keyring, error) {
	values := map[string]string{}
	for _, item := range strings.Split(cfg.EncryptionKeys, ",") {
		parts := strings.SplitN(strings.TrimSpace(item), ":", 2)
		if len(parts) == 2 {
			values[strings.TrimSpace(parts[0])] = strings.TrimSpace(parts[1])
		}
	}
	return crypto.NewKeyring(cfg.EncryptionKeyVersion, values)
}
