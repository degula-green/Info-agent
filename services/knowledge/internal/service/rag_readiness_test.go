package service

import (
	"testing"

	"info-agent/knowledge/internal/domain"
)

func TestRAGKnowledgeReadyUsesProcessingStatusForActiveSharedItems(t *testing.T) {
	for _, item := range []domain.KnowledgeItem{
		{LifecycleStatus: "active", ProcessingStatus: "ready"},
		{LifecycleStatus: "ready", ProcessingStatus: "ready"},
	} {
		if !ragKnowledgeReady(&item) {
			t.Fatalf("expected item to be RAG-ready: %+v", item)
		}
	}
}

func TestRAGKnowledgeReadyRejectsUnprocessedOrInactiveItems(t *testing.T) {
	for _, item := range []domain.KnowledgeItem{
		{LifecycleStatus: "active", ProcessingStatus: "pending"},
		{LifecycleStatus: "active", ProcessingStatus: "failed"},
		{LifecycleStatus: "revoked", ProcessingStatus: "ready"},
		{LifecycleStatus: "deleted", ProcessingStatus: "ready"},
	} {
		if ragKnowledgeReady(&item) {
			t.Fatalf("expected item not to be RAG-ready: %+v", item)
		}
	}
}
