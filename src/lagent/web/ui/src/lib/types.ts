/* 后端契约的 TypeScript 投影。
 *
 * 唯一的事实来源是 src/lagent/schemas.py —— 这里的每个字段都与那边的
 * Pydantic 模型逐一对应，**名字一字不改**。之所以不手写 codegen：
 * 只有十来个模型，手写一遍顺便能把「哪个字段可能为 null」看一遍，
 * 而自动生成会把这些信息磨平成 any。
 *
 * 一条纪律：宁可在这里写错、然后在浏览器里看到 undefined，
 * 也不要为了「看起来能跑」给字段加 ? 或 any —— 那会把契约漂移藏起来。
 */

export type Role = "user" | "admin" | "sysadmin";

export interface UserOut {
  id: number;
  username: string;
  role: Role;
  certs: string[];
}

export interface MeOut extends UserOut {
  rank: number;
  capabilities: string[];
}

export interface TokenResponse {
  access_token: string;
  token_type: string;
  expires_in: number;
  user: MeOut;
}

export type ReservationStatus =
  | "pending"
  | "confirmed"
  | "cancelled"
  | "completed"
  | "expired";

export interface ReservationOut {
  id: number;
  equipment_id: number;
  user_id: number;
  user_name: string;
  equipment_name: string;
  lab_label: string;
  date: string;
  start_time: string;
  end_time: string;
  status: ReservationStatus;
  purpose: string;
  slot: string;
  no_show_at: string | null;
  pardoned_at: string | null;
}

export type BookingReason =
  | "ok"
  | "not_found"
  | "invalid"
  | "forbidden"
  | "state"
  | "conflict"
  | "contention";

export interface BookingOutcome {
  ok: boolean;
  message: string;
  reservation: ReservationOut | null;
  retries: number;
  conflict_with: number | null;
  reason: BookingReason | null;
  replayed: boolean;
}

export interface EquipmentOut {
  id: number;
  code: string;
  name: string;
  category: string;
  max_hours: number;
  status: string;
  requires_training: boolean;
  requires_approval: boolean;
}

export interface OpenHours {
  weekday: [string, string] | null;
  weekend: [string, string] | null;
}

export interface LabOut {
  id: number;
  label: string;
  building: string;
  floor: string;
  room: string;
  capacity: number;
  note: string;
  open_hours: OpenHours;
  equipment: EquipmentOut[];
}

export interface AuditLogOut {
  id: number;
  created_at: string;
  actor_id: number | null;
  actor_name: string;
  action: string;
  target_type: string;
  target_id: string;
  outcome: string;
  detail: string;
  client_host: string;
}

export interface Proposal {
  kind: string;
  equipment_id: number;
  equipment_name: string;
  lab_label: string;
  date: string;
  start: string;
  end: string;
  hours: number;
  relaxations: string[];
  score: number;
  reason: string;
}

export interface DocHit {
  source: string;
  heading: string;
  text: string;
  score: number;
  matched_by: string[];
}

export interface TraceStep {
  node: string;
  detail: string;
  elapsed_ms: number;
}

export interface ChatResponse {
  reply: string;
  intent: string | null;
  stage: string;
  missing: string[];
  proposals: Proposal[];
  citations: DocHit[];
  booking: BookingOutcome | null;
  trace: TraceStep[];
  degraded: boolean;
}

export interface ChatRequest {
  message: string;
  session_id: string;
  accept_equipment_id?: number;
  accept_date?: string;
  accept_start?: string;
  accept_end?: string;
}

export interface HealthDetails {
  app_mode: string;
  database: string;
  agent_available: boolean;
  retrieval_backend: string;
  retrieval_degraded_reason: string | null;
  now: string;
  counts: {
    labs: number;
    equipment: number;
    active: number;
    reservations: number;
  };
}

export interface AccessVerifyResponse {
  granted: boolean;
  reason_code: string;
  message: string;
  permit_id: number | null;
  user_id: number | null;
  user_name: string;
  lab_id: number | null;
  lab_label: string;
}

export interface InsideEntry {
  permit_id: number;
  user_id: number;
  username: string;
  lab_id: number;
  lab_label: string;
  valid_from: string;
  valid_to: string;
  checked_in_at: string | null;
}

export interface AccessIssueResponse {
  permit_id: number;
  user_id: number;
  lab_id: number;
  date: string;
  valid_from: string;
  valid_to: string;
  /* 明文凭证只在这里返回一次，服务端只留哈希。 */
  credential: string;
  required_certs: string[];
}

export interface ViolationsOut {
  blocked: boolean;
  over_threshold: boolean;
  message: string;
  count: number;
  threshold: number;
  window_days: number;
  blocking_enabled: boolean;
}

export interface NotificationOut {
  kind: string;
  title: string;
  body: string;
  status: string;
  created_at: string;
}

export interface TodayOut {
  date: string;
  weekday: string;
  time: string;
  tomorrow: string;
}

export interface RetrieveOut {
  backend: string;
  stats: { chunks: number };
  degraded_reason: string | null;
  hits: DocHit[];
}

export interface ToolOut {
  name: string;
  description: string;
  params: Record<string, unknown>;
  side_effect: boolean;
}

export interface BatchReviewOut {
  approved: number;
  failed: { id: number; reason: string }[];
}
