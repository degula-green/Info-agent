BEGIN;

DELETE FROM iam.role_permissions AS rp
USING iam.roles AS r, iam.permissions AS p
WHERE rp.role_id = r.id
  AND rp.permission_id = p.id
  AND r.code = 'owner'
  AND p.code IN (
      'organization.member.suspend',
      'organization.member.remove',
      'organization.owner.transfer',
      'organization.audit.read'
  );

DELETE FROM iam.permissions
WHERE code IN (
    'organization.member.suspend',
    'organization.member.remove',
    'organization.owner.transfer',
    'organization.audit.read'
);

COMMIT;
