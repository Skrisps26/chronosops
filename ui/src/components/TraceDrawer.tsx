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
    <footer className="border-t border-zinc-800 bg-black">
      <div className="flex items-center justify-between px-4 py-2 bg-zinc-950 border-b border-zinc-800/80">
        <button
          onClick={() => setIsOpen(!isOpen)}
          className="flex items-center gap-2 text-xs font-semibold uppercase tracking-wider text-zinc-300 hover:text-white transition-colors"
        >
          <Terminal className="h-3.5 w-3.5 text-zinc-400" />
          <span>FalkorDB Evidence Trace ({traces.length})</span>
          {isOpen ? <ChevronDown className="h-3.5 w-3.5 text-zinc-500" /> : <ChevronUp className="h-3.5 w-3.5 text-zinc-500" />}
        </button>

        <div className="flex items-center gap-3 text-[11px] text-zinc-500">
          <span>Grounded Cypher queries</span>
          {traces.length > 0 && onClear && (
            <button
              onClick={onClear}
              className="text-[10px] text-zinc-500 hover:text-zinc-300 hover:underline"
            >
              Clear
            </button>
          )}
        </div>
      </div>

      {isOpen && (
        <div className="h-44 space-y-2 overflow-y-auto p-3 font-mono text-[11px] bg-black">
          {traces.length === 0 ? (
            <div className="italic text-slate-500 py-4 text-center">
              No server-backed Cypher queries or execution events captured yet. Run an action, question, or simulation to see real-time execution traces.
            </div>
          ) : (
            traces.map((log, index) => (
              <div
                key={`${log.tool}-${index}`}
                className="rounded-md border border-zinc-800/80 bg-zinc-950 p-2.5"
              >
                <div className="mb-1.5 flex flex-wrap items-center justify-between gap-2">
                  <div className="flex items-center gap-2">
                    <span className="rounded bg-zinc-900 border border-zinc-800 px-1.5 py-0.5 text-zinc-200 font-bold text-[10px]">
                      {log.tool}
                    </span>
                    <span className="text-zinc-500 text-[10px]">
                      Graph: <strong className="text-zinc-400">{log.graph}</strong>
                    </span>
                  </div>

                  {log.duration_ms !== undefined && (
                    <div className="flex items-center gap-1 text-zinc-500 text-[10px]">
                      <Clock className="h-3 w-3 text-zinc-500" />
                      <span className="font-semibold text-zinc-300">{log.duration_ms.toFixed(1)}ms</span>
                    </div>
                  )}
                </div>

                {log.cypher && (
                  <div className="flex items-start gap-1.5 text-zinc-300 bg-black rounded p-2 border border-zinc-800/80 mt-1">
                    <Code2 className="h-3.5 w-3.5 text-zinc-500 shrink-0 mt-0.5" />
                    <pre className="break-all whitespace-pre-wrap font-mono text-[10.5px] leading-relaxed text-zinc-300 m-0">
                      {log.cypher}
                    </pre>
                  </div>
                )}

                {log.params && Object.keys(log.params).length > 0 && (
                  <div className="mt-1 text-[10px] text-zinc-500">
                    <span>Parameters: </span>
                    <code className="text-zinc-400 bg-black px-1 py-0.5 rounded border border-zinc-900">
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
