export type Api = <T = any>(
  path: string,
  method?: string,
  body?: unknown,
  signal?: AbortSignal,
) => Promise<T>;
export type AccessScope = "disabled" | "self" | "all";
export interface ToolPolicy {
  enabled: boolean;
  user_enabled: boolean;
  autonomous_enabled: boolean;
  request_access: { member: AccessScope; admin: AccessScope } | null;
  max_per_hour: number;
  max_duration: number;
  protected_ids: string[];
  allow_reject_rejoin: boolean;
  like_max_times?: number;
  like_daily_limit?: number;
  poke_cooldown_seconds?: number;
}
export interface PermissionRole {
  name: string;
  allow: string[];
  max_duration: number | null;
  protected_ids: string[];
}
export interface MemberAssignment {
  roles: string[];
  allow: string[];
  deny: string[];
  tools_disabled: boolean;
  muted: boolean;
  expires_at: number | null;
  max_duration: number | null;
  protected_ids: string[];
}
export interface AccessSnapshot {
  group_revision: number;
  interaction_enabled: boolean;
  tools: Record<string, ToolPolicy>;
  permissions: {
    revision: number;
    inherit_qq_admin: boolean;
    member_tools: string[];
    autonomous_rules: string;
    roles: Record<string, PermissionRole>;
    members: Record<string, MemberAssignment>;
  };
}
export interface Persona {
  id: string;
  name: string;
  description: string;
  prompt: string;
  aliases: string[];
  relationships?: Record<string, string>;
  version: number;
}
export interface Group {
  id: string;
  name: string;
  enabled: boolean;
  paused: boolean;
  management_paused: boolean;
  persona: string;
  tools: Record<string, any>;
  [key: string]: any;
}
export interface Health {
  onebot_connected?: boolean;
  self_id?: string;
  model_configured?: boolean;
  initialized?: boolean;
  uptime?: number;
  queue_depth?: number;
  last_event?: number;
}
export interface Money {
  currency: string;
  cost: number;
}
export interface Dashboard {
  generated_at: number;
  range: {
    start: number;
    end: number;
    bucket: string;
    coverage_start: number;
    partial: boolean;
  };
  health: Health;
  metrics: {
    calls: number;
    success: number;
    failed: number;
    excluded: Record<string, number>;
    success_rate: number | null;
    p95_ms: number | null;
    input_tokens: number;
    cached_tokens: number;
    output_tokens: number;
    costs: Money[];
  };
  series: {
    ts: number;
    label: string;
    calls: number | null;
    success: number | null;
    failed: number | null;
    partial: boolean;
  }[];
  purposes: {
    purpose: string;
    calls: number;
    input_tokens: number;
    cached_tokens: number;
    output_tokens: number;
    costs: Money[];
  }[];
  groups: (Pick<
    Group,
    "id" | "name" | "enabled" | "paused" | "management_paused" | "persona"
  > & { calls: number; failures: number })[];
  alerts: {
    id: string;
    scope: string;
    type: string;
    status: string;
    ts: number;
    title: string;
    trace_id?: string;
  }[];
  tasks: {
    id: string;
    scope: string;
    text: string;
    due: number;
    status: string;
  }[];
}
