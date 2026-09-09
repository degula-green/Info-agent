package application

import "context"

type AuthorizationCheck struct {
	ResourceType string
	ResourcePart string
	ResourceID   string
	Action       string
}

type AuthorizationDecision struct {
	Allowed bool
}

type AuthorizationScope struct {
	Objects map[string][]string
}

type AuthorizationProvider interface {
	Check(ctx context.Context, subjectID, organizationID string, check AuthorizationCheck) (bool, error)
	ListObjects(ctx context.Context, subjectID, organizationID, objectType, relation string) ([]string, error)
}
