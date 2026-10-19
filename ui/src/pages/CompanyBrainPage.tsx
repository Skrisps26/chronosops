import React, { useEffect, useRef, useState, useCallback } from 'react';
import cytoscape from 'cytoscape';
import {
  Database,
  Users,
  AlertTriangle,
  FileText,
  Search,
  ExternalLink,
  ShieldAlert,
  Sparkles,
  ChevronRight,
  BookOpen,
} from 'lucide-react';
import { CytoscapeElement, CitationItem, TraceLog } from '../types';

interface CompanyBrainPageProps {
  onAddTrace: (trace: TraceLog) => void;
}

export const CompanyBrainPage: React.FC<CompanyBrainPageProps> = ({ onAddTrace }) => {
  const [elements, setElements] = useState<CytoscapeElement[]>([]);
  const [serviceId, setServiceId] = useState<string>('payments-primary');
  const [question, setQuestion] = useState<string>('Who owns payments-primary?');
  const [loading, setLoading] = useState<boolean>(false);
  const [answer, setAnswer] = useState<string | null>(null);
  const [citations, setCitations] = useState<CitationItem[]>([]);
  const [selectedCitation, setSelectedCitation] = useState<CitationItem | null>(null);
  const [error, setError] = useState<string | null>(null);

  const cyRef = useRef<HTMLDivElement>(null);
  const cyInstance = useRef<cytoscape.Core | null>(null);

  const delay = (ms: number): Promise<void> => {
    const { promise, resolve } = Promise.withResolvers<void>();
    setTimeout(resolve, ms);
    return promise;
  };

  // Fetch Company Brain graph elements
  const fetchGraph = useCallback(async () => {
    try {
      const res = await fetch('/api/company-brain/graph');
      if (!res.ok) throw new Error(`Company Brain graph endpoint returned HTTP ${res.status}`);
      const data = await res.json();
      setElements(data.elements || []);
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Could not fetch Company Brain graph');
    }
  }, []);

  useEffect(() => {
    fetchGraph();
  }, [fetchGraph]);

  // Cytoscape initialization
  useEffect(() => {
    if (!cyRef.current || elements.length === 0) return;

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
            width: 22,
            height: 22,
            'transition-property': 'background-color, border-color, border-width, width, height, opacity',
            'transition-duration': 300,
          },
        },
        {
          selector: 'node[type = "service"]',
          style: {
            'background-color': '#10b981',
            'border-color': '#a7f3d0',
            'border-width': 2,
            width: 24,
            height: 24,
          },
        },
        {
          selector: 'node[type = "team"]',
          style: {
            'background-color': '#06b6d4',
            'border-color': '#67e8f9',
            'border-width': 2.5,
            shape: 'round-rectangle',
            width: 38,
            height: 26,
          },
        },
        {
          selector: 'node[type = "person"]',
          style: {
            'background-color': '#a855f7',
            'border-color': '#d8b4fe',
            'border-width': 2,
            width: 24,
            height: 24,
          },
        },
        {
          selector: 'node[type = "document"]',
          style: {
            'background-color': '#64748b',
            'border-color': '#cbd5e1',
            'border-width': 1.5,
            shape: 'rectangle',
            width: 26,
            height: 20,
          },
        },
        {
          selector: 'node.beacon-service',
          style: {
            'background-color': '#06b6d4',
            'border-color': '#ffffff',
            'border-width': 4,
            width: 36,
            height: 36,
          },
        },
        {
          selector: 'node.beacon-team',
          style: {
            'background-color': '#06b6d4',
            'border-color': '#38bdf8',
            'border-width': 4,
            width: 46,
            height: 32,
          },
        },
        {
          selector: 'node.beacon-person',
          style: {
            'background-color': '#c084fc',
            'border-color': '#f3e8ff',
            'border-width': 3,
            width: 30,
            height: 30,
          },
        },
        {
          selector: 'node.beacon-doc',
          style: {
            'background-color': '#f59e0b',
            'border-color': '#fef3c7',
            'border-width': 3,
            width: 32,
            height: 24,
          },
        },
        {
          selector: 'edge[type = "OWNED_BY"]',
          style: {
            width: 2,
            'line-color': '#06b6d4',
            'target-arrow-color': '#06b6d4',
            'target-arrow-shape': 'triangle',
            'curve-style': 'bezier',
          },
        },
        {
          selector: 'edge[type = "MEMBER_OF"]',
          style: {
            width: 1.5,
            'line-color': '#a855f7',
            'target-arrow-color': '#a855f7',
            'target-arrow-shape': 'triangle',
            'curve-style': 'bezier',
          },
        },
        {
          selector: 'edge[type = "DOC_REF"]',
          style: {
            width: 1.5,
            'line-color': '#475569',
            'target-arrow-color': '#475569',
            'target-arrow-shape': 'vee',
            'line-style': 'dashed',
            'curve-style': 'bezier',
          },
        },
        {
          selector: 'edge.beacon-edge',
          style: {
            width: 4,
            'line-color': '#38bdf8',
            'target-arrow-color': '#38bdf8',
          },
        },
      ],
      layout: {
        name: 'cose',
        animate: false,
        randomize: false,
        componentSpacing: 120,
        nodeRepulsion: () => 2500000,
        nodeOverlap: 50,
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

  // Ownership Traversal Beacon Animation
  const animateOwnershipTraversal = async (targetService: string) => {
    const cy = cyInstance.current;
    if (!cy) return;

    // Clear previous beacon classes
    cy.elements().removeClass([
      'beacon-service',
      'beacon-team',
      'beacon-person',
      'beacon-doc',
      'beacon-edge',
    ]);

    // 1. Service Beacon Pulse
    const svcNode = cy.$(`node[id = "${targetService}"]`);
    if (svcNode.length > 0) {
      svcNode.addClass('beacon-service');
      svcNode.animate(
        { style: { width: 40, height: 40 } },
        { duration: 300, easing: 'ease-out' }
      );
    }

    await delay(400);

    // 2. Traversal along OWNED_BY to Team
    const ownedEdge = cy.$(`edge[type = "OWNED_BY"][source = "${targetService}"]`);
    ownedEdge.addClass('beacon-edge');
    const teamNode = cy.$(`node[id = "team-Payments"]`);
    teamNode.addClass('beacon-team');

    await delay(400);

    // 3. Traversal to team members (Ivan, Bob, Alice)
    const memberEdges = cy.$('edge[type = "MEMBER_OF"][target = "team-Payments"]');
    memberEdges.addClass('beacon-edge');
    const personNodes = cy.$('node[type = "person"][team = "Payments"]');
    personNodes.addClass('beacon-person');

    await delay(400);

    // 4. Highlight citation source document
    const docNode = cy.$('node[id = "doc-services.csv"]');
    docNode.addClass('beacon-doc');
  };

  const handleQuery = async () => {
    if (!question.trim()) return;
    setLoading(true);
    setError(null);
    setSelectedCitation(null);

    try {
      const res = await fetch('/api/questions', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ question, service_id: serviceId }),
      });

      if (!res.ok) throw new Error(`Query failed: HTTP ${res.status}`);
      const data = await res.json();

      setAnswer(data.answer ?? 'No answer generated');
      const citeList = data.citations || data.sources || [];
      setCitations(citeList);

      if (data.tool_result?.trace) {
        onAddTrace(data.tool_result.trace);
      }

      // Trigger animated traversal
      await animateOwnershipTraversal(serviceId);
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Error consulting Company Brain');
    } finally {
      setLoading(false);
    }
  };

  return (
    <div className="flex h-[calc(100vh-3.5rem)] flex-col lg:flex-row overflow-hidden bg-black text-zinc-100">
      {/* Left Inquiry & Answer Panel */}
      <aside className="w-full lg:w-96 flex flex-col border-r border-zinc-800 bg-black p-4 overflow-y-auto shrink-0 gap-4">
        <div>
          <div className="flex items-center gap-2 text-zinc-200 mb-1">
            <Database className="h-4 w-4 text-zinc-400" />
            <h2 className="text-xs font-bold uppercase tracking-wider">Company Brain Knowledge Graph</h2>
          </div>
          <p className="text-[11px] text-zinc-400 leading-relaxed">
            Track 03: Company Brain. Connects canonical microservices, teams, engineers, and architecture RFCs. Answers are grounded in signed graph properties and published source chunks.
          </p>
        </div>

        {/* Query Input Box */}
        <div className="rounded-lg border border-zinc-800 bg-zinc-950 p-3 space-y-3">
          <label className="block text-xs font-medium text-zinc-300">
            Canonical Target Service
            <select
              value={serviceId}
              onChange={(e) => {
                setServiceId(e.target.value);
                setQuestion(`Who owns ${e.target.value}?`);
              }}
              className="mt-1 w-full rounded-md border border-zinc-800 bg-black p-2 text-xs text-zinc-200 focus:border-zinc-500 focus:outline-none"
            >
              <option value="payments-primary">payments-primary (Owned by Payments)</option>
              <option value="checkout">checkout (Owned by Checkout)</option>
              <option value="gateway">gateway (Owned by Infrastructure)</option>
              <option value="orders">orders (Owned by Orders)</option>
            </select>
          </label>

          <label className="block text-xs font-medium text-zinc-300">
            Question
            <textarea
              value={question}
              onChange={(e) => setQuestion(e.target.value)}
              rows={2}
              className="mt-1 w-full rounded-md border border-zinc-800 bg-black p-2 text-xs text-zinc-200 focus:border-zinc-500 focus:outline-none resize-none"
              placeholder="Ask about team ownership, bus factor, or architecture..."
            />
          </label>

          {/* Preset Question Chips */}
          <div className="flex flex-wrap gap-1.5 pt-1">
            <button
              onClick={() => setQuestion(`Who owns ${serviceId}?`)}
              className="rounded-md border border-zinc-800 bg-zinc-900 px-2 py-1 text-[10px] font-medium text-zinc-300 hover:text-white hover:bg-zinc-850 transition"
            >
              Who owns this service?
            </button>
            <button
              onClick={() => setQuestion(`What is the bus factor for ${serviceId}?`)}
              className="rounded-md border border-zinc-800 bg-zinc-900 px-2 py-1 text-[10px] font-medium text-zinc-300 hover:text-white hover:bg-zinc-850 transition"
            >
              Bus factor?
            </button>
            <button
              onClick={() => setQuestion(`Why is ${serviceId} unavailable for shifting?`)}
              className="rounded-md border border-zinc-800 bg-zinc-900 px-2 py-1 text-[10px] font-medium text-zinc-300 hover:text-white hover:bg-zinc-850 transition"
            >
              Why backup queue?
            </button>
          </div>

          <button
            onClick={handleQuery}
            disabled={loading || !question.trim()}
            className="flex w-full items-center justify-center gap-2 rounded-md bg-white px-4 py-2 text-xs font-bold text-black shadow transition hover:bg-zinc-200 disabled:cursor-not-allowed disabled:opacity-40"
          >
            {loading ? (
              <>
                <Search className="h-4 w-4 animate-spin text-black" />
                <span>Traversing Company Brain Graph...</span>
              </>
            ) : (
              <>
                <Sparkles className="h-4 w-4" />
                <span>Query Company Brain</span>
              </>
            )}
          </button>
        </div>

        {/* Grounded Answer Card */}
        {answer && (
          <div className="rounded-lg border border-zinc-800 bg-zinc-950 p-3.5 space-y-2.5">
            <div className="flex items-center gap-2 text-zinc-200 font-bold text-xs uppercase tracking-wide">
              <Users className="h-4 w-4 text-zinc-400" />
              <span>Grounded Knowledge Answer</span>
            </div>
            <p className="text-xs text-zinc-200 leading-relaxed bg-black p-2.5 rounded-md border border-zinc-800">
              {answer}
            </p>

            {/* Citations List */}
            {citations.length > 0 && (
              <div className="pt-2 border-t border-zinc-900 space-y-1.5">
                <span className="text-[10px] font-semibold text-zinc-500 uppercase tracking-wider block">
                  Grounding Source Citations ({citations.length}):
                </span>
                {citations.map((c, i) => (
                  <button
                    key={`${c.document_id}-${i}`}
                    onClick={() => setSelectedCitation(c)}
                    className="flex w-full items-center justify-between p-2 rounded-md border border-zinc-800 bg-zinc-900/60 hover:bg-zinc-900 text-left transition"
                  >
                    <div className="flex items-center gap-2 min-w-0">
                      <FileText className="h-3.5 w-3.5 text-zinc-400 shrink-0" />
                      <div className="truncate">
                        <span className="font-mono text-[11px] text-zinc-200 font-semibold">{c.document_id}</span>
                        {c.chunk_id && <span className="text-[10px] text-zinc-500 ml-1.5">#{c.chunk_id.slice(0, 10)}</span>}
                      </div>
                    </div>
                    <ChevronRight className="h-3.5 w-3.5 text-zinc-600 shrink-0" />
                  </button>
                ))}
              </div>
            )}
          </div>
        )}

        {/* Selected Citation Full Text Drawer */}
        {selectedCitation && (
          <div className="rounded-lg border border-zinc-800 bg-zinc-950 p-3 space-y-2 text-xs">
            <div className="flex items-center justify-between border-b border-zinc-900 pb-1.5">
              <span className="font-bold text-zinc-200 flex items-center gap-1.5">
                <BookOpen className="h-3.5 w-3.5 text-zinc-400" />
                {selectedCitation.document_id}
              </span>
              <button
                onClick={() => setSelectedCitation(null)}
                className="text-zinc-500 hover:text-zinc-300 text-xs"
              >
                ✕
              </button>
            </div>
            <div className="font-mono text-[10.5px] text-zinc-300 leading-relaxed bg-black p-2 rounded border border-zinc-900">
              {selectedCitation.excerpt || selectedCitation.text || 'No chunk excerpt'}
            </div>
            {selectedCitation.content_hash && (
              <div className="text-[9.5px] text-zinc-500 truncate">
                SHA-256: <code className="text-zinc-400">{selectedCitation.content_hash}</code>
              </div>
            )}
          </div>
        )}

        {error && (
          <div className="rounded-lg border border-red-900/60 bg-zinc-950 p-3 text-xs text-red-300">
            {error}
          </div>
        )}
      </aside>

      {/* Center Graph Canvas */}
      <main className="relative flex-1 flex flex-col min-w-0 bg-black">
        {/* Canvas Legend */}
        <div className="absolute left-4 top-3 z-10 flex flex-wrap gap-2 text-[11px] pointer-events-none">
          <span className="flex items-center gap-1.5 rounded-md border border-zinc-800 bg-zinc-950/90 px-2.5 py-1 text-zinc-300">
            <span className="h-2 w-2 rounded-full bg-emerald-500" /> Service Node
          </span>
          <span className="flex items-center gap-1.5 rounded-md border border-zinc-800 bg-zinc-950/90 px-2.5 py-1 text-zinc-300">
            <span className="h-2 w-2 rounded-full bg-sky-400" /> Team Node (OWNED_BY)
          </span>
          <span className="flex items-center gap-1.5 rounded-md border border-zinc-800 bg-zinc-950/90 px-2.5 py-1 text-zinc-300">
            <span className="h-2 w-2 rounded-full bg-purple-500" /> Engineer (MEMBER_OF)
          </span>
          <span className="flex items-center gap-1.5 rounded-md border border-zinc-800 bg-zinc-950/90 px-2.5 py-1 text-zinc-300">
            <span className="h-2 w-2 rounded-full bg-zinc-500" /> Document Node
          </span>
        </div>

        <div ref={cyRef} className="h-full w-full cursor-grab active:cursor-grabbing bg-black" />
      </main>
    </div>
  );
};
