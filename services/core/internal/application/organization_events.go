package application

import (
	"context"
	"errors"
	"time"

	"info-agent/core/internal/domain"
)

type OrganizationEventSource interface {
	ListPendingOrganizationEvents(ctx context.Context, limit int) ([]domain.OrganizationEvent, error)
	MarkOrganizationEventPublished(ctx context.Context, eventID string, now time.Time) error
	MarkOrganizationEventFailed(ctx context.Context, eventID, reason string, availableAt, now time.Time) error
}

type OrganizationEventSink interface {
	PublishOrganizationEvent(ctx context.Context, event domain.OrganizationEvent) error
}

type OrganizationEventRelay struct {
	source   OrganizationEventSource
	sink     OrganizationEventSink
	interval time.Duration
	now      func() time.Time
}

func NewOrganizationEventRelay(
	source OrganizationEventSource,
	sink OrganizationEventSink,
	interval time.Duration,
	now func() time.Time,
) *OrganizationEventRelay {
	if interval <= 0 {
		interval = 5 * time.Second
	}
	if now == nil {
		now = time.Now
	}
	return &OrganizationEventRelay{source: source, sink: sink, interval: interval, now: now}
}

func (r *OrganizationEventRelay) Run(ctx context.Context) {
	if r == nil || r.source == nil || r.sink == nil {
		return
	}
	_ = r.Tick(ctx)
	ticker := time.NewTicker(r.interval)
	defer ticker.Stop()
	for {
		select {
		case <-ctx.Done():
			return
		case <-ticker.C:
			_ = r.Tick(ctx)
		}
	}
}

func (r *OrganizationEventRelay) Tick(ctx context.Context) error {
	if r == nil || r.source == nil || r.sink == nil {
		return errors.New("organization event relay is not configured")
	}
	events, err := r.source.ListPendingOrganizationEvents(ctx, 100)
	if err != nil {
		return err
	}
	var firstErr error
	for _, event := range events {
		publishErr := r.sink.PublishOrganizationEvent(ctx, event)
		if publishErr == nil {
			if markErr := r.source.MarkOrganizationEventPublished(ctx, event.ID, r.now().UTC()); markErr != nil && firstErr == nil {
				firstErr = markErr
			}
			continue
		}
		availableAt := r.now().UTC().Add(30 * time.Second)
		if markErr := r.source.MarkOrganizationEventFailed(ctx, event.ID, publishErr.Error(), availableAt, r.now().UTC()); markErr != nil && firstErr == nil {
			firstErr = markErr
		} else if firstErr == nil {
			firstErr = publishErr
		}
	}
	return firstErr
}
