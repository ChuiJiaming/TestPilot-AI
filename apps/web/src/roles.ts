// 平台统一的角色中文名。后端枚举见 apps/api/app/auth/lifecycle.py 与
// db/migrations/001_schema.sql；这里只管显示，不做权限判断。
const ROLE_LABELS: Record<string, string> = {
  org_admin: "组织管理员",
  org_member: "组织成员",
  project_admin: "项目管理员",
  tester: "测试人员",
  developer: "开发人员",
  viewer: "只读成员",
};

/** 角色枚举 → 中文显示名；未知值原样返回，避免后端新增角色时显示为空。 */
export function roleLabel(role: string | null | undefined): string {
  if (!role) return "未分配";
  return ROLE_LABELS[role] ?? role;
}
