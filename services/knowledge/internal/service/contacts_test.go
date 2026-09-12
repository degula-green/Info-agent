package service

import (
	"context"
	"testing"

	"info-agent/knowledge/internal/domain"
	"info-agent/knowledge/internal/repository"
)

func TestListContactsMergesOnlyMappedIdentities(t *testing.T) {
	repo := repository.NewMemoryStore()
	ctx := context.Background()
	account, err := repo.SaveConnector(ctx, domain.ConnectorAccount{OwnerUserID: "u-internal", Platform: domain.PlatformWechat, ExternalAccountID: "wx", Status: domain.ConnectorActive})
	if err != nil {
		t.Fatal(err)
	}
	feishu, err := repo.SaveConnector(ctx, domain.ConnectorAccount{OwnerUserID: "u-internal", Platform: domain.PlatformFeishu, WorkspaceKey: "tenant", ExternalAccountID: "fs", Status: domain.ConnectorActive})
	if err != nil {
		t.Fatal(err)
	}
	id1, err := repo.UpsertExternalIdentity(ctx, repository.ExternalIdentityInput{Platform: domain.PlatformWechat, ExternalUserID: "wx-1", DisplayName: "同一人", MappedUserID: "u-internal"})
	if err != nil {
		t.Fatal(err)
	}
	id2, err := repo.UpsertExternalIdentity(ctx, repository.ExternalIdentityInput{Platform: domain.PlatformFeishu, WorkspaceKey: "tenant", ExternalUserID: "fs-1", DisplayName: "同一人", MappedUserID: "u-internal"})
	if err != nil {
		t.Fatal(err)
	}
	if _, err = repo.UpsertContactRelation(ctx, repository.ContactRelationInput{OwnerUserID: "u-internal", ConnectorID: account.ID, ExternalIdentityID: id1}); err != nil {
		t.Fatal(err)
	}
	if _, err = repo.UpsertContactRelation(ctx, repository.ContactRelationInput{OwnerUserID: "u-internal", ConnectorID: feishu.ID, ExternalIdentityID: id2}); err != nil {
		t.Fatal(err)
	}
	if _, err := repo.UpsertExternalIdentity(ctx, repository.ExternalIdentityInput{Platform: domain.PlatformWechat, ExternalUserID: "wx-2", DisplayName: "同名但不同人"}); err != nil {
		t.Fatal(err)
	}
	svc := &Service{Repo: repo}
	contacts, err := svc.ListContacts(ctx, "u-internal", "")
	if err != nil {
		t.Fatal(err)
	}
	if len(contacts) != 1 || contacts[0].Kind != "internal" || len(contacts[0].Identities) != 2 {
		t.Fatalf("unexpected merged contacts: %#v", contacts)
	}
}

func TestListContactsKeepsUnmappedPlatformsSeparate(t *testing.T) {
	repo := repository.NewMemoryStore()
	ctx := context.Background()
	if _, err := repo.UpsertExternalIdentity(ctx, repository.ExternalIdentityInput{Platform: domain.PlatformWechat, ExternalUserID: "same", DisplayName: "相同昵称"}); err != nil {
		t.Fatal(err)
	}
	if _, err := repo.UpsertExternalIdentity(ctx, repository.ExternalIdentityInput{Platform: domain.PlatformFeishu, ExternalUserID: "same", DisplayName: "相同昵称"}); err != nil {
		t.Fatal(err)
	}
	// Identities become visible to the contact owner through membership, so an
	// unmapped identity without a conversation must not be guessed into a view.
	contacts, err := (&Service{Repo: repo}).ListContacts(ctx, "owner", "")
	if err != nil {
		t.Fatal(err)
	}
	if len(contacts) != 0 {
		t.Fatalf("unmapped identities leaked without a relationship: %#v", contacts)
	}
}
