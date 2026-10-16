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

interface MonitoringState {
  healthy: boolean;
  status: string;
  target: string;
}

interface CitationChunk {
  chunk_id?: string;
  text?: string;
}

interface CitationItem {
  doc_id?: string;
  document_id?: string;
  excerpt?: string;
  chunks?: CitationChunk[];
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

interface ApprovalItem {
  approval_id: string;
  incident_id: string;
  service_id: string;
  action: string;
  status?: string;
  proposed?: ProposedAction;
}

interface TraceLog {
  tool: string;
  graph: string;
  cypher: string;
  params?: Record<string, unknown>;
  duration_ms?: number;
  algorithm_result?: unknown;
}

export default function App() {
  const [topology, setTopology] = useState<CytoscapeElement[]>([]);
  const [monitoring, setMonitoring] = useState<MonitoringState | null>(null);
  const [selectedNode, setSelectedNode] = useState<NodeData | null>(null);
  const [question, setQuestion] = useState('Who owns payments and why is the backup unavailable?');
  const [answer, setAnswer] = useState<string | null>(null);
  const [citations, setCitations] = useState<CitationItem[]>([]);
  const [activeApproval, setActiveApproval] = useState<ApprovalItem | null>(null);
  const [traceLogs, setTraceLogs] = useState<TraceLog[]>([]);
  const [isDrawerOpen, setIsDrawerOpen] = useState(true);
  const [loading, setLoading] = useState(false);
  const cyRef = useRef<HTMLDivElement>(null);
  const cyInstance = useRef<cytoscape.Core | null>(null);

  // 1. Fetch initial topology and monitoring
  useEffect(() => {
    fetch('/api/monitoring')
      .then((r) => r.json())
      .then((data: MonitoringState) => setMonitoring(data))
      .catch(() => {});

    fetch('/api/topology')
      .then((r) => r.json())
      .then((data: { elements?: CytoscapeElement[] }) => {
        setTopology(data.elements || []);
      })
      .catch(() => {});

    fetch('/api/incidents')
      .then((r) => r.json())
      .then((data: { approvals?: ApprovalItem[] }) => {
        if (data.approvals && data.approvals.length > 0) {
          const pending = data.approvals.find((a) => a.status === 'pending');
          if (pending) setActiveApproval(pending);
        }
      })
      .catch(() => {});
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
            label: 'data(label)',
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
  }, [topology]);

  const handleAsk = async () => {
    setLoading(true);
    try {
      const res = await fetch('/api/questions', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ question }),
      });
      const data = (await res.json()) as {
        answer?: string;
        citations?: CitationItem[];
        sources?: CitationItem[];
        tool_result?: { trace?: TraceLog };
      };
      setAnswer(data.answer || null);
      setCitations(data.citations || data.sources || []);
      if (data.tool_result?.trace) {
        setTraceLogs((prev) => [data.tool_result!.trace!, ...prev]);
      }
    } finally {
      setLoading(false);
    }
  };

  const handleTriggerIncident = async () => {
    setLoading(true);
    try {
      const res = await fetch('/api/incidents', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          execution_mode: 'simulation',
          service_id: 'payments-primary',
          max_cost: 12,
        }),
      });
      const data = (await res.json()) as {
        approval_id?: string;
        incident_id: string;
        proposed_action?: ProposedAction;
      };
      if (data.approval_id) {
        setActiveApproval({
          approval_id: data.approval_id,
          incident_id: data.incident_id,
          service_id: 'payments-primary',
          action: data.proposed_action?.action_kind || 'simulate_failover',
          proposed: data.proposed_action,
        });
      }
    } finally {
      setLoading(false);
    }
  };

  const handleApprove = async () => {
    if (!activeApproval) return;
    setLoading(true);
    try {
      const res = await fetch(`/api/incidents/${activeApproval.incident_id}/approve`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ approval_id: activeApproval.approval_id }),
      });
      const data = (await res.json()) as { status: string };
      alert(`Action Approved! Outcome: ${data.status}`);
      setActiveApproval(null);
    } finally {
      setLoading(false);
    }
  };

  const handleReject = async () => {
    if (!activeApproval) return;
    setLoading(true);
    try {
      await fetch(`/api/incidents/${activeApproval.incident_id}/reject`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ approval_id: activeApproval.approval_id }),
      });
      setActiveApproval(null);
    } finally {
      setLoading(false);
    }
  };

  return (
    <div className="flex flex-col h-screen bg-slate-950 text-slate-100 font-sans">
      {/* Top Header */}
      <header className="flex items-center justify-between px-6 py-3 border-b border-slate-800 bg-slate-900/80 backdrop-blur">
        <div className="flex items-center gap-3">
          <span className="text-xl font-bold tracking-tight text-cyan-400">⏱️ ChronosOps</span>
          <span className="text-xs px-2 py-0.5 rounded bg-slate-800 text-slate-400 border border-slate-700">
            Tenant: acme
          </span>
          <span className="text-xs px-2 py-0.5 rounded bg-emerald-950 text-emerald-400 border border-emerald-800">
            FalkorDB Connected
          </span>
          <span className="text-xs px-2 py-0.5 rounded bg-slate-800 text-slate-300">
            Synthetic Topology • Real EC2 & Slack Integration
          </span>
        </div>
        <div className="flex items-center gap-4 text-xs text-slate-400">
          <div>
            Monitoring:{' '}
            <span className={monitoring?.healthy ? 'text-emerald-400 font-medium' : 'text-amber-400 font-medium'}>
              {monitoring?.status || 'Active'}
            </span>
          </div>
          <div className="text-slate-500">|</div>
          <div>
            Bedrock: <span className="text-cyan-400">Nova Lite (us-east-1)</span>
          </div>
        </div>
      </header>

      {/* Main 3-Column Layout */}
      <div className="flex flex-1 overflow-hidden">
        {/* Left Panel: Questions & Incident Trigger */}
        <aside className="w-80 border-r border-slate-800 p-4 flex flex-col gap-5 overflow-y-auto bg-slate-900/40">
          <div>
            <h2 className="text-xs uppercase tracking-wider font-semibold text-slate-400 mb-2">Company Brain</h2>
            <div className="flex flex-col gap-2">
              <textarea
                value={question}
                onChange={(e) => setQuestion(e.target.value)}
                className="w-full h-20 text-xs p-2 rounded bg-slate-950 border border-slate-800 text-slate-200 focus:outline-none focus:border-cyan-500 resize-none"
                placeholder="Ask about ownership, architecture, or bus factor..."
              />
              <button
                onClick={handleAsk}
                disabled={loading}
                className="w-full py-1.5 px-3 bg-cyan-600 hover:bg-cyan-500 text-white rounded text-xs font-medium transition"
              >
                {loading ? 'Consulting Graph...' : 'Query Company Brain'}
              </button>
            </div>
            {/* Quick prompts */}
            <div className="flex flex-wrap gap-1 mt-2">
              <button
                onClick={() => setQuestion('Who owns payments-primary?')}
                className="text-[10px] px-2 py-1 rounded bg-slate-800 hover:bg-slate-700 text-slate-300"
              >
                Who owns payments?
              </button>
              <button
                onClick={() => setQuestion('What is the bus factor for payments-primary?')}
                className="text-[10px] px-2 py-1 rounded bg-slate-800 hover:bg-slate-700 text-slate-300"
              >
                Bus factor?
              </button>
              <button
                onClick={() => setQuestion('Why is payments-backup unavailable for live shifting?')}
                className="text-[10px] px-2 py-1 rounded bg-slate-800 hover:bg-slate-700 text-slate-300"
              >
                Why backup unavailable?
              </button>
            </div>
          </div>

          <div className="border-t border-slate-800 pt-4">
            <h2 className="text-xs uppercase tracking-wider font-semibold text-slate-400 mb-2">Incident Simulation</h2>
            <p className="text-[11px] text-slate-400 mb-3">
              Trigger graph algorithms to calculate downstream blast radius and safe failover under financial risk caps.
            </p>
            <button
              onClick={handleTriggerIncident}
              disabled={loading}
              className="w-full py-2 px-3 bg-red-600/90 hover:bg-red-500 text-white rounded text-xs font-semibold shadow-sm transition"
            >
              Simulate payments-primary Outage
            </button>
          </div>

          {answer && (
            <div className="border-t border-slate-800 pt-4">
              <h2 className="text-xs uppercase tracking-wider font-semibold text-cyan-400 mb-2">Grounded Answer</h2>
              <div className="p-2.5 rounded bg-slate-950 border border-slate-800 text-xs text-slate-300 leading-relaxed">
                {answer}
              </div>
            </div>
          )}
        </aside>

        {/* Center: Cytoscape Topology */}
        <main className="flex-1 relative bg-slate-950 flex flex-col">
          <div className="absolute top-3 left-4 z-10 flex gap-2 text-[11px]">
            <span className="flex items-center gap-1.5 px-2 py-1 rounded bg-slate-900/90 border border-slate-800 text-slate-300">
              <span className="w-2.5 h-2.5 rounded-full bg-emerald-500"></span> Healthy (
              {topology.filter((e) => e.data.health !== 'degraded' && !e.data.source).length})
            </span>
            <span className="flex items-center gap-1.5 px-2 py-1 rounded bg-slate-900/90 border border-slate-800 text-slate-300">
              <span className="w-2.5 h-2.5 rounded-full bg-red-500"></span> Degraded (payments-primary)
            </span>
            <span className="flex items-center gap-1.5 px-2 py-1 rounded bg-slate-900/90 border border-slate-800 text-slate-300">
              <span className="w-2.5 h-2.5 rounded bg-cyan-400"></span> Candidate Route (ROUTES_TO)
            </span>
          </div>

          <div ref={cyRef} className="w-full h-full cursor-grab active:cursor-grabbing" />
        </main>

        {/* Right Column: Evidence & Approval Box */}
        <aside className="w-96 border-l border-slate-800 p-4 flex flex-col gap-4 overflow-y-auto bg-slate-900/40">
          <h2 className="text-xs uppercase tracking-wider font-semibold text-slate-400">Incident Evidence</h2>

          {/* Pending Approval Box */}
          {activeApproval ? (
            <div className="p-3.5 rounded-lg bg-amber-950/30 border border-amber-500/50 flex flex-col gap-2.5 shadow-lg">
              <div className="flex items-center justify-between">
                <span className="text-xs font-bold text-amber-400 flex items-center gap-1">
                  ⚠️ Human Approval Required
                </span>
                <span className="text-[10px] text-amber-300/70 font-mono">5m expiry</span>
              </div>
              <div className="text-xs text-slate-200">
                Action:{' '}
                <code className="text-cyan-300 font-bold">
                  {activeApproval.action || activeApproval.proposed?.action_kind}
                </code>
              </div>
              {activeApproval.proposed?.route && (
                <div className="text-xs text-slate-300">
                  Safe Route:{' '}
                  <span className="font-mono text-emerald-400">
                    {activeApproval.proposed.route.join(' → ')}
                  </span>
                  <div className="text-[11px] text-slate-400 mt-0.5">
                    Latency: {activeApproval.proposed.latency_ms}ms • Cost: {activeApproval.proposed.cost} (Budget: 12)
                  </div>
                </div>
              )}
              <div className="flex gap-2 mt-2">
                <button
                  onClick={handleApprove}
                  className="flex-1 py-1.5 bg-emerald-600 hover:bg-emerald-500 text-white rounded text-xs font-semibold"
                >
                  Approve Action
                </button>
                <button
                  onClick={handleReject}
                  className="py-1.5 px-3 bg-slate-800 hover:bg-slate-700 text-slate-300 rounded text-xs"
                >
                  Reject
                </button>
              </div>
            </div>
          ) : (
            <div className="p-3 rounded border border-dashed border-slate-800 text-xs text-slate-500 text-center">
              No pending approval requests
            </div>
          )}

          {/* Selected Node Inspector */}
          {selectedNode && (
            <div className="p-3 rounded bg-slate-950 border border-slate-800 text-xs flex flex-col gap-1.5">
              <div className="font-bold text-slate-200 text-sm">{selectedNode.label || selectedNode.id}</div>
              <div className="text-slate-400">
                Team: <span className="text-slate-200">{selectedNode.owner_team || 'N/A'}</span>
              </div>
              <div className="text-slate-400">
                Tier: <span className="text-slate-200">{selectedNode.tier || 'N/A'}</span>
              </div>
              <div className="text-slate-400">
                Health:{' '}
                <span
                  className={
                    selectedNode.health === 'degraded' ? 'text-red-400 font-bold' : 'text-emerald-400'
                  }
                >
                  {selectedNode.health || 'healthy'}
                </span>
              </div>
            </div>
          )}

          {/* Citations Box */}
          {citations.length > 0 && (
            <div className="flex flex-col gap-2">
              <h3 className="text-xs font-semibold text-slate-400">Cited Graph Sources</h3>
              {citations.map((c, i) => (
                <div
                  key={i}
                  className="p-2.5 rounded bg-slate-950 border border-slate-800 text-[11px] flex flex-col gap-1"
                >
                  <div className="font-mono text-cyan-400 truncate">{c.doc_id || c.document_id}</div>
                  <div className="text-slate-400 line-clamp-3 italic">
                    "{c.excerpt || c.chunks?.[0]?.text || 'Document citation'}"
                  </div>
                </div>
              ))}
            </div>
          )}
        </aside>
      </div>

      {/* Bottom Collapsible Trace Drawer */}
      <footer className={`border-t border-slate-800 bg-slate-900 transition-all ${isDrawerOpen ? 'h-44' : 'h-8'}`}>
        <div
          onClick={() => setIsDrawerOpen(!isDrawerOpen)}
          className="flex items-center justify-between px-6 py-1.5 bg-slate-800/60 cursor-pointer hover:bg-slate-800 text-xs text-slate-300 font-medium"
        >
          <span>🔍 Glass-Box Trace Drawer ({traceLogs.length} events logged)</span>
          <span>{isDrawerOpen ? '▼ Collapse' : '▲ Expand'}</span>
        </div>
        {isDrawerOpen && (
          <div className="p-3 h-36 overflow-y-auto font-mono text-[11px] space-y-2 bg-slate-950">
            {traceLogs.length === 0 ? (
              <div className="text-slate-500 italic">No tool calls executed yet. Run a query or incident triage.</div>
            ) : (
              traceLogs.map((log, idx) => (
                <div key={idx} className="p-2 rounded bg-slate-900/60 border border-slate-800/80">
                  <div className="flex justify-between text-cyan-400 mb-1">
                    <span>
                      Tool: {log.tool} ({log.graph})
                    </span>
                    <span>{log.duration_ms ? `${log.duration_ms.toFixed(1)}ms` : ''}</span>
                  </div>
                  <div className="text-slate-300 break-all">{log.cypher}</div>
                </div>
              ))
            )}
          </div>
        )}
      </footer>
    </div>
  );
}
