import React, { useEffect, useRef, useState, useCallback } from 'react';
import cytoscape from 'cytoscape';
import {
  Activity,
  AlertTriangle,
  CheckCircle2,
  Clock,
  Play,
  RotateCcw,
  Check,
  X,
  Sliders,
  Radio,
  ArrowRight,
} from 'lucide-react';
import { CytoscapeElement, NodeData, ProposedAction, TraceLog, ActionStatus } from '../types';

interface SimulationPageProps {
  onAddTrace: (trace: TraceLog) => void;
}

export const SimulationPage: React.FC<SimulationPageProps> = ({ onAddTrace }) => {
  const [topology, setTopology] = useState<CytoscapeElement[]>([]);
  const [serviceId, setServiceId] = useState<string>('payments-primary');
  const [maxCost, setMaxCost] = useState<number>(12);
  const [loading, setLoading] = useState<boolean>(false);
  const [simulationPhase, setSimulationPhase] = useState<
    'idle' | 'outage' | 'blast_radius' | 'path_search' | 'awaiting_approval' | 'applied' | 'rejected'
  >('idle');
  const [activeApproval, setActiveApproval] = useState<{
    approval_id: string;
    incident_id: string;
    expires_at?: string;
    proposed: ProposedAction;
  } | null>(null);
  const [actionOutcome, setActionOutcome] = useState<{
    status: ActionStatus;
    incident_id: string;
    active_route?: string[];
  } | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [selectedNode, setSelectedNode] = useState<NodeData | null>(null);
  const [blastServices, setBlastServices] = useState<string[]>([]);

  const cyRef = useRef<HTMLDivElement>(null);
  const cyInstance = useRef<cytoscape.Core | null>(null);
  const packetAnimRef = useRef<number | null>(null);
  const dashOffsetRef = useRef<number>(0);

  // Fetch base ops_topology
  const fetchTopology = useCallback(async () => {
    try {
      const response = await fetch('/api/topology');
      if (!response.ok) throw new Error('Topology endpoint unavailable');
      const data = await response.json();
      setTopology(data.elements || []);
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Could not fetch topology');
    }
  }, []);

  useEffect(() => {
    fetchTopology();
  }, [fetchTopology]);

  // Initialize and style Cytoscape
  useEffect(() => {
    if (!cyRef.current || topology.length === 0) return;

    const cy = cytoscape({
      container: cyRef.current,
      elements: topology,
      style: [
        {
          selector: 'node',
          style: {
            label: 'data(id)',
            'font-size': '10px',
            'font-weight': 600,
            color: '#f8fafc',
            'text-outline-width': 2,
            'text-outline-color': '#020617',
            'text-outline-opacity': 0.9,
            'background-color': '#10b981',
            'text-valign': 'bottom',
            'text-margin-y': 4,
            width: 22,
            height: 22,
            'transition-property': 'background-color, border-color, border-width, width, height, opacity',
            'transition-duration': 300,
          },
        },
        {
          selector: 'node[id = "payments-primary"]',
          style: {
            shape: 'diamond',
            width: 32,
            height: 32,
          },
        },
        {
          selector: 'node.outage-node',
          style: {
            'background-color': '#f43f5e',
            'border-color': '#fda4af',
            'border-width': 4,
            width: 38,
            height: 38,
          },
        },
        {
          selector: 'node.blast-affected',
          style: {
            'background-color': '#f59e0b',
            'border-color': '#fde68a',
            'border-width': 3,
            width: 28,
            height: 28,
          },
        },
        {
          selector: 'node.route-active',
          style: {
            'background-color': '#06b6d4',
            'border-color': '#a5f3fc',
            'border-width': 3,
            width: 28,
            height: 28,
          },
        },
        {
          selector: 'node.route-rejected',
          style: {
            'background-color': '#ef4444',
            'border-color': '#fca5a5',
            'border-width': 2,
          },
        },
        {
          selector: 'edge[type = "DEPENDS_ON"]',
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
          selector: 'edge[type = "ROUTES_TO"]',
          style: {
            width: 2,
            'line-color': '#475569',
            'target-arrow-color': '#475569',
            'target-arrow-shape': 'vee',
            'line-style': 'dashed',
            'line-dash-pattern': [6, 3],
            'curve-style': 'bezier',
            'transition-property': 'line-color, width',
            'transition-duration': 300,
          },
        },
        {
          selector: 'edge.edge-blast',
          style: {
            'line-color': '#f59e0b',
            'target-arrow-color': '#f59e0b',
            width: 3,
          },
        },
        {
          selector: 'edge.edge-eval-rejected',
          style: {
            'line-color': '#ef4444',
            'target-arrow-color': '#ef4444',
            width: 2.5,
          },
        },
        {
          selector: 'edge.edge-route-active',
          style: {
            'line-color': '#06b6d4',
            'target-arrow-color': '#06b6d4',
            width: 4,
          },
        },
      ],
      layout: {
        name: 'cose',
        animate: false,
        randomize: false,
        componentSpacing: 100,
        nodeRepulsion: () => 2048000,
        nodeOverlap: 50,
        idealEdgeLength: () => 90,
        edgeElasticity: () => 32,
        nestingFactor: 1.2,
        gravity: 0.15,
        numIter: 1200,
        initialTemp: 1000,
        coolingFactor: 0.99,
        minTemp: 1.0,
        padding: 30,
      },
    });

    cy.on('tap', 'node', (evt) => {
      setSelectedNode(evt.target.data() as NodeData);
    });

    cyInstance.current = cy;

    const resize = new ResizeObserver(() => {
      cy.resize();
      cy.fit(undefined, 30);
    });
    resize.observe(cyRef.current);

    return () => {
      if (packetAnimRef.current) cancelAnimationFrame(packetAnimRef.current);
      resize.disconnect();
      cy.destroy();
    };
  }, [topology]);

  // Clean animation state
  const resetGraphVisuals = useCallback(() => {
    if (packetAnimRef.current) {
      cancelAnimationFrame(packetAnimRef.current);
      packetAnimRef.current = null;
    }
    const cy = cyInstance.current;
    if (!cy) return;
    cy.elements().removeClass([
      'outage-node',
      'blast-affected',
      'route-active',
      'route-rejected',
      'edge-blast',
      'edge-eval-rejected',
      'edge-route-active',
    ]);
    cy.edges().style({ 'line-dash-offset': 0 });
  }, []);

  // Multi-phase simulation animation runner
  const runSimulationWorkflow = async () => {
    const cy = cyInstance.current;
    if (!cy) return;

    setLoading(true);
    setError(null);
    resetGraphVisuals();
    setSimulationPhase('outage');

    try {
      // 1. Outage Detonation: Target node pulses red
      const targetNode = cy.$(`node[id = "${serviceId}"]`);
      if (targetNode.length > 0) {
        targetNode.addClass('outage-node');
        targetNode.animate(
          { style: { width: 44, height: 44 } },
          { duration: 400, easing: 'ease-out' }
        );
      }

      const delay = (ms: number) => {
        const { promise, resolve } = Promise.withResolvers<void>();
        setTimeout(resolve, ms);
        return promise;
      };

      // Small delay for visual impact
      await delay(600);
      // 2. Blast Radius Animation: Calculate & Ripple outward through incoming DEPENDS_ON
      setSimulationPhase('blast_radius');
      const blastDeps = ['checkout', 'gateway', 'orders', 'worker'];
      setBlastServices(blastDeps);

      for (let i = 0; i < blastDeps.length; i++) {
        const depId = blastDeps[i];
        const node = cy.$(`node[id = "${depId}"]`);
        node.addClass('blast-affected');
        // Light up connected dependency edge
        cy.$(`edge[source = "${depId}"][target = "${serviceId}"]`).addClass('edge-blast');
        cy.$(`edge[source = "${depId}"][target = "checkout"]`).addClass('edge-blast');
        await delay(250);
      }

      await delay(500);

      // 3. Path Search Animation (algo.SPpaths): Compare candidate paths
      setSimulationPhase('path_search');
      // Evaluate rejected path (checkout -> premium -> payments-backup)
      const rejectedEdge1 = cy.$('edge[source = "checkout"][target = "premium"]');
      const rejectedEdge2 = cy.$('edge[source = "premium"][target = "payments-backup"]');
      const premiumNode = cy.$('node[id = "premium"]');

      rejectedEdge1.addClass('edge-eval-rejected');
      rejectedEdge2.addClass('edge-eval-rejected');
      premiumNode.addClass('route-rejected');
      await delay(700);

      // Now highlight accepted path (checkout -> queue -> payments-backup)
      const approvedEdge1 = cy.$('edge[source = "checkout"][target = "queue"]');
      const approvedEdge2 = cy.$('edge[source = "queue"][target = "payments-backup"]');
      const queueNode = cy.$('node[id = "queue"]');
      const backupNode = cy.$('node[id = "payments-backup"]');

      approvedEdge1.addClass('edge-route-active');
      approvedEdge2.addClass('edge-route-active');
      queueNode.addClass('route-active');
      backupNode.addClass('route-active');

      // Call API to generate server-side proposal
      const res = await fetch('/api/incidents', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          service_id: serviceId,
          execution_mode: 'simulation',
          max_cost: maxCost,
          session_id: 'console-simulation-run',
        }),
      });

      if (!res.ok) throw new Error(`Simulation failed: HTTP ${res.status}`);
      const data = await res.json();

      if (data.tool_results) {
        data.tool_results.forEach((tr: { trace?: TraceLog }) => {
          if (tr.trace) onAddTrace(tr.trace);
        });
      }

      setActiveApproval({
        approval_id: data.approval_id,
        incident_id: data.incident_id,
        expires_at: data.expires_at,
        proposed: data.proposed_action,
      });

      setSimulationPhase('awaiting_approval');
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Simulation failed');
      setSimulationPhase('idle');
    } finally {
      setLoading(false);
    }
  };

  // Start continuous traffic packet animation loop along approved route
  const startPacketAnimation = () => {
    const cy = cyInstance.current;
    if (!cy) return;

    const activeEdges = cy.$('edge.edge-route-active');
    if (activeEdges.length === 0) return;

    const animatePacket = () => {
      dashOffsetRef.current = (dashOffsetRef.current - 1.5) % 18;
      activeEdges.style({
        'line-dash-offset': dashOffsetRef.current,
      });
      packetAnimRef.current = requestAnimationFrame(animatePacket);
    };

    packetAnimRef.current = requestAnimationFrame(animatePacket);
  };

  // Decision handler (Approve / Reject)
  const handleDecision = async (decision: 'approve' | 'reject') => {
    if (!activeApproval) return;
    setLoading(true);
    setError(null);

    try {
      const res = await fetch(`/api/incidents/${activeApproval.incident_id}/${decision}`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ approval_id: activeApproval.approval_id }),
      });

      if (!res.ok) throw new Error(`Action decision refused: HTTP ${res.status}`);
      const data = await res.json();

      setActionOutcome({
        status: data.status,
        incident_id: data.incident_id,
        active_route: data.active_route,
      });

      if (decision === 'approve') {
        setSimulationPhase('applied');
        startPacketAnimation();
      } else {
        setSimulationPhase('rejected');
        resetGraphVisuals();
      }
      setActiveApproval(null);
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Decision failed');
    } finally {
      setLoading(false);
    }
  };

  const handleReset = () => {
    resetGraphVisuals();
    setActiveApproval(null);
    setActionOutcome(null);
    setSimulationPhase('idle');
    setBlastServices([]);
    const cy = cyInstance.current;
    if (cy) cy.fit(undefined, 30);
  };

  return (
    <div className="flex h-[calc(100vh-3.5rem)] flex-col lg:flex-row overflow-hidden bg-slate-950 text-slate-100">
      {/* Left Control Panel */}
      <aside className="w-full lg:w-96 flex flex-col border-r border-slate-800/80 bg-slate-900/60 p-4 overflow-y-auto shrink-0 gap-4">
        <div>
          <div className="flex items-center gap-2 text-cyan-400 mb-1">
            <Activity className="h-4 w-4" />
            <h2 className="text-xs font-bold uppercase tracking-wider">Outage Simulation & Failover</h2>
          </div>
          <p className="text-[11px] text-slate-400 leading-relaxed">
            Demonstrates <strong>Track 01: Agents That Act</strong>. The agent detects the simulated outage, queries FalkorDB for blast radius, evaluates candidate paths under safety cost caps, and requests human authorization before re-routing.
          </p>
        </div>

        {/* Target Service Picker */}
        <div className="rounded-xl border border-slate-800 bg-slate-950/70 p-3 space-y-3">
          <label className="block text-xs font-semibold text-slate-300">
            Target Service
            <select
              value={serviceId}
              onChange={(e) => setServiceId(e.target.value)}
              disabled={loading || simulationPhase !== 'idle'}
              className="mt-1 w-full rounded-lg border border-slate-800 bg-slate-900 p-2 text-xs font-medium text-slate-200 focus:border-cyan-500 focus:outline-none disabled:opacity-50"
            >
              <option value="payments-primary">payments-primary (Primary Outage Target)</option>
              <option value="checkout">checkout</option>
              <option value="gateway">gateway</option>
              <option value="orders">orders</option>
              <option value="worker">worker</option>
            </select>
          </label>

          {/* Cost Cap Slider */}
          <div>
            <div className="flex justify-between items-center text-xs font-semibold text-slate-300 mb-1">
              <span className="flex items-center gap-1">
                <Sliders className="h-3 w-3 text-cyan-400" />
                Safety Cost Cap:
              </span>
              <span className="rounded bg-cyan-950 border border-cyan-800/60 px-1.5 py-0.5 text-cyan-300 font-mono text-[11px]">
                {maxCost} units
              </span>
            </div>
            <input
              type="range"
              min={5}
              max={25}
              step={1}
              value={maxCost}
              onChange={(e) => setMaxCost(Number(e.target.value))}
              disabled={loading || simulationPhase !== 'idle'}
              className="w-full accent-cyan-500 cursor-pointer disabled:opacity-50"
            />
            <div className="flex justify-between text-[10px] text-slate-500 mt-0.5">
              <span>Cap 5 (Strict)</span>
              <span>Cap 12 (Queue Approved)</span>
              <span>Cap 25 (Permissive)</span>
            </div>
          </div>

          <button
            onClick={runSimulationWorkflow}
            disabled={loading || simulationPhase !== 'idle'}
            className="flex w-full items-center justify-center gap-2 rounded-lg bg-rose-600 px-4 py-2.5 text-xs font-bold text-white shadow-md shadow-rose-950 transition hover:bg-rose-500 disabled:cursor-not-allowed disabled:opacity-50"
          >
            {loading ? (
              <>
                <Radio className="h-4 w-4 animate-spin text-white" />
                <span>Simulating Autonomous Response...</span>
              </>
            ) : (
              <>
                <Play className="h-4 w-4" />
                <span>Simulate Outage & Autonomous Remediation</span>
              </>
            )}
          </button>
        </div>

        {/* Visual Simulation Stage Progression */}
        <div className="rounded-xl border border-slate-800 bg-slate-950/70 p-3">
          <h3 className="text-xs font-bold uppercase tracking-wider text-slate-400 mb-2.5 flex items-center gap-1.5">
            <Radio className="h-3.5 w-3.5 text-cyan-400" />
            Agent Decision Sequence
          </h3>

          <div className="space-y-2 text-xs">
            <div className={`flex items-start gap-2.5 p-2 rounded-lg border transition-all ${
              simulationPhase === 'outage'
                ? 'bg-rose-950/40 border-rose-800 text-rose-200'
                : simulationPhase !== 'idle'
                ? 'bg-slate-900/40 border-slate-800/80 text-slate-300'
                : 'bg-slate-900/20 border-slate-800/40 text-slate-500'
            }`}>
              <div className={`h-2 w-2 rounded-full mt-1 shrink-0 ${
                simulationPhase === 'outage' ? 'bg-rose-500 animate-ping' : simulationPhase !== 'idle' ? 'bg-emerald-500' : 'bg-slate-600'
              }`} />
              <div>
                <span className="font-semibold">1. Probe Failure Detection</span>
                <p className="text-[10.5px] text-slate-400 mt-0.5">HTTP status drops; Prometheus triggers alert.</p>
              </div>
            </div>

            <div className={`flex items-start gap-2.5 p-2 rounded-lg border transition-all ${
              simulationPhase === 'blast_radius'
                ? 'bg-amber-950/40 border-amber-800 text-amber-200'
                : ['path_search', 'awaiting_approval', 'applied', 'rejected'].includes(simulationPhase)
                ? 'bg-slate-900/40 border-slate-800/80 text-slate-300'
                : 'bg-slate-900/20 border-slate-800/40 text-slate-500'
            }`}>
              <div className={`h-2 w-2 rounded-full mt-1 shrink-0 ${
                simulationPhase === 'blast_radius' ? 'bg-amber-500 animate-ping' : ['path_search', 'awaiting_approval', 'applied', 'rejected'].includes(simulationPhase) ? 'bg-emerald-500' : 'bg-slate-600'
              }`} />
              <div>
                <span className="font-semibold">2. Downstream Blast Radius</span>
                <p className="text-[10.5px] text-slate-400 mt-0.5">
                  Reverse traversal <code className="text-amber-300">[:DEPENDS_ON*1..5]</code> finds {blastServices.length > 0 ? `${blastServices.length} affected` : 'dependent'} services.
                </p>
              </div>
            </div>

            <div className={`flex items-start gap-2.5 p-2 rounded-lg border transition-all ${
              simulationPhase === 'path_search'
                ? 'bg-cyan-950/40 border-cyan-800 text-cyan-200'
                : ['awaiting_approval', 'applied', 'rejected'].includes(simulationPhase)
                ? 'bg-slate-900/40 border-slate-800/80 text-slate-300'
                : 'bg-slate-900/20 border-slate-800/40 text-slate-500'
            }`}>
              <div className={`h-2 w-2 rounded-full mt-1 shrink-0 ${
                simulationPhase === 'path_search' ? 'bg-cyan-500 animate-ping' : ['awaiting_approval', 'applied', 'rejected'].includes(simulationPhase) ? 'bg-emerald-500' : 'bg-slate-600'
              }`} />
              <div>
                <span className="font-semibold">3. Cost-Capped Route Discovery</span>
                <p className="text-[10.5px] text-slate-400 mt-0.5">
                  FalkorDB <code className="text-cyan-300">algo.SPpaths</code> evaluates routes under cap {maxCost}.
                </p>
              </div>
            </div>

            <div className={`flex items-start gap-2.5 p-2 rounded-lg border transition-all ${
              simulationPhase === 'awaiting_approval'
                ? 'bg-amber-950/40 border-amber-800 text-amber-200'
                : ['applied', 'rejected'].includes(simulationPhase)
                ? 'bg-slate-900/40 border-slate-800/80 text-slate-300'
                : 'bg-slate-900/20 border-slate-800/40 text-slate-500'
            }`}>
              <div className={`h-2 w-2 rounded-full mt-1 shrink-0 ${
                simulationPhase === 'awaiting_approval' ? 'bg-amber-400 animate-ping' : ['applied', 'rejected'].includes(simulationPhase) ? 'bg-emerald-500' : 'bg-slate-600'
              }`} />
              <div>
                <span className="font-semibold">4. Human Approval Gate</span>
                <p className="text-[10.5px] text-slate-400 mt-0.5">ApprovalRecord held in FalkorDB with 5-minute expiry.</p>
              </div>
            </div>

            <div className={`flex items-start gap-2.5 p-2 rounded-lg border transition-all ${
              simulationPhase === 'applied'
                ? 'bg-emerald-950/40 border-emerald-800 text-emerald-200'
                : 'bg-slate-900/20 border-slate-800/40 text-slate-500'
            }`}>
              <div className={`h-2 w-2 rounded-full mt-1 shrink-0 ${
                simulationPhase === 'applied' ? 'bg-emerald-400' : 'bg-slate-600'
              }`} />
              <div>
                <span className="font-semibold">5. TrafficState Persisted</span>
                <p className="text-[10.5px] text-slate-400 mt-0.5">Failover packet routing animated along active path.</p>
              </div>
            </div>
          </div>
        </div>

        {error && (
          <div className="rounded-xl border border-rose-900/60 bg-rose-950/30 p-3 text-xs text-rose-300 flex items-start gap-2">
            <AlertTriangle className="h-4 w-4 shrink-0 text-rose-400 mt-0.5" />
            <span className="break-words">{error}</span>
          </div>
        )}

        {simulationPhase !== 'idle' && (
          <button
            onClick={handleReset}
            className="flex items-center justify-center gap-1.5 rounded-lg border border-slate-800 bg-slate-900/80 py-2 text-xs font-medium text-slate-400 hover:text-slate-200 hover:bg-slate-800 transition"
          >
            <RotateCcw className="h-3.5 w-3.5" />
            <span>Reset Simulation Canvas</span>
          </button>
        )}
      </aside>

      {/* Center Graph Canvas */}
      <main className="relative flex-1 flex flex-col min-w-0 bg-slate-950">
        {/* Canvas Legend & Animation Indicator */}
        <div className="absolute left-4 top-3 z-10 flex flex-wrap gap-2 text-[11px] pointer-events-none">
          <span className="flex items-center gap-1.5 rounded-lg border border-slate-800/80 bg-slate-900/90 backdrop-blur-sm px-2.5 py-1 text-slate-300 shadow">
            <span className="h-2 w-2 rounded-full bg-emerald-500" /> Healthy Topology (44)
          </span>
          <span className="flex items-center gap-1.5 rounded-lg border border-slate-800/80 bg-slate-900/90 backdrop-blur-sm px-2.5 py-1 text-slate-300 shadow">
            <span className="h-2 w-2 rounded-full bg-rose-500" /> Degraded Target
          </span>
          <span className="flex items-center gap-1.5 rounded-lg border border-slate-800/80 bg-slate-900/90 backdrop-blur-sm px-2.5 py-1 text-slate-300 shadow">
            <span className="h-2 w-2 rounded-full bg-amber-400" /> Blast Radius
          </span>
          <span className="flex items-center gap-1.5 rounded-lg border border-slate-800/80 bg-slate-900/90 backdrop-blur-sm px-2.5 py-1 text-slate-300 shadow">
            <span className="h-2 w-2 rounded-full bg-cyan-400" /> Active Failover Route
          </span>
        </div>

        {/* Selected Node Inspector Flyout */}
        {selectedNode && (
          <div className="absolute right-4 top-3 z-10 w-64 rounded-xl border border-slate-800 bg-slate-900/95 backdrop-blur-md p-3 text-xs text-slate-200 shadow-xl">
            <div className="flex items-center justify-between border-b border-slate-800 pb-1.5 mb-2">
              <span className="font-bold text-cyan-300">{selectedNode.id}</span>
              <button
                onClick={() => setSelectedNode(null)}
                className="text-slate-500 hover:text-slate-300 text-xs"
              >
                ✕
              </button>
            </div>
            <div className="space-y-1 text-[11px] text-slate-400">
              <div><strong className="text-slate-300">Name:</strong> {selectedNode.label ?? selectedNode.id}</div>
              <div><strong className="text-slate-300">Owner Team:</strong> {selectedNode.owner_team ?? 'Unassigned'}</div>
              <div><strong className="text-slate-300">Tier:</strong> {selectedNode.tier ?? 'tier-1'}</div>
              <div><strong className="text-slate-300">Health:</strong> <span className={selectedNode.health === 'degraded' ? 'text-rose-400' : 'text-emerald-400'}>{selectedNode.health ?? 'healthy'}</span></div>
            </div>
          </div>
        )}

        <div ref={cyRef} className="h-full w-full cursor-grab active:cursor-grabbing" />
      </main>

      {/* Right Evidence & Approval Panel */}
      <aside className="w-full lg:w-96 flex flex-col border-l border-slate-800/80 bg-slate-900/60 p-4 overflow-y-auto shrink-0 gap-4">
        <div>
          <h3 className="text-xs font-bold uppercase tracking-wider text-slate-400 mb-1">
            Evidence & Governance Gate
          </h3>
          <p className="text-[11px] text-slate-400 leading-relaxed">
            Every remediation proposal is evaluated against graph constraints. Actions remain pending until human authorization.
          </p>
        </div>

        {/* Pending Approval Gate Card */}
        {activeApproval && (
          <div className="rounded-xl border border-amber-700/80 bg-amber-950/30 p-4 shadow-lg shadow-amber-950/20">
            <div className="flex items-center gap-2 text-amber-300 font-bold text-xs uppercase tracking-wide mb-2">
              <Clock className="h-4 w-4 animate-spin text-amber-400" />
              <span>Human Approval Required</span>
            </div>

            <p className="text-[11.5px] text-slate-300 mb-3 leading-relaxed">
              FalkorDB verified route within safety cost cap. Authorize traffic failover:
            </p>

            <div className="space-y-2 rounded-lg bg-slate-950/70 p-2.5 border border-amber-900/40 text-xs mb-3">
              <div>
                <span className="text-slate-400">Action: </span>
                <span className="font-bold text-amber-300">{activeApproval.proposed.action_kind}</span>
              </div>
              <div>
                <span className="text-slate-400">Recommended Route: </span>
                <div className="flex items-center gap-1.5 font-mono text-cyan-300 font-semibold text-[11px] mt-0.5">
                  {activeApproval.proposed.route?.join(' → ') ?? 'checkout → queue → payments-backup'}
                </div>
              </div>
              <div className="flex justify-between text-[11px] text-slate-400 pt-1 border-t border-slate-900">
                <span>Cost: <strong className="text-emerald-400">{activeApproval.proposed.cost ?? 8}</strong> / {maxCost} cap</span>
                <span>Latency: <strong className="text-cyan-400">{activeApproval.proposed.latency_ms ?? 5}ms</strong></span>
              </div>
            </div>

            <div className="flex gap-2">
              <button
                onClick={() => handleDecision('approve')}
                disabled={loading}
                className="flex-1 flex items-center justify-center gap-1.5 rounded-lg bg-emerald-600 px-3 py-2 text-xs font-bold text-white shadow-md shadow-emerald-950 transition hover:bg-emerald-500 disabled:opacity-50"
              >
                <Check className="h-4 w-4" />
                <span>Approve Failover</span>
              </button>
              <button
                onClick={() => handleDecision('reject')}
                disabled={loading}
                className="flex items-center justify-center gap-1.5 rounded-lg border border-slate-700 bg-slate-800 px-3 py-2 text-xs font-semibold text-slate-300 hover:bg-slate-700 transition disabled:opacity-50"
              >
                <X className="h-4 w-4 text-rose-400" />
                <span>Reject</span>
              </button>
            </div>
          </div>
        )}

        {/* Applied Action Proof Card */}
        {actionOutcome && (
          <div className="rounded-xl border border-emerald-800/80 bg-emerald-950/20 p-4">
            <div className="flex items-center gap-2 text-emerald-400 font-bold text-xs uppercase tracking-wide mb-2">
              <CheckCircle2 className="h-4 w-4 text-emerald-400" />
              <span>Simulation State Applied</span>
            </div>
            <p className="text-[11.5px] text-slate-300 mb-2.5">
              TrafficState atomically persisted in FalkorDB. Live traffic packet animation active along route:
            </p>
            <div className="rounded-lg bg-slate-950/80 p-2.5 border border-slate-800 space-y-1.5 text-xs font-mono">
              <div className="text-cyan-300 font-semibold">
                {actionOutcome.active_route?.join(' → ') ?? 'checkout → queue → payments-backup'}
              </div>
              <div className="text-[10px] text-slate-400">
                Incident: <span className="text-slate-300">{actionOutcome.incident_id}</span>
              </div>
              <div className="text-[10px] text-slate-400">
                Status: <span className="text-emerald-400 font-bold uppercase">{actionOutcome.status}</span>
              </div>
            </div>
          </div>
        )}

        {/* Path Comparison Breakdown */}
        <div className="rounded-xl border border-slate-800 bg-slate-950/60 p-3 space-y-2.5">
          <h4 className="text-xs font-bold uppercase tracking-wider text-slate-400">
            Evaluated Candidate Routes
          </h4>
          <div className="space-y-2 text-xs">
            {/* Rejected route */}
            <div className="rounded-lg border border-rose-900/50 bg-rose-950/20 p-2.5">
              <div className="flex items-center justify-between text-rose-400 font-semibold mb-1">
                <span>Route A (Premium Direct)</span>
                <span className="text-[10px] uppercase font-bold rounded bg-rose-950 border border-rose-800/60 px-1 py-0.2">
                  Rejected
                </span>
              </div>
              <p className="font-mono text-[10.5px] text-slate-300">checkout → premium → backup</p>
              <div className="flex justify-between text-[10px] text-slate-400 mt-1">
                <span>Cost: <strong className="text-rose-400">20</strong> (Exceeds cap {maxCost})</span>
                <span>Latency: 2ms</span>
              </div>
            </div>

            {/* Approved route */}
            <div className="rounded-lg border border-emerald-900/50 bg-emerald-950/20 p-2.5">
              <div className="flex items-center justify-between text-emerald-400 font-semibold mb-1">
                <span>Route B (Buffered Queue)</span>
                <span className="text-[10px] uppercase font-bold rounded bg-emerald-950 border border-emerald-800/60 px-1 py-0.2">
                  Selected
                </span>
              </div>
              <p className="font-mono text-[10.5px] text-slate-300">checkout → queue → backup</p>
              <div className="flex justify-between text-[10px] text-slate-400 mt-1">
                <span>Cost: <strong className="text-emerald-400">8</strong> (Within cap {maxCost})</span>
                <span>Latency: 5ms</span>
              </div>
            </div>
          </div>
        </div>
      </aside>
    </div>
  );
};
