-- 020_setting_direct_apply.sql
--
-- 平台管理员保存系统设置时立即生效，不再要求第二位管理员审批。
-- 018 里的 ck_system_setting_change_approval_separation 强制
-- approved_by <> requested_by，会让「自己批准自己」的直接生效写不进去。
-- 这里换成只要求 approved_by 非空的约束：变更仍留痕（status=APPROVED，
-- 申请人与生效人相同），审计仍记录旧值与新值的哈希。
--
-- 向后兼容：历史 PENDING 行不受影响，decide_change 仍可正常审批。

BEGIN;

ALTER TABLE system_setting_changes
    DROP CONSTRAINT IF EXISTS ck_system_setting_change_approval_separation;

ALTER TABLE system_setting_changes
    ADD CONSTRAINT ck_system_setting_change_approval_present
    CHECK (status <> 'APPROVED' OR approved_by IS NOT NULL);

COMMIT;
