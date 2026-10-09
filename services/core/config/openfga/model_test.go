package openfga_test

import (
	"os"
	"strings"
	"testing"
)

func TestSensitiveOriginalModelRequiresGroupMembershipAndOrganizationMembership(t *testing.T) {
	raw, err := os.ReadFile("model.fga")
	if err != nil {
		t.Fatal(err)
	}
	model := string(raw)
	required := []string{
		"define member: participant and member from organization",
		"define moderator: information_admin from organization",
		"define original_viewer: owner or moderator or member from conversation_group",
		"define eligible_viewer: owner or viewer or original_viewer from parent",
	}
	for _, fragment := range required {
		if !strings.Contains(model, fragment) {
			t.Fatalf("OpenFGA model is missing required authorization fragment %q", fragment)
		}
	}
}
