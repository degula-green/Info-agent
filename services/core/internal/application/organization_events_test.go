package application

import (
	"context"
	"errors"
	"testing"
	"time"

	"info-agent/core/internal/domain"
)

type organizationEventSourceStub struct {
	events    []domain.OrganizationEvent
	published []string
	failed    []string
}

func (s *organizationEventSourceStub) ListPendingOrganizationEvents(context.Context, int) ([]domain.OrganizationEvent, error) {
	return append([]domain.OrganizationEvent(nil), s.events...), nil
}

func (s *organizationEventSourceStub) MarkOrganizationEventPublished(_ context.Context, eventID string, _ time.Time) error {
	s.published = append(s.published, eventID)
	return nil
}

func (s *organizationEventSourceStub) MarkOrganizationEventFailed(_ context.Context, eventID, _ string, _, _ time.Time) error {
	s.failed = append(s.failed, eventID)
	return nil
}

type organizationEventSinkStub struct {
	err error
}

func (s organizationEventSinkStub) PublishOrganizationEvent(context.Context, domain.OrganizationEvent) error {
	return s.err
}

func TestOrganizationEventRelayMarksPublished(t *testing.T) {
	source := &organizationEventSourceStub{events: []domain.OrganizationEvent{{ID: "event-1"}}}
	relay := NewOrganizationEventRelay(source, organizationEventSinkStub{}, time.Second, nil)
	if err := relay.Tick(context.Background()); err != nil {
		t.Fatal(err)
	}
	if len(source.published) != 1 || source.published[0] != "event-1" {
		t.Fatalf("published = %#v", source.published)
	}
}

func TestOrganizationEventRelayMarksFailed(t *testing.T) {
	source := &organizationEventSourceStub{events: []domain.OrganizationEvent{{ID: "event-1"}}}
	relay := NewOrganizationEventRelay(source, organizationEventSinkStub{err: errors.New("offline")}, time.Second, nil)
	if err := relay.Tick(context.Background()); err == nil {
		t.Fatal("expected relay error")
	}
	if len(source.failed) != 1 || source.failed[0] != "event-1" {
		t.Fatalf("failed = %#v", source.failed)
	}
}
