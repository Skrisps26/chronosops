import React, { useEffect, useRef, useState } from 'react';
import cytoscape from 'cytoscape';

interface NodeData {
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
}

interface CytoscapeElement {
  data: NodeData;
}


interface IdentityState {
  tenant_id: string;
  execution_mode: 'simulation';
  real_recovery_enabled: boolean;
  data_origin: string;
}

interface CitationChunk {
  chunk_id?: string;
  chunk_index?: number;
  text?: string;
  excerpt?: string;
}

interface CitationItem {
  doc_id?: string;
  document_id?: string;
  excerpt?: string;
  text?: string;
  chunks?: CitationChunk[];
}

interface SourceDocument {
  document_id: string;
  title?: string;
  revision?: string | number;
  source_version?: string;
  content_hash?: string;
  permalink?: string;
  path?: string;
  chunks: CitationChunk[];
}

interface ProposedAction {
  action_kind?: string;
  route?: string[];
  latency_ms?: number;
  cost?: number;
  rationale?: string;
  document_name?: string;
  instance_id?: string;
}

type ActionStatus =
  | 'pending' | 'awaiting_approval' | 'investigating' | 'executing' | 'verifying'
  | 'recovered' | 'failed' | 'execution_unknown' | 'verification_unknown'
  | 'recovery_failed' | 'recovered_externally' | 'applied' | 'rejected' | 'expired';

interface ActionOutcome {
  incident_id: string;
  approval_id?: string;
  status: ActionStatus;
  command_id?: string;
  ssm_status?: string;
  recovery_sample_ids?: string[];
  memory_pending?: boolean;
  notification_pending?: boolean;
}

const actionLabels: Record<ActionStatus, string> = {
  pending: 'Awaiting human approval',
  awaiting_approval: 'Awaiting human approval',
  investigating: 'Investigating',
  executing: 'Command execution in progress — recovery not yet verified',
  verifying: 'Command succeeded — verifying HTTP health',
  recovered: 'Recovery verified by post-command probes',
  failed: 'Action failed',
  execution_unknown: 'Execution unknown — operator reconciliation required',
  verification_unknown: 'Health verification unknown — operator reconciliation required',
  recovery_failed: 'Application still unhealthy after command',
  recovered_externally: 'Recovered externally — not attributed to this action',
  applied: 'Simulation applied',
  rejected: 'Simulation proposal rejected',
  expired: 'Approval expired — investigate current evidence',
};

interface ApprovalItem {
  approval_id: string;
  incident_id: string;
  service_id: string;
  action?: string;
  status?: ActionStatus;
  proposed?: ProposedAction;
  expires_at?: string;
}

interface TraceLog {
  tool: string;
  graph: string;
  cypher?: string;
  params?: Record<string, unknown>;
  duration_ms?: number;
  algorithm_result?: unknown;
}

interface RunEvent {
  run_id: string;
  sequence: number;
  type: string;
  timestamp: string;
  payload?: Record<string, unknown>;
}

async function readPayload(response: Response): Promise<Record<string, unknown>> {
  const body = await response.text();
  if (!body) return {};
  try {
    return JSON.parse(body) as Record<string, unknown>;
  } catch {
    return { detail: body };
  }
}

function responseError(payload: { detail?: unknown }, status: number, fallback: string): Error {
  return new Error(typeof payload.detail === 'string' ? payload.detail : `${fallback} (HTTP ${status})`);
}
export default function App() {
  const [identity, setIdentity] = useState<IdentityState | null>(null);
  const [topology, setTopology] = useState<CytoscapeElement[]>([]);
  const [selectedNode, setSelectedNode] = useState<NodeData | null>(null);
  const [serviceId, setServiceId] = useState('payments-primary');
  const [question, setQuestion] = useState('Who owns payments and why is the backup unavailable?');
  const [answer, setAnswer] = useState<string | null>(null);
  const [citations, setCitations] = useState<CitationItem[]>([]);
  const [questionError, setQuestionError] = useState<string | null>(null);
  const [incidentError, setIncidentError] = useState<string | null>(null);
  const [activeApproval, setActiveApproval] = useState<ApprovalItem | null>(null);
  const [actionOutcome, setActionOutcome] = useState<ActionOutcome | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);
  const [sourceDocument, setSourceDocument] = useState<SourceDocument | null>(null);
  const [sourceError, setSourceError] = useState<string | null>(null);
  const [sourceLoading, setSourceLoading] = useState(false);
  const [runSubscription, setRunSubscription] = useState<string | null>(null);
  const [traceLogs, setTraceLogs] = useState<TraceLog[]>([]);
  const [isDrawerOpen, setIsDrawerOpen] = useState(true);
  const [loading, setLoading] = useState(false);
  const cyRef = useRef<HTMLDivElement>(null);
  const cyInstance = useRef<cytoscape.Core | null>(null);
  const socketRef = useRef<WebSocket | null>(null);

  // The reverse proxy owns browser identity; the UI sends neither tenant IDs nor credentials.
  useEffect(() => {
    fetch('/api/identity')
      .then(async (response) => {
        const data = await readPayload(response);
        if (!response.ok || typeof data.tenant_id !== 'string') return;
        setIdentity(data as IdentityState);
      })
      .catch(() => {});


    fetch('/api/topology')
      .then(async (response) => {
        const data = await readPayload(response);
        if (!response.ok) throw responseError(data, response.status, 'Topology unavailable');
        setTopology(Array.isArray(data.elements) ? data.elements as CytoscapeElement[] : []);
      })
      .catch(() => setTopology([]));

    fetch('/api/incidents')
      .then(async (response) => {
        const data = await readPayload(response);
        if (!response.ok) throw responseError(data, response.status, 'Incident history unavailable');
        const approvals = data.approvals as (ApprovalItem & ActionOutcome)[] | undefined;
        if (!approvals?.length) return;
        const pending = approvals.find((item) => item.status === 'pending' || item.status === 'awaiting_approval');
        if (pending) setActiveApproval(pending);
        const recent = approvals.find((item) => item.status !== 'pending' && item.status !== 'awaiting_approval');
        if (recent) setActionOutcome(recent);
      })
      .catch(() => {});

    return () => {
      const socket = socketRef.current;
      socketRef.current = null;
      socket?.close();
    };
  }, []);

  // 2. Initialize Cytoscape
  useEffect(() => {
    if (!cyRef.current || topology.length === 0) return;

    if (cyInstance.current) {
      cyInstance.current.destroy();
    }

    const cy = cytoscape({
      container: cyRef.current,
      elements: topology,
      style: [
        {
          selector: 'node',
          style: {
            label: 'data(id)',
            'font-size': '10px',
            color: '#f3f4f6',
            'background-color': '#10b981',
            'text-valign': 'bottom',
            'text-margin-y': 4,
            width: 22,
            height: 22,
          },
        },
        {
          selector: 'node[health = "degraded"]',
          style: {
            'background-color': '#ef4444',
            width: 32,
            height: 32,
          },
        },
        {
          selector: 'node[id = "payments-primary"]',
          style: {
            'background-color': '#ef4444',
            shape: 'diamond',
            width: 36,
            height: 36,
          },
        },
        {
          selector: 'edge[type = "DEPENDS_ON"]',
          style: {
            width: 1.5,
            'line-color': '#4b5563',
            'target-arrow-color': '#4b5563',
            'target-arrow-shape': 'triangle',
            'curve-style': 'bezier',
          },
        },
        {
          selector: 'edge[type = "ROUTES_TO"]',
          style: {
            width: 2,
            'line-color': '#06b6d4',
            'target-arrow-color': '#06b6d4',
            'target-arrow-shape': 'vee',
            'line-style': 'dashed',
            'curve-style': 'bezier',
          },
        },
      ],
      layout: {
        name: 'cose',
        animate: false,
        randomize: false,
      },
    });

    cy.on('tap', 'node', (evt) => {
      const data = evt.target.data() as NodeData;
      setSelectedNode(data);
    });

    cyInstance.current = cy;
    const resize = new ResizeObserver(() => {
      cy.resize();
      cy.fit(undefined, 30);
    });
    resize.observe(cyRef.current);
    return () => {
      resize.disconnect();
      cy.destroy();
    };
  }, [topology]);

  const subscribeToRunEvents = (runId: string) => {
    socketRef.current?.close();
    const protocol = window.location.protocol === 'https:' ? 'wss' : 'ws';
    const socket = new WebSocket(`${protocol}://${window.location.host}/api/runs/${encodeURIComponent(runId)}/events`);
    socketRef.current = socket;
    setRunSubscription(`Waiting for server events for returned run ${runId}.`);

    socket.onmessage = (message) => {
      try {
        const event = JSON.parse(message.data) as RunEvent;
        if (event.run_id !== runId || typeof event.type !== 'string') return;
        setRunSubscription(`Received ${event.type} from run ${runId}.`);
        setTraceLogs((previous) => [{
          tool: event.type,
          graph: 'workflow event',
          cypher: JSON.stringify(event.payload ?? {}),
        }, ...previous]);
      } catch {
        setRunSubscription(`Run events for ${runId} could not be read.`);
      }
    };

    socket.onerror = () => setRunSubscription(`Run event subscription unavailable for ${runId}.`);
    socket.onclose = () => {
      if (socketRef.current === socket) {
        setRunSubscription(`Run event connection closed for ${runId}; received events remain in the trace.`);
      }
    };
  };

  const handleAsk = async () => {
    setLoading(true);
    try {
      const response = await fetch('/api/questions', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ question, service_id: serviceId }),
      });
      const data = await readPayload(response) as {
        answer?: string;
        citations?: CitationItem[];
        sources?: CitationItem[];
        tool_result?: { trace?: TraceLog };
      };
      if (!response.ok) throw responseError(data, response.status, 'Question request failed');
      setAnswer(typeof data.answer === 'string' ? data.answer : null);
      setCitations(Array.isArray(data.citations) ? data.citations : Array.isArray(data.sources) ? data.sources : []);
      if (data.tool_result?.trace) setTraceLogs((previous) => [data.tool_result.trace!, ...previous]);
      setQuestionError(null);
    } catch (error) {
      setQuestionError(error instanceof Error ? error.message : 'Question request could not be completed');
    } finally {
      setLoading(false);
    }
  };

  const handleTriggerIncident = async () => {
    setLoading(true);
    try {
      const response = await fetch('/api/incidents', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ execution_mode: 'simulation', service_id: serviceId, max_cost: 12 }),
      });
      const data = await readPayload(response) as {
        approval_id?: string;
        incident_id?: string;
        run_id?: string;
        proposed_action?: ProposedAction;
        expires_at?: string;
        tool_results?: { trace?: TraceLog }[];
      };
      if (!response.ok) throw responseError(data, response.status, 'Simulation request failed');
      if (typeof data.incident_id !== 'string') throw new Error('Simulation did not return an incident ID');
      if (typeof data.approval_id === 'string') {
        setActiveApproval({
          approval_id: data.approval_id,
          incident_id: data.incident_id,
          service_id: serviceId,
          action: data.proposed_action?.action_kind,
          proposed: data.proposed_action,
          expires_at: data.expires_at,
        });
      }
      const traces = (data.tool_results ?? []).flatMap((result) => result.trace ? [result.trace] : []);
      setTraceLogs((previous) => [...traces, ...previous]);
      if (typeof data.run_id === 'string') subscribeToRunEvents(data.run_id);
      setIncidentError(null);
    } catch (error) {
      setIncidentError(error instanceof Error ? error.message : 'Simulation request could not be completed');
    } finally {
      setLoading(false);
    }
  };

  const handleDecision = async (decision: 'approve' | 'reject') => {
    if (!activeApproval || loading) return;
    setLoading(true);
    try {
      const response = await fetch(`/api/incidents/${activeApproval.incident_id}/${decision}`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ approval_id: activeApproval.approval_id }),
      });
      const data = await readPayload(response);
      if (!response.ok) throw responseError(data, response.status, 'Simulation decision refused');
      if (typeof data.status !== 'string' || typeof data.incident_id !== 'string') {
        throw new Error('Action state was not returned; refresh before another decision');
      }
      setActionOutcome({
        incident_id: data.incident_id,
        approval_id: typeof data.approval_id === 'string' ? data.approval_id : undefined,
        status: data.status as ActionStatus,
        command_id: typeof data.command_id === 'string' ? data.command_id : undefined,
        ssm_status: typeof data.ssm_status === 'string' ? data.ssm_status : undefined,
        recovery_sample_ids: Array.isArray(data.recovery_sample_ids) ? data.recovery_sample_ids.filter((value): value is string => typeof value === 'string') : undefined,
        memory_pending: typeof data.memory_pending === 'boolean' ? data.memory_pending : undefined,
        notification_pending: typeof data.notification_pending === 'boolean' ? data.notification_pending : undefined,
      });
      if (data.status !== 'pending' && data.status !== 'awaiting_approval') setActiveApproval(null);
      setActionError(null);
    } catch (error) {
      setActionError(error instanceof Error ? error.message : 'Simulation decision could not be confirmed; refresh action state');
    } finally {
      setLoading(false);
    }
  };

  const refreshAction = async () => {
    const incidentId = activeApproval?.incident_id ?? actionOutcome?.incident_id;
    if (!incidentId || loading) return;
    setLoading(true);
    try {
      const response = await fetch(`/api/incidents/${incidentId}`);
      const data = await readPayload(response);
      if (!response.ok) throw responseError(data, response.status, 'Action state unavailable');
      if (typeof data.status !== 'string' || typeof data.incident_id !== 'string' || typeof data.service_id !== 'string') {
        throw new Error('Action state was incomplete; refresh before another decision');
      }
      setActionOutcome({
        incident_id: data.incident_id,
        approval_id: typeof data.approval_id === 'string' ? data.approval_id : undefined,
        status: data.status as ActionStatus,
        command_id: typeof data.command_id === 'string' ? data.command_id : undefined,
        ssm_status: typeof data.ssm_status === 'string' ? data.ssm_status : undefined,
        recovery_sample_ids: Array.isArray(data.recovery_sample_ids) ? data.recovery_sample_ids.filter((value): value is string => typeof value === 'string') : undefined,
        memory_pending: typeof data.memory_pending === 'boolean' ? data.memory_pending : undefined,
        notification_pending: typeof data.notification_pending === 'boolean' ? data.notification_pending : undefined,
      });
      if (data.status === 'pending' || data.status === 'awaiting_approval') {
        setActiveApproval({
          approval_id: typeof data.approval_id === 'string' ? data.approval_id : '',
          incident_id: data.incident_id,
          service_id: data.service_id,
          status: data.status as ActionStatus,
          action: typeof data.action_kind === 'string' ? data.action_kind : undefined,
          proposed: data.proposed_action as ProposedAction | undefined,
          expires_at: typeof data.expires_at === 'string' ? data.expires_at : undefined,
        });
      } else {
        setActiveApproval(null);
      }
      setActionError(null);
    } catch (error) {
      setActionError(error instanceof Error ? error.message : 'Current action state is unavailable');
    } finally {
      setLoading(false);
    }
  };

  const openSource = async (citation: CitationItem) => {
    const documentId = citation.document_id ?? citation.doc_id;
    if (!documentId) {
      setSourceError('This citation did not include a source document ID.');
      return;
    }
    setSourceLoading(true);
    try {
      const response = await fetch(`/api/sources/${encodeURIComponent(documentId)}`);
      const data = await readPayload(response);
      if (!response.ok) throw responseError(data, response.status, 'Current source evidence unavailable');
      if (typeof data.document_id !== 'string' || !Array.isArray(data.chunks)) {
        throw new Error('Source response was incomplete; current evidence cannot be displayed');
      }
      setSourceDocument({
        document_id: data.document_id,
        title: typeof data.title === 'string' ? data.title : undefined,
        revision: typeof data.revision === 'string' || typeof data.revision === 'number' ? data.revision : undefined,
        source_version: typeof data.source_version === 'string' ? data.source_version : undefined,
        content_hash: typeof data.content_hash === 'string' ? data.content_hash : undefined,
        permalink: typeof data.permalink === 'string' ? data.permalink : undefined,
        path: typeof data.path === 'string' ? data.path : undefined,
        chunks: data.chunks as CitationChunk[],
      });
      setSourceError(null);
    } catch (error) {
      setSourceError(error instanceof Error ? error.message : 'Current source evidence unavailable');
    } finally {
      setSourceLoading(false);
    }
  };

  return (
    <div className="flex min-h-screen flex-col bg-slate-950 font-sans text-slate-100 lg:h-screen">
      <header className="flex flex-wrap items-center justify-between gap-3 border-b border-slate-800 bg-slate-900/80 px-6 py-3 backdrop-blur">
        <div className="flex flex-wrap items-center gap-2">
          <span className="text-xl font-bold tracking-tight text-cyan-400">ChronosOps</span>
          <span className="rounded border border-slate-700 bg-slate-800 px-2 py-0.5 text-xs text-slate-300">
            Synthetic topology · graph-predicted
          </span>
          <span className="rounded border border-amber-800 bg-amber-950 px-2 py-0.5 text-xs text-amber-300">
            Simulation actions only
          </span>
        </div>
        <div className="flex flex-wrap items-center gap-x-4 gap-y-1 text-xs text-slate-400">
          <span>Authenticated tenant: <strong className="font-medium text-slate-200">{identity?.tenant_id ?? 'identity unavailable'}</strong></span>
          <span>Topology: <strong className="font-medium text-slate-200">{identity?.data_origin ?? 'source identity unavailable'}</strong></span>
          <span>Execution: <strong className="font-medium text-amber-300">{identity?.execution_mode ?? 'identity unavailable'}</strong></span>
          <span>Live recovery: <strong className="font-medium text-amber-300">{identity ? identity.real_recovery_enabled ? 'enabled' : 'disabled' : 'identity unavailable'}</strong></span>
          <span>Live workload monitoring: <strong className="font-medium text-amber-300">unavailable</strong></span>
          <span>Slack live configuration: <strong className="font-medium text-amber-300">unavailable</strong></span>
        </div>
      </header>

      <div className="flex flex-1 flex-col lg:min-h-0 lg:flex-row lg:overflow-hidden">
        <aside className="flex w-full shrink-0 flex-col gap-5 overflow-y-auto border-r border-slate-800 bg-slate-900/40 p-4 lg:w-80">
          <div>
            <h2 className="mb-2 text-xs font-semibold uppercase tracking-wider text-slate-400">Company Brain</h2>
            <div className="flex flex-col gap-2">
              <label className="flex flex-col gap-1 text-xs text-slate-300">
                Canonical service ID
                <select
                  value={serviceId}
                  onChange={(event) => setServiceId(event.target.value)}
                  className="w-full rounded border border-slate-800 bg-slate-950 p-2 text-sm text-slate-200 focus:border-cyan-500 focus:outline-none"
                >
                  {Array.from(new Set(['payments-primary', ...topology
                    .filter((element) => !element.data.source)
                    .map((element) => element.data.id)])).sort().map((id) => (
                    <option key={id} value={id}>{id}</option>
                  ))}
                </select>
              </label>
              <textarea
                value={question}
                onChange={(event) => setQuestion(event.target.value)}
                className="h-20 w-full resize-none rounded border border-slate-800 bg-slate-950 p-2 text-sm text-slate-200 focus:border-cyan-500 focus:outline-none"
                placeholder="Ask about ownership, architecture, or bus factor..."
              />
              <button
                onClick={handleAsk}
                disabled={loading || !question.trim()}
                className="w-full rounded bg-cyan-600 px-3 py-1.5 text-xs font-medium text-white transition hover:bg-cyan-500 disabled:cursor-not-allowed disabled:opacity-50"
              >
                {loading ? 'Consulting graph…' : 'Query Company Brain'}
              </button>
            </div>
            {questionError && <p role="alert" className="mt-2 break-words text-sm text-amber-300">{questionError}</p>}
            <div className="mt-2 flex flex-wrap gap-1">
              <button onClick={() => setQuestion(`Who owns ${serviceId}?`)} className="rounded bg-slate-800 px-2 py-1 text-[10px] text-slate-300 hover:bg-slate-700">Who owns this service?</button>
              <button onClick={() => setQuestion(`What is the bus factor for ${serviceId}?`)} className="rounded bg-slate-800 px-2 py-1 text-[10px] text-slate-300 hover:bg-slate-700">Bus factor?</button>
              <button onClick={() => setQuestion(`Why is ${serviceId} unavailable for shifting?`)} className="rounded bg-slate-800 px-2 py-1 text-[10px] text-slate-300 hover:bg-slate-700">Why unavailable?</button>
              <button onClick={() => setQuestion(`Prior incidents for ${serviceId}?`)} className="rounded bg-slate-800 px-2 py-1 text-[10px] text-slate-300 hover:bg-slate-700">Prior incidents?</button>
            </div>
          </div>

          <div className="border-t border-slate-800 pt-4">
            <h2 className="mb-2 text-xs font-semibold uppercase tracking-wider text-slate-400">Incident simulation</h2>
            <p className="mb-3 text-[11px] leading-relaxed text-slate-400">
              Runs a graph-predicted simulation for the selected canonical service. Approval never authorizes or performs EC2 recovery.
            </p>
            <button
              onClick={handleTriggerIncident}
              disabled={loading}
              className="w-full rounded bg-red-600/90 px-3 py-2 text-xs font-semibold text-white shadow-sm transition hover:bg-red-500 disabled:cursor-not-allowed disabled:opacity-50"
            >
              Simulate {serviceId} outage
            </button>
            {incidentError && <p role="alert" className="mt-2 break-words text-sm text-amber-300">{incidentError}</p>}
          </div>

          {answer && (
            <div className="border-t border-slate-800 pt-4">
              <h2 className="mb-2 text-xs font-semibold uppercase tracking-wider text-cyan-400">Grounded answer</h2>
              <div className="rounded border border-slate-800 bg-slate-950 p-2.5 text-xs leading-relaxed text-slate-300">{answer}</div>
            </div>
          )}
        </aside>

        <main className="relative flex h-[28rem] w-full min-w-0 shrink-0 flex-col overflow-hidden bg-slate-950 lg:h-auto lg:min-h-0 lg:flex-1">
          <div className="absolute left-4 top-3 z-10 flex flex-wrap gap-2 text-[11px]">
            <span className="flex items-center gap-1.5 rounded border border-slate-800 bg-slate-900/90 px-2 py-1 text-slate-300">
              <span className="h-2.5 w-2.5 rounded-full bg-emerald-500"></span> Graph-marked healthy (
              {topology.filter((element) => element.data.health === 'healthy' && !element.data.source).length})
            </span>
            <span className="flex items-center gap-1.5 rounded border border-slate-800 bg-slate-900/90 px-2 py-1 text-slate-300">
              <span className="h-2.5 w-2.5 rounded-full bg-red-500"></span> Graph-marked degraded
            </span>
            <span className="flex items-center gap-1.5 rounded border border-slate-800 bg-slate-900/90 px-2 py-1 text-slate-300">
              <span className="h-2.5 w-2.5 rounded bg-cyan-400"></span> Predicted route (ROUTES_TO)
            </span>
          </div>

          <div ref={cyRef} className="h-full w-full min-w-0 cursor-grab overflow-hidden active:cursor-grabbing" />
        </main>

        <aside className="flex w-full shrink-0 flex-col gap-4 overflow-y-auto border-l border-slate-800 bg-slate-900/40 p-4 lg:w-96">
          <h2 className="text-xs font-semibold uppercase tracking-wider text-slate-400">Evidence & simulation approval</h2>

          {activeApproval ? (
            <div className="flex flex-col gap-2.5 rounded-lg border border-amber-500/50 bg-amber-950/30 p-3.5 shadow-lg">
              <div className="flex items-center justify-between gap-2">
                <span className="text-xs font-bold text-amber-400">Simulation approval required</span>
                <span className="text-[10px] text-amber-200">
                  {activeApproval.expires_at ? `Expires ${new Date(activeApproval.expires_at).toLocaleTimeString()}` : 'Expiry not returned'}
                </span>
              </div>
              <p className="text-xs leading-relaxed text-amber-100">
                This approves a simulation proposal only. It does not submit or authorize EC2 recovery.
              </p>
              <div className="text-xs text-slate-200">
                Proposed action: <code className="font-bold text-cyan-300">{activeApproval.action ?? activeApproval.proposed?.action_kind ?? 'Not returned'}</code>
              </div>
              {activeApproval.proposed?.route && (
                <div className="text-xs text-slate-300">
                  Predicted route: <span className="font-mono text-emerald-400">{activeApproval.proposed.route.join(' → ')}</span>
                  <div className="mt-0.5 text-[11px] text-slate-400">
                    {activeApproval.proposed.latency_ms !== undefined && `Latency: ${activeApproval.proposed.latency_ms}ms · `}
                    {activeApproval.proposed.cost !== undefined ? `Cost: ${activeApproval.proposed.cost}` : 'Cost not returned'}
                  </div>
                </div>
              )}
              <div className="mt-2 flex gap-2">
                <button onClick={() => handleDecision('approve')} disabled={loading} className="flex-1 rounded bg-emerald-600 py-1.5 text-xs font-semibold text-white hover:bg-emerald-500 disabled:cursor-not-allowed disabled:opacity-50">
                  Approve simulation
                </button>
                <button onClick={() => handleDecision('reject')} disabled={loading} className="rounded bg-slate-800 px-3 py-1.5 text-xs text-slate-300 hover:bg-slate-700 disabled:cursor-not-allowed disabled:opacity-50">
                  Reject
                </button>
              </div>
            </div>
          ) : (
            <div className="rounded border border-dashed border-slate-800 p-3 text-center text-xs text-slate-500">No pending simulation approvals</div>
          )}
          {actionError && <p role="alert" className="break-words text-sm text-amber-300">{actionError}</p>}
          {actionOutcome && (
            <div role="status" aria-live="polite" className="flex flex-col gap-2 break-words text-sm text-slate-200">
              <h3 className="font-semibold">Recorded simulation state</h3>
              <p>{actionLabels[actionOutcome.status] ?? 'Action state unavailable — refresh before another decision'}</p>
              <code className="text-xs text-slate-400">{actionOutcome.status}</code>
              <p className="break-all text-xs text-slate-400">{actionOutcome.incident_id}</p>
              {actionOutcome.command_id && <p className="break-all text-xs">SSM command: {actionOutcome.command_id}</p>}
              {actionOutcome.ssm_status && <p className="text-xs">SSM status: {actionOutcome.ssm_status}</p>}
              {!!actionOutcome.recovery_sample_ids?.length && <p className="text-xs">Saved health proof: {actionOutcome.recovery_sample_ids.length} probes</p>}
              {actionOutcome.memory_pending && <p className="text-xs">Memory consolidation pending</p>}
              {actionOutcome.notification_pending && <p className="text-xs">Slack notification pending</p>}
            </div>
          )}
          {(activeApproval || actionOutcome) && (
            <button onClick={refreshAction} disabled={loading} className="text-left text-sm text-cyan-300 disabled:cursor-not-allowed disabled:opacity-50">
              {loading ? 'Waiting for action state…' : 'Refresh action state'}
            </button>
          )}
          {runSubscription && <p role="status" aria-live="polite" className="break-words text-xs text-slate-400">{runSubscription}</p>}

          {selectedNode && (
            <div className="flex flex-col gap-1.5 rounded border border-slate-800 bg-slate-950 p-3 text-xs">
              <div className="text-sm font-bold text-slate-200">{selectedNode.label || selectedNode.id}</div>
              <div className="text-slate-400">Team: <span className="text-slate-200">{selectedNode.owner_team || 'Not returned'}</span></div>
              <div className="text-slate-400">Tier: <span className="text-slate-200">{selectedNode.tier || 'Not returned'}</span></div>
              <div className="text-slate-400">Graph health: <span className={selectedNode.health === 'degraded' ? 'font-bold text-red-400' : 'text-emerald-400'}>{selectedNode.health || 'Not returned'}</span></div>
            </div>
          )}

          {citations.length > 0 && (
            <div className="flex flex-col gap-2">
              <h3 className="text-xs font-semibold text-slate-400">Cited graph sources</h3>
              {citations.map((citation, index) => (
                <div key={citation.document_id ?? citation.doc_id ?? index} className="flex flex-col gap-1 rounded border border-slate-800 bg-slate-950 p-2.5 text-[11px]">
                  <div className="truncate font-mono text-cyan-400">{citation.doc_id || citation.document_id || 'Document ID not returned'}</div>
                  <div className="max-h-16 overflow-hidden text-slate-400 italic">"{citation.excerpt || citation.text || citation.chunks?.[0]?.text || 'Excerpt not returned'}"</div>
                  <button onClick={() => openSource(citation)} disabled={sourceLoading || !(citation.document_id ?? citation.doc_id)} className="mt-1 text-left text-xs text-cyan-300 hover:text-cyan-200 disabled:cursor-not-allowed disabled:opacity-50">
                    {sourceLoading ? 'Opening source…' : 'Open current source evidence'}
                  </button>
                </div>
              ))}
            </div>
          )}
          {sourceError && <p role="alert" className="break-words text-sm text-amber-300">{sourceError}</p>}
          {sourceDocument && (
            <section aria-label="Current source evidence" className="flex flex-col gap-2 rounded border border-cyan-900 bg-slate-950 p-3 text-xs text-slate-300">
              <div className="flex items-center justify-between gap-2">
                <h3 className="font-semibold text-cyan-300">Current source evidence</h3>
                <button onClick={() => setSourceDocument(null)} className="text-xs text-slate-400 hover:text-slate-200">Close</button>
              </div>
              <dl className="grid grid-cols-[auto_1fr] gap-x-2 gap-y-1 break-all">
                <dt className="text-slate-500">Document</dt><dd>{sourceDocument.document_id}</dd>
                <dt className="text-slate-500">Revision</dt><dd>{sourceDocument.revision ?? 'Not returned'}</dd>
                <dt className="text-slate-500">Source version</dt><dd>{sourceDocument.source_version ?? 'Not returned'}</dd>
                <dt className="text-slate-500">Hash</dt><dd className="font-mono text-[10px]">{sourceDocument.content_hash ?? 'Not returned'}</dd>
                <dt className="text-slate-500">Permalink</dt><dd>{sourceDocument.permalink ? <a href={sourceDocument.permalink} target="_blank" rel="noreferrer" className="text-cyan-300 underline underline-offset-2">{sourceDocument.permalink}</a> : 'No permalink on this published source'}</dd>
                <dt className="text-slate-500">Source path</dt><dd>{sourceDocument.path ?? 'Not returned'}</dd>
              </dl>
              <div className="border-t border-slate-800 pt-2">
                <h4 className="mb-1 font-semibold text-slate-200">Published chunks ({sourceDocument.chunks.length})</h4>
                <div className="flex max-h-52 flex-col gap-2 overflow-y-auto">
                  {sourceDocument.chunks.map((chunk, index) => (
                    <article key={chunk.chunk_id ?? index} className="rounded bg-slate-900 p-2">
                      <div className="mb-1 font-mono text-[10px] text-slate-500">{chunk.chunk_id ?? `chunk ${chunk.chunk_index ?? index}`}</div>
                      <p className="whitespace-pre-wrap break-words text-slate-300">{chunk.text ?? chunk.excerpt ?? 'Chunk text not returned'}</p>
                    </article>
                  ))}
                </div>
              </div>
            </section>
          )}
        </aside>
      </div>

      {/* Bottom Collapsible Trace Drawer */}
      <footer className={`border-t border-slate-800 bg-slate-900 transition-all ${isDrawerOpen ? 'h-44' : 'h-10'}`}>
        <button
          type="button"
          onClick={() => setIsDrawerOpen(!isDrawerOpen)}
          aria-expanded={isDrawerOpen}
          className="flex w-full items-center justify-between bg-slate-800/60 px-6 py-1.5 text-xs font-medium text-slate-300 hover:bg-slate-800"
        >
          <span>Server-backed trace ({traceLogs.length} entries)</span>
          <span>{isDrawerOpen ? 'Collapse' : 'Expand'}</span>
        </button>
        {isDrawerOpen && (
          <div className="h-36 space-y-2 overflow-y-auto bg-slate-950 p-3 font-mono text-[11px]">
            {traceLogs.length === 0 ? (
              <div className="italic text-slate-500">No server-backed query or run events have been received.</div>
            ) : (
              traceLogs.map((log, index) => (
                <div key={`${log.tool}-${index}`} className="rounded border border-slate-800/80 bg-slate-900/60 p-2">
                  <div className="mb-1 flex justify-between text-cyan-400">
                    <span>Tool: {log.tool} ({log.graph})</span>
                    <span>{log.duration_ms ? `${log.duration_ms.toFixed(1)}ms` : ''}</span>
                  </div>
                  <div className="break-all text-slate-300">{log.cypher ?? 'No query payload returned'}</div>
                  {log.params && <div className="mt-1 break-all text-slate-400">Params: {JSON.stringify(log.params)}</div>}
                </div>
              ))
            )}
          </div>
        )}
      </footer>
    </div>
  );
}
