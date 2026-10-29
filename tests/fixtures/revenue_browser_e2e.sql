PRAGMA foreign_keys=ON;
BEGIN IMMEDIATE;

INSERT INTO users(
  id,username,display_name,password_hash,role_id,is_active,must_change_password,created_at
) VALUES
  (900001,'revenue-e2e-admin','E2E Admin','isolated-session-only',(SELECT id FROM roles WHERE name='Admin'),1,0,'2026-08-29T00:00:00Z'),
  (900002,'revenue-e2e-manager','E2E Manager','isolated-session-only',(SELECT id FROM roles WHERE name='Manager'),1,0,'2026-08-29T00:00:00Z'),
  (900003,'revenue-e2e-consultant','E2E Consultant','isolated-session-only',(SELECT id FROM roles WHERE name='Consultant'),1,0,'2026-08-29T00:00:00Z'),
  (900004,'revenue-e2e-finance','E2E Finance','isolated-session-only',(SELECT id FROM roles WHERE name='Finance'),1,0,'2026-08-29T00:00:00Z'),
  (900005,'revenue-e2e-staff','E2E Staff','isolated-session-only',(SELECT id FROM roles WHERE name='Content Staff'),1,0,'2026-08-29T00:00:00Z');

INSERT INTO sessions(token,user_id,csrf_token,expires_at,created_at,last_seen_at) VALUES
  ('revenue-e2e-admin',900001,'e2e-admin-csrf','2099-01-01T00:00:00Z','2026-08-29T00:00:00Z','2098-12-31T23:59:59Z'),
  ('revenue-e2e-manager',900002,'e2e-manager-csrf','2099-01-01T00:00:00Z','2026-08-29T00:00:00Z','2098-12-31T23:59:59Z'),
  ('revenue-e2e-consultant',900003,'e2e-consultant-csrf','2099-01-01T00:00:00Z','2026-08-29T00:00:00Z','2098-12-31T23:59:59Z'),
  ('revenue-e2e-finance',900004,'e2e-finance-csrf','2099-01-01T00:00:00Z','2026-08-29T00:00:00Z','2098-12-31T23:59:59Z'),
  ('revenue-e2e-staff',900005,'e2e-staff-csrf','2099-01-01T00:00:00Z','2026-08-29T00:00:00Z','2098-12-31T23:59:59Z');

INSERT INTO user_module_permissions(
  id,user_id,module_key,can_access,can_view,created_at,updated_at,updated_by
) VALUES
  (900011,900001,'revenue_commission',1,1,'2026-08-29T00:00:00Z','2026-08-29T00:00:00Z',900001),
  (900012,900002,'dashboard',1,1,'2026-08-29T00:00:00Z','2026-08-29T00:00:00Z',900001),
  (900013,900002,'revenue_commission',1,1,'2026-08-29T00:00:00Z','2026-08-29T00:00:00Z',900001),
  (900014,900002,'higher_education',1,1,'2026-08-29T00:00:00Z','2026-08-29T00:00:00Z',900001),
  (900015,900003,'dashboard',1,1,'2026-08-29T00:00:00Z','2026-08-29T00:00:00Z',900001),
  (900016,900003,'revenue_commission',1,1,'2026-08-29T00:00:00Z','2026-08-29T00:00:00Z',900001),
  (900017,900004,'dashboard',1,1,'2026-08-29T00:00:00Z','2026-08-29T00:00:00Z',900001),
  (900018,900004,'revenue_commission',1,1,'2026-08-29T00:00:00Z','2026-08-29T00:00:00Z',900001),
  (900019,900005,'dashboard',1,1,'2026-08-29T00:00:00Z','2026-08-29T00:00:00Z',900001),
  (900020,900005,'revenue_commission',1,1,'2026-08-29T00:00:00Z','2026-08-29T00:00:00Z',900001);

UPDATE revenue_module_state
SET status='active',cutover_at='2026-08-29T00:00:00Z',activated_by=900001,
    updated_at='2026-08-29T00:00:00Z'
WHERE id=1 AND status='disabled';

COMMIT;
