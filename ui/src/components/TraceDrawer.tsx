import React, { useState } from 'react';
import { Terminal, ChevronUp, ChevronDown, Clock, Code2 } from 'lucide-react';
import { TraceLog } from '../types';

interface TraceDrawerProps {
  traces: TraceLog[];
  onClear?: () => void;
}

export const TraceDrawer: React.FC<TraceDrawerProps> = ({ traces, onClear }) => {
  const [isOpen, setIsOpen] = useState(false);

  return (
    <footer className="border-t border-slate-800/80 bg-slate-950/95 transition-all">
      <div className="flex items-center justify-between px-4 py-2 bg-slate-900/60 border-b border-slate-800/40">
        <button
          onClick={() => setIsOpen(!isOpen)}
          className="flex items-center gap-2 text-xs font-semibold uppercase tracking-wider text-slate-300 hover:text-cyan-400 transition-colors"
        >
          <Terminal className="h-3.5 w-3.5 text-cyan-400" />
          <span>FalkorDB Glass-Box Evidence Trace ({traces.length} entries)</span>
          {isOpen ? <ChevronDown className="h-3.5 w-3.5 text-slate-400" /> : <ChevronUp className="h-3.5 w-3.5 text-slate-400" />}
        </button>

        <div className="flex items-center gap-3 text-[11px] text-slate-400">
          <span>All decisions grounded in real Cypher queries</span>
          {traces.length > 0 && onClear && (
            <button
              onClick={onClear}
              className="text-[10px] text-slate-500 hover:text-slate-300 hover:underline"
            >
              Clear
            </button>
          )}
        </div>
      </div>

      {isOpen && (
        <div className="h-44 space-y-2 overflow-y-auto p-3 font-mono text-[11px] bg-slate-950">
          {traces.length === 0 ? (
            <div className="italic text-slate-500 py-4 text-center">
              No server-backed Cypher queries or execution events captured yet. Run an action, question, or simulation to see real-time execution traces.
            </div>
          ) : (
            traces.map((log, index) => (
              <div
                key={`${log.tool}-${index}`}
                className="rounded-lg border border-slate-800/80 bg-slate-900/70 p-2.5 transition-colors hover:border-slate-700"
              >
                <div className="mb-1.5 flex flex-wrap items-center justify-between gap-2">
                  <div className="flex items-center gap-2">
                    <span className="rounded bg-cyan-950/80 border border-cyan-800/60 px-1.5 py-0.5 text-cyan-300 font-bold text-[10px]">
                      Tool: {log.tool}
                    </span>
                    <span className="text-slate-400 text-[10px]">
                      Graph: <strong className="text-slate-200">{log.graph}</strong>
                    </span>
                  </div>

                  {log.duration_ms !== undefined && (
                    <div className="flex items-center gap-1 text-slate-400 text-[10px]">
                      <Clock className="h-3 w-3 text-cyan-400" />
                      <span className="font-semibold text-slate-200">{log.duration_ms.toFixed(1)}ms</span>
                    </div>
                  )}
                </div>

                {log.cypher && (
                  <div className="flex items-start gap-1.5 text-slate-300 bg-slate-950/60 rounded p-1.5 border border-slate-800/50 mt-1">
                    <Code2 className="h-3.5 w-3.5 text-cyan-500 shrink-0 mt-0.5" />
                    <pre className="break-all whitespace-pre-wrap font-mono text-[10.5px] leading-relaxed text-cyan-200/90 m-0">
                      {log.cypher}
                    </pre>
                  </div>
                )}

                {log.params && Object.keys(log.params).length > 0 && (
                  <div className="mt-1 text-[10px] text-slate-400">
                    <span className="text-slate-500">Parameters: </span>
                    <code className="text-slate-300 bg-slate-950/40 px-1 py-0.5 rounded">
                      {JSON.stringify(log.params)}
                    </code>
                  </div>
                )}
              </div>
            ))
          )}
        </div>
      )}
    </footer>
  );
};
