BEGIN;

INSERT INTO iam.permissions (code, name, description) VALUES
    ('organization.member.suspend', 'Suspend members', 'Suspend an active organization member'),
    ('organization.member.remove', 'Remove members', 'Permanently remove an organization member'),
    ('organization.owner.transfer', 'Transfer ownership', 'Grant the owner role to another active member'),
    ('organization.audit.read', 'Read audit logs', 'Read organization audit logs')
ON CONFLICT (code) DO NOTHING;

INSERT INTO iam.role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM iam.roles AS r
JOIN iam.permissions AS p
    ON p.code IN (
        'organization.member.suspend',
        'organization.member.remove',
        'organization.owner.transfer',
        'organization.audit.read'
    )
WHERE r.code = 'owner'
ON CONFLICT DO NOTHING;

COMMIT;
