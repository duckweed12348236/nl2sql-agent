import type { ColumnRole, TableRole } from '@/types/modeling'

export const TABLE_ROLE_LABEL: Record<TableRole, string> = {
  fact: '事实表',
  dim: '维度表',
}

export const TABLE_ROLE_TAG: Record<TableRole, string> = {
  fact: 'tag--peach',
  dim: 'tag--sky',
}

export const COLUMN_ROLE_LABEL: Record<ColumnRole, string> = {
  primary_key: '主键',
  foreign_key: '外键',
  measure: '度量',
  dimension: '维度',
}

export const COLUMN_ROLE_TAG: Record<ColumnRole, string> = {
  primary_key: 'tag--accent',
  foreign_key: 'tag--sky',
  measure: 'tag--peach',
  dimension: 'tag--mint',
}

export function tableRoleLabel(role: TableRole | null): string {
  return role ? TABLE_ROLE_LABEL[role] : '未设置'
}

export function tableRoleTag(role: TableRole | null): string {
  return role ? TABLE_ROLE_TAG[role] : 'tag--neutral'
}

export function columnRoleLabel(role: ColumnRole | null): string {
  return role ? COLUMN_ROLE_LABEL[role] : '未设置'
}

export function columnRoleTag(role: ColumnRole | null): string {
  return role ? COLUMN_ROLE_TAG[role] : 'tag--neutral'
}
