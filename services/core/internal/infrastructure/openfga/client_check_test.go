package openfga

import (
	"testing"

	"info-agent/core/internal/application"
)

func TestMapResourceOrganizationInformationAdmin(t *testing.T) {
	objectType, relation, objectID, err := mapResource(application.AuthorizationCheck{
		ResourceType: "organization",
		ResourcePart: "information_admin",
		ResourceID:   "org-1",
		Action:       "view",
	})
	if err != nil {
		t.Fatal(err)
	}
	if objectType != "organization" || relation != "information_admin" || objectID != "org-1" {
		t.Fatalf("unexpected mapping: %s %s %s", objectType, relation, objectID)
	}
}
