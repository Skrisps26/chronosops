import React from 'react';
import { Activity, Brain, Database, Shield, Server } from 'lucide-react';
import { IdentityState } from '../types';

export type ActiveTab = 'simulation' | 'memory' | 'company-brain';

interface NavigationProps {
  activeTab: ActiveTab;
  onTabChange: (tab: ActiveTab) => void;
  identity: IdentityState | null;
}

export const Navigation: React.FC<NavigationProps> = ({
  activeTab,
  onTabChange,
  identity,
}) => {
  const tabs: { id: ActiveTab; label: string; icon: React.FC<{ className?: string }>; track: string }[] = [
    {
      id: 'simulation',
      label: 'Incident Simulation',
      icon: Activity,
      track: 'Track 01: Agents That Act',
    },
    {
      id: 'memory',
      label: 'Agent Memory',
      icon: Brain,
      track: 'Track 02: Memory & Playbooks',
    },
    {
      id: 'company-brain',
      label: 'Company Brain',
      icon: Database,
      track: 'Track 03: Knowledge Graph',
    },
  ];

  return (
    <header className="border-b border-slate-800/80 bg-slate-950/90 backdrop-blur-md sticky top-0 z-50">
      <div className="flex flex-col sm:flex-row items-center justify-between px-4 py-2.5 gap-3">
        {/* Brand identity */}
        <div className="flex items-center gap-3">
          <div className="flex h-9 w-9 items-center justify-center rounded-lg bg-cyan-950 border border-cyan-800/80 text-cyan-400 shadow-sm shadow-cyan-950">
            <Server className="h-5 w-5" />
          </div>
          <div>
            <div className="flex items-center gap-2">
              <span className="text-base font-bold tracking-tight text-slate-100">ChronosOps</span>
              <span className="rounded bg-cyan-950/80 border border-cyan-800/50 px-1.5 py-0.5 text-[10px] font-medium text-cyan-300">
                FalkorDB Powered
              </span>
            </div>
            <p className="text-[11px] text-slate-400 leading-none mt-0.5">
              Autonomous, Graph-Grounded Incident Response
            </p>
          </div>
        </div>

        {/* 3-Page Navigation Tabs */}
        <nav className="flex items-center gap-1 rounded-xl bg-slate-900/90 border border-slate-800/80 p-1 shadow-inner">
          {tabs.map((tab) => {
            const Icon = tab.icon;
            const isActive = activeTab === tab.id;
            return (
              <button
                key={tab.id}
                onClick={() => {
                  window.location.hash = tab.id;
                  onTabChange(tab.id);
                }}
                className={`flex items-center gap-2 rounded-lg px-3 py-1.5 text-xs font-semibold transition-all ${
                  isActive
                    ? 'bg-cyan-600 text-white shadow-md shadow-cyan-950 border border-cyan-500/50'
                    : 'text-slate-400 hover:text-slate-200 hover:bg-slate-800/60'
                }`}
              >
                <Icon className={`h-4 w-4 ${isActive ? 'text-white' : 'text-slate-400'}`} />
                <span>{tab.label}</span>
                <span className={`hidden md:inline-block rounded px-1.5 py-0.2 text-[9px] font-normal ${
                  isActive ? 'bg-cyan-700/80 text-cyan-100' : 'bg-slate-800 text-slate-500'
                }`}>
                  {tab.id === 'simulation' ? 'T1' : tab.id === 'memory' ? 'T2' : 'T3'}
                </span>
              </button>
            );
          })}
        </nav>

        {/* System & Tenant Status Badges */}
        <div className="flex items-center gap-2 text-[11px]">
          <div className="flex items-center gap-1.5 rounded-md border border-slate-800 bg-slate-900/80 px-2.5 py-1 text-slate-300">
            <span className="h-2 w-2 rounded-full bg-emerald-400 animate-pulse" />
            <span className="text-slate-400">Tenant:</span>
            <span className="font-semibold text-slate-200 uppercase tracking-wide">
              {identity?.tenant_id ?? 'acme'}
            </span>
          </div>

          <div className="flex items-center gap-1.5 rounded-md border border-amber-900/40 bg-amber-950/20 px-2 py-1 text-amber-300">
            <Shield className="h-3 w-3 text-amber-400" />
            <span className="text-[10px] font-medium">Simulation Mode</span>
          </div>
        </div>
      </div>
    </header>
  );
};
