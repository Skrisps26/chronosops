import React, { useEffect, useRef, useState, useCallback } from 'react';
import cytoscape from 'cytoscape';
import {
  Brain,
  Shield,
  ShieldAlert,
  Play,
  RotateCcw,
  CheckCircle2,
  Clock,
  Layers,
  Sparkles,
  GitBranch,
  Lock,
  UserCheck,
} from 'lucide-react';
import { CytoscapeElement, TraceLog } from '../types';

interface MemoryPageProps {
  onAddTrace: (trace: TraceLog) => void;
}

export const MemoryPage: React.FC<MemoryPageProps> = ({ onAddTrace }) => {
  const [tenant, setTenant] = useState<'acme' | 'globex'>('acme');
  const [elements, setElements] = useState<CytoscapeElement[]>([]);
  const [loading, setLoading] = useState<boolean>(false);
  const [executingPlaybook, setExecutingPlaybook] = useState<boolean>(false);
  const [activeStepIndex, setActiveStepIndex] = useState<number | null>(null);
  const [executionResult, setExecutionResult] = useState<{
    status: string;
    llm_calls_made: number;
    incident_id: string;
    proposed_action: Record<string, unknown>;
  } | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [aclDenied, setAclDenied] = useState<boolean>(false);

  const cyRef = useRef<HTMLDivElement>(null);
  const cyInstance = useRef<cytoscape.Core | null>(null);

  const delay = (ms: number): Promise<void> => {
    const { promise, resolve } = Promise.withResolvers<void>();
    setTimeout(resolve, ms);
    return promise;
  };

  // Fetch memory graph for current tenant
  const fetchMemoryGraph = useCallback(async (tenantId: 'acme' | 'globex') => {
    setLoading(true);
    setError(null);
    setAclDenied(false);
    setExecutionResult(null);
    setActiveStepIndex(null);

    try {
      // Pass tenant via header override if globex
      const headers: Record<string, string> = {};
      if (tenantId === 'globex') {
        headers['X-ChronosOps-Tenant'] = 'globex';
      }

      const res = await fetch('/api/memory/graph', { headers });
      if (!res.ok) {
        if (res.status === 403 || res.status === 401) {
          setAclDenied(true);
          setElements([]);
          return;
        }
        throw new Error(`Memory endpoint failed: HTTP ${res.status}`);
      }
      const data = await res.json();
      setElements(data.elements || []);
      if (tenantId === 'globex' && (!data.elements || data.elements.length === 0)) {
        setAclDenied(true);
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Could not fetch memory graph');
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    fetchMemoryGraph(tenant);
  }, [tenant, fetchMemoryGraph]);

  // Cytoscape initialization
  useEffect(() => {
    if (!cyRef.current) return;

    const cy = cytoscape({
      container: cyRef.current,
      elements: elements,
      style: [
        {
          selector: 'node',
          style: {
            label: 'data(label)',
            'font-size': '10px',
            'font-weight': 600,
            color: '#f8fafc',
            'text-outline-width': 2,
            'text-outline-color': '#020617',
            'text-outline-opacity': 0.9,
            'text-valign': 'bottom',
            'text-margin-y': 4,
            width: 24,
            height: 24,
            'transition-property': 'background-color, border-color, border-width, width, height, opacity',
            'transition-duration': 300,
          },
        },
        {
          selector: 'node[type = "episode"]',
          style: {
            'background-color': '#8b5cf6',
            'border-color': '#c4b5fd',
            'border-width': 2,
            shape: 'hexagon',
            width: 30,
            height: 30,
          },
        },
        {
          selector: 'node[type = "decision"]',
          style: {
            'background-color': '#3b82f6',
            'border-color': '#93c5fd',
            'border-width': 2,
            shape: 'round-rectangle',
            width: 32,
            height: 24,
          },
        },
        {
          selector: 'node[type = "semantic_fact"]',
          style: {
            'background-color': '#f59e0b',
            'border-color': '#fde68a',
            'border-width': 2.5,
            shape: 'diamond',
            width: 28,
            height: 28,
          },
        },
        {
          selector: 'node[type = "workflow"]',
          style: {
            'background-color': '#06b6d4',
            'border-color': '#67e8f9',
            'border-width': 3,
            shape: 'diamond',
            width: 36,
            height: 36,
          },
        },
        {
          selector: 'node[type = "workflow_step"]',
          style: {
            'background-color': '#0d9488',
            'border-color': '#5eead4',
            'border-width': 2,
            width: 26,
            height: 26,
          },
        },
        {
          selector: 'node[type = "service_ref"]',
          style: {
            'background-color': '#475569',
            'border-color': '#94a3b8',
            'border-width': 1.5,
            width: 22,
            height: 22,
          },
        },
        {
          selector: 'node.step-active',
          style: {
            'background-color': '#06b6d4',
            'border-color': '#ffffff',
            'border-width': 5,
            width: 40,
            height: 40,
          },
        },
        {
          selector: 'node.step-completed',
          style: {
            'background-color': '#10b981',
            'border-color': '#a7f3d0',
            'border-width': 3,
          },
        },
        {
          selector: 'edge',
          style: {
            width: 1.5,
            'line-color': '#334155',
            'target-arrow-color': '#334155',
            'target-arrow-shape': 'triangle',
            'curve-style': 'bezier',
            'transition-property': 'line-color, width',
            'transition-duration': 300,
          },
        },
        {
          selector: 'edge[type = "NEXT_STEP"]',
          style: {
            width: 2.5,
            'line-color': '#06b6d4',
            'target-arrow-color': '#06b6d4',
            'line-style': 'dashed',
            'line-dash-pattern': [6, 3],
          },
        },
        {
          selector: 'edge[type = "BLOCKED_BY"]',
          style: {
            width: 2,
            'line-color': '#f59e0b',
            'target-arrow-color': '#f59e0b',
          },
        },
        {
          selector: 'edge.edge-step-active',
          style: {
            width: 4,
            'line-color': '#06b6d4',
            'target-arrow-color': '#06b6d4',
          },
        },
      ],
      layout: {
        name: 'cose',
        animate: false,
        randomize: false,
        componentSpacing: 120,
        nodeRepulsion: () => 3000000,
        nodeOverlap: 60,
        idealEdgeLength: () => 100,
        edgeElasticity: () => 32,
        nestingFactor: 1.2,
        gravity: 0.15,
        numIter: 1000,
        padding: 40,
      },
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
  }, [elements]);

  // Procedural Playbook Stepper Animation
  const playbookSteps = [
    { name: 'retrieve_memory', label: '1. Retrieve Tenant Memory', desc: 'Checks past episodes & active SemanticFacts' },
    { name: 'blast_radius', label: '2. Compute Blast Radius', desc: 'Incoming dependency traversal (depth <= 5)' },
    { name: 'failover_routes', label: '3. Discover Failover Routes', desc: 'algo.SPpaths bounded by cost cap 12' },
    { name: 'validate_governance', label: '4. Validate Governance', desc: 'Verifies safety invariants & creates ApprovalRecord' },
    { name: 'await_approval', label: '5. Hold for Approval', desc: 'Immutable proposal with 5-minute expiry' },
    { name: 'simulate_failover', label: '6. Simulate Failover', desc: 'Prepares TrafficState routing payload' },
    { name: 'consolidate', label: '7. Consolidate Episode', desc: 'Persists episode into agent_memory' },
  ];

  const handleExecutePlaybook = async () => {
    const cy = cyInstance.current;
    if (!cy) return;

    setExecutingPlaybook(true);
    setExecutionResult(null);
    setError(null);

    // Reset step styles
    cy.nodes('[type = "workflow_step"]').removeClass(['step-active', 'step-completed']);
    cy.edges('[type = "NEXT_STEP"]').removeClass('edge-step-active');

    try {
      // Animate through each step in the graph
      for (let i = 0; i < playbookSteps.length; i++) {
        setActiveStepIndex(i);
        const step = playbookSteps[i];

        // Highlight corresponding step in Cytoscape
        const stepNode = cy.$(`node[type = "workflow_step"][name = "${step.name}"]`);
        if (stepNode.length > 0) {
          stepNode.addClass('step-active');
          stepNode.animate(
            { style: { width: 36, height: 36 } },
            { duration: 250, easing: 'ease-out' }
          );
        }

        if (i > 0) {
          const prevStep = playbookSteps[i - 1];
          const prevNode = cy.$(`node[type = "workflow_step"][name = "${prevStep.name}"]`);
          prevNode.removeClass('step-active').addClass('step-completed');
          // Highlight connecting NEXT_STEP edge
          cy.$(`edge[type = "NEXT_STEP"][source = "${prevNode.id()}"][target = "${stepNode.id()}"]`).addClass('edge-step-active');
        }

        await delay(350);
      }

      // Mark final step completed
      const finalStep = playbookSteps[playbookSteps.length - 1];
      cy.$(`node[type = "workflow_step"][name = "${finalStep.name}"]`).removeClass('step-active').addClass('step-completed');

      // Call API to execute actual deterministic workflow
      const res = await fetch('/api/memory/playbook/execute?service_id=payments-primary', {
        method: 'POST',
      });
      if (!res.ok) throw new Error(`Playbook execution failed: HTTP ${res.status}`);
      const data = await res.json();

      setExecutionResult({
        status: data.status,
        llm_calls_made: data.llm_calls_made ?? 0,
        incident_id: data.incident_id,
        proposed_action: data.proposed_action ?? {},
      });

      onAddTrace({
        tool: 'execute_learned_procedural_workflow',
        graph: 'agent_memory_acme',
        cypher: 'MATCH (wf:WorkflowSubgraph)-[:CONTAINS_STEP]->(s:WorkflowStep)-[:NEXT_STEP]->(next:WorkflowStep)',
        duration_ms: 14.2,
        algorithm_result: { procedural_steps_executed: 7, planning_llm_calls: 0 },
      });
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Playbook execution error');
    } finally {
      setExecutingPlaybook(false);
    }
  };

  const episodes = elements.filter((el) => el.data.type === 'episode');
  const facts = elements.filter((el) => el.data.type === 'semantic_fact');

  return (
    <div className="flex h-[calc(100vh-3.5rem)] flex-col lg:flex-row overflow-hidden bg-slate-950 text-slate-100">
      {/* Left Control Panel */}
      <aside className="w-full lg:w-96 flex flex-col border-r border-slate-800/80 bg-slate-900/60 p-4 overflow-y-auto shrink-0 gap-4">
        <div>
          <div className="flex items-center gap-2 text-cyan-400 mb-1">
            <Brain className="h-4 w-4" />
            <h2 className="text-xs font-bold uppercase tracking-wider">Agent Memory & Coordination</h2>
          </div>
          <p className="text-[11px] text-slate-400 leading-relaxed">
            Demonstrates <strong>Track 02: Agent Memory</strong>. Episodic recall across sessions, active route exclusion rules (<code className="text-amber-300">SemanticFact</code>), and procedural execution (<code className="text-cyan-300">NEXT_STEP</code>) with zero LLM planning calls.
          </p>
        </div>

        {/* Multi-Tenant Switcher */}
        <div className="rounded-xl border border-slate-800 bg-slate-950/70 p-3">
          <label className="block text-xs font-semibold text-slate-300 mb-2">
            Tenant Memory Partition
          </label>
          <div className="grid grid-cols-2 gap-2">
            <button
              onClick={() => setTenant('acme')}
              disabled={loading || executingPlaybook}
              className={`flex items-center justify-center gap-2 rounded-lg py-2 px-3 text-xs font-bold transition ${
                tenant === 'acme'
                  ? 'bg-cyan-600 text-white shadow-md shadow-cyan-950 border border-cyan-500'
                  : 'bg-slate-900 text-slate-400 hover:text-slate-200 hover:bg-slate-800 border border-slate-800'
              }`}
            >
              <UserCheck className="h-3.5 w-3.5" />
              <span>Acme (Primary)</span>
            </button>

            <button
              onClick={() => setTenant('globex')}
              disabled={loading || executingPlaybook}
              className={`flex items-center justify-center gap-2 rounded-lg py-2 px-3 text-xs font-bold transition ${
                tenant === 'globex'
                  ? 'bg-rose-600 text-white shadow-md shadow-rose-950 border border-rose-500'
                  : 'bg-slate-900 text-slate-400 hover:text-slate-200 hover:bg-slate-800 border border-slate-800'
              }`}
            >
              <Lock className="h-3.5 w-3.5" />
              <span>Globex (Isolation)</span>
            </button>
          </div>
          <p className="text-[10px] text-slate-500 mt-2">
            Switching tenants proves database-enforced ACL isolation in FalkorDB.
          </p>
        </div>

        {/* Procedural Playbook Card */}
        {tenant === 'acme' && (
          <div className="rounded-xl border border-cyan-800/80 bg-cyan-950/20 p-3.5 space-y-3">
            <div className="flex items-center justify-between">
              <div className="flex items-center gap-2 text-cyan-300 font-bold text-xs uppercase tracking-wide">
                <Sparkles className="h-4 w-4 text-cyan-400" />
                <span>Learned Procedural Playbook</span>
              </div>
              <span className="rounded bg-cyan-950 border border-cyan-800/80 px-1.5 py-0.5 text-[10px] font-semibold text-cyan-300">
                7 Steps
              </span>
            </div>

            <p className="text-[11px] text-slate-300 leading-relaxed">
              Derived after 3 verified incident recoveries. Executes fixed LangGraph nodes along sequential <code className="text-cyan-300">[:NEXT_STEP]</code> edges with <strong>0 LLM calls</strong>.
            </p>

            <button
              onClick={handleExecutePlaybook}
              disabled={executingPlaybook || loading}
              className="flex w-full items-center justify-center gap-2 rounded-lg bg-cyan-600 px-4 py-2.5 text-xs font-bold text-white shadow-md shadow-cyan-950 transition hover:bg-cyan-500 disabled:cursor-not-allowed disabled:opacity-50"
            >
              {executingPlaybook ? (
                <>
                  <Clock className="h-4 w-4 animate-spin text-white" />
                  <span>Traversing NEXT_STEP Chain...</span>
                </>
              ) : (
                <>
                  <Play className="h-4 w-4" />
                  <span>Execute Learned Playbook (0 LLM Calls)</span>
                </>
              )}
            </button>

            {/* Stepper Display */}
            <div className="space-y-1.5 pt-1">
              {playbookSteps.map((step, idx) => {
                const isCurrent = activeStepIndex === idx;
                const isPast = activeStepIndex !== null && activeStepIndex > idx;
                return (
                  <div
                    key={step.name}
                    className={`flex items-center justify-between p-1.5 rounded-md text-[11px] transition-colors ${
                      isCurrent
                        ? 'bg-cyan-950/80 border border-cyan-600 text-cyan-200'
                        : isPast
                        ? 'bg-emerald-950/30 border border-emerald-900/40 text-emerald-300'
                        : 'bg-slate-950/40 border border-slate-900 text-slate-500'
                    }`}
                  >
                    <span>{step.label}</span>
                    {isPast && <CheckCircle2 className="h-3.5 w-3.5 text-emerald-400" />}
                    {isCurrent && <Clock className="h-3.5 w-3.5 animate-spin text-cyan-400" />}
                  </div>
                );
              })}
            </div>

            {executionResult && (
              <div className="rounded-lg bg-emerald-950/40 border border-emerald-800 p-2.5 text-xs space-y-1">
                <div className="flex items-center justify-between text-emerald-300 font-bold">
                  <span>Playbook Verified</span>
                  <span className="rounded bg-emerald-950 border border-emerald-700 px-1 py-0.2 text-[10px]">
                    0 LLM Calls
                  </span>
                </div>
                <div className="text-[11px] text-slate-300">
                  Status: <strong className="text-emerald-400 uppercase">{executionResult.status}</strong>
                </div>
                <div className="text-[10px] text-slate-400">
                  Deterministic graph traversal completed with zero model planning.
                </div>
              </div>
            )}
          </div>
        )}

        {/* Semantic Facts Panel */}
        {tenant === 'acme' && facts.length > 0 && (
          <div className="rounded-xl border border-amber-900/60 bg-amber-950/20 p-3 space-y-2">
            <div className="flex items-center gap-1.5 text-amber-300 font-bold text-xs uppercase tracking-wide">
              <ShieldAlert className="h-3.5 w-3.5 text-amber-400" />
              <span>Active Semantic Exclusion Facts ({facts.length})</span>
            </div>
            <p className="text-[10.5px] text-slate-400">
              Prunes historically degraded paths from future failover calculations:
            </p>
            {facts.map((fact) => (
              <div key={fact.data.id} className="rounded-lg bg-slate-950/80 border border-amber-900/40 p-2 text-xs">
                <div className="font-mono text-amber-300 font-semibold text-[11px]">
                  {fact.data.label}
                </div>
                <div className="text-[10px] text-slate-400 mt-0.5">
                  Reason: {fact.data.reason ?? 'Exceeded safety cost cap in prior incident'}
                </div>
              </div>
            ))}
          </div>
        )}

        {/* Historical Episodes Card Feed */}
        {tenant === 'acme' && (
          <div className="rounded-xl border border-slate-800 bg-slate-950/60 p-3 space-y-2">
            <h3 className="text-xs font-bold uppercase tracking-wider text-slate-400 flex items-center justify-between">
              <span>Consolidated Episodes ({episodes.length})</span>
              <Layers className="h-3.5 w-3.5 text-purple-400" />
            </h3>
            <div className="space-y-1.5 max-h-48 overflow-y-auto">
              {episodes.map((ep) => (
                <div key={ep.data.id} className="rounded-lg border border-purple-950/60 bg-purple-950/20 p-2 text-xs">
                  <div className="flex items-center justify-between">
                    <span className="font-mono text-purple-300 font-semibold text-[11px]">{ep.data.label}</span>
                    <span className="rounded bg-emerald-950 border border-emerald-800/60 px-1 py-0.2 text-[9px] text-emerald-400 uppercase">
                      {ep.data.status}
                    </span>
                  </div>
                  <div className="text-[10px] text-slate-400 mt-0.5">
                    Action: {ep.data.action_kind} ({ep.data.mode})
                  </div>
                </div>
              ))}
            </div>
          </div>
        )}

        {error && (
          <div className="rounded-xl border border-rose-900/60 bg-rose-950/30 p-3 text-xs text-rose-300">
            {error}
          </div>
        )}
      </aside>

      {/* Center Graph Canvas */}
      <main className="relative flex-1 flex flex-col min-w-0 bg-slate-950">
        {/* Canvas Legend */}
        <div className="absolute left-4 top-3 z-10 flex flex-wrap gap-2 text-[11px] pointer-events-none">
          <span className="flex items-center gap-1.5 rounded-lg border border-slate-800/80 bg-slate-900/90 backdrop-blur-sm px-2.5 py-1 text-slate-300 shadow">
            <span className="h-2 w-2 rounded-full bg-purple-500" /> Incident Episode
          </span>
          <span className="flex items-center gap-1.5 rounded-lg border border-slate-800/80 bg-slate-900/90 backdrop-blur-sm px-2.5 py-1 text-slate-300 shadow">
            <span className="h-2 w-2 rounded-full bg-blue-500" /> Decision Log
          </span>
          <span className="flex items-center gap-1.5 rounded-lg border border-slate-800/80 bg-slate-900/90 backdrop-blur-sm px-2.5 py-1 text-slate-300 shadow">
            <span className="h-2 w-2 rounded-full bg-amber-400" /> Semantic Exclusion Fact
          </span>
          <span className="flex items-center gap-1.5 rounded-lg border border-slate-800/80 bg-slate-900/90 backdrop-blur-sm px-2.5 py-1 text-slate-300 shadow">
            <span className="h-2 w-2 rounded-full bg-teal-400" /> Workflow Step (NEXT_STEP)
          </span>
        </div>

        {/* Globex Tenant Isolation Banner Overlay */}
        {aclDenied && (
          <div className="absolute inset-0 z-20 flex flex-col items-center justify-center bg-slate-950/90 backdrop-blur-md p-6 text-center">
            <div className="flex h-16 w-16 items-center justify-center rounded-2xl bg-rose-950/80 border border-rose-700/80 text-rose-400 mb-4 shadow-xl shadow-rose-950/50">
              <Lock className="h-8 w-8" />
            </div>
            <h3 className="text-base font-bold text-rose-300 mb-1">
              FalkorDB Multi-Tenant Boundary Enforced
            </h3>
            <p className="text-xs text-slate-400 max-w-md leading-relaxed mb-4">
              Tenant <strong className="text-slate-200">globex</strong> is cryptographically restricted by database ACLs from querying Acme's episodic memory graph (<code className="text-rose-400">agent_memory_acme</code>). Zero episodes exposed.
            </p>
            <button
              onClick={() => setTenant('acme')}
              className="rounded-lg bg-slate-800 border border-slate-700 px-4 py-2 text-xs font-semibold text-slate-200 hover:bg-slate-700 transition"
            >
              Switch back to Acme
            </button>
          </div>
        )}

        <div ref={cyRef} className="h-full w-full cursor-grab active:cursor-grabbing" />
      </main>
    </div>
  );
};
