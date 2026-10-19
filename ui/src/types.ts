export interface NodeData {
  id: string;
  label?: string;
  source?: string;
  target?: string;
  type?: string;
  health?: string;
  owner_team?: string;
  tier?: string;
  origin?: string;
  latency_ms?: number;
  cost?: number;
  email?: string;
  team?: string;
  status?: string;
  mode?: string;
  active?: boolean;
  reason?: string;
  name?: string;
  order?: number;
  workflow_id?: string;
}

export interface CytoscapeElement {
  data: NodeData;
}

export interface TraceLog {
  tool: string;
  graph: string;
  cypher?: string;
  params?: Record<string, unknown>;
  duration_ms?: number;
  algorithm_result?: unknown;
}

export interface IdentityState {
  tenant_id: string;
  execution_mode: 'simulation' | 'real';
  real_recovery_enabled: boolean;
  data_origin: string;
  observed_at?: string;
}

export interface CitationItem {
  document_id: string;
  origin?: string;
  source_kind?: string;
  content_hash?: string;
  source_version?: string;
  revision?: number | string;
  path?: string;
  permalink?: string;
  author_id?: string | null;
  chunk_id?: string;
  chunk_index?: number;
  excerpt?: string;
  text?: string;
}

export interface ProposedAction {
  action_kind?: string;
  source?: string;
  target?: string;
  route?: string[];
  latency_ms?: number;
  cost?: number;
  rationale?: string;
  document_name?: string;
  instance_id?: string;
}

export type ActionStatus =
  | 'pending'
  | 'awaiting_approval'
  | 'investigating'
  | 'executing'
  | 'verifying'
  | 'recovered'
  | 'failed'
  | 'execution_unknown'
  | 'verification_unknown'
  | 'recovery_failed'
  | 'recovered_externally'
  | 'applied'
  | 'rejected'
  | 'expired';
