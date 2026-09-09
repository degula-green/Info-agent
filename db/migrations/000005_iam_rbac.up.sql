-- System-defined RBAC catalog. Organization membership remains the scope of
-- role assignments; iam.membership_roles is the organization-scoped user-role
-- relationship and is intentionally retained for compatibility.
CREATE TABLE iam.roles (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    code VARCHAR(64) NOT NULL UNIQUE,
    name VARCHAR(128) NOT NULL,
    description TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT roles_code_chk
        CHECK (code IN ('owner', 'information_admin', 'membership_approver'))
);

CREATE TABLE iam.permissions (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    code VARCHAR(128) NOT NULL UNIQUE,
    name VARCHAR(128) NOT NULL,
    description TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE iam.role_permissions (
    role_id UUID NOT NULL REFERENCES iam.roles (id) ON DELETE CASCADE,
    permission_id UUID NOT NULL REFERENCES iam.permissions (id) ON DELETE CASCADE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (role_id, permission_id)
);

INSERT INTO iam.roles (code, name, description) VALUES
    ('owner', 'Owner', 'All organization permissions'),
    ('information_admin', 'Information administrator', 'Information resource management permissions'),
    ('membership_approver', 'Membership approver', 'Invitation and membership admission permissions')
ON CONFLICT (code) DO NOTHING;

INSERT INTO iam.permissions (code, name, description) VALUES
    ('organization.member.read', 'Read members', 'Read organization member information'),
    ('organization.invitation.create', 'Create invitations', 'Create organization invitations'),
    ('organization.invitation.revoke', 'Revoke invitations', 'Revoke pending organization invitations'),
    ('organization.role.manage', 'Manage roles', 'Grant and revoke organization roles'),
    ('organization.information.manage', 'Manage information', 'Manage organization information resources')
ON CONFLICT (code) DO NOTHING;

-- Owner receives every permission currently defined in the system.
INSERT INTO iam.role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM iam.roles AS r
CROSS JOIN iam.permissions AS p
WHERE r.code = 'owner'
ON CONFLICT DO NOTHING;

INSERT INTO iam.role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM iam.roles AS r
JOIN iam.permissions AS p ON p.code = 'organization.information.manage'
WHERE r.code = 'information_admin'
ON CONFLICT DO NOTHING;

INSERT INTO iam.role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM iam.roles AS r
JOIN iam.permissions AS p
    ON p.code IN (
        'organization.member.read',
        'organization.invitation.create',
        'organization.invitation.revoke'
    )
WHERE r.code = 'membership_approver'
ON CONFLICT DO NOTHING;

ALTER TABLE iam.membership_roles
    ADD COLUMN role_id UUID REFERENCES iam.roles (id);

ALTER TABLE iam.roles
    ADD CONSTRAINT roles_id_code_uq UNIQUE (id, code);

UPDATE iam.membership_roles AS mr
SET role_id = r.id
FROM iam.roles AS r
WHERE r.code = mr.role_code;

-- member is an implicit capability of an active membership from this point on.
DELETE FROM iam.membership_roles
WHERE role_code = 'member';

ALTER TABLE iam.membership_roles
    DROP CONSTRAINT membership_roles_role_code_chk,
    ADD CONSTRAINT membership_roles_role_code_chk
        CHECK (role_code IN ('owner', 'information_admin', 'membership_approver'));

ALTER TABLE iam.membership_roles
    ADD CONSTRAINT membership_roles_role_fk
        FOREIGN KEY (role_id, role_code) REFERENCES iam.roles (id, code);

CREATE INDEX membership_roles_role_id_idx
    ON iam.membership_roles (role_id);

COMMENT ON TABLE iam.roles IS '系统预置的组织管理角色定义';
COMMENT ON TABLE iam.permissions IS '按业务动作划分的系统权限定义';
COMMENT ON TABLE iam.role_permissions IS '系统角色与权限的固定映射';
COMMENT ON COLUMN iam.membership_roles.role_id IS '系统角色 ID；role_code 为兼容字段';
