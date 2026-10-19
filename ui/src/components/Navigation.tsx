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
    <header className="border-b border-zinc-800 bg-black sticky top-0 z-50">
      <div className="flex flex-col sm:flex-row items-center justify-between px-5 py-2.5 gap-3">
        {/* Brand identity */}
        <div className="flex items-center gap-3">
          <div className="flex h-8 w-8 items-center justify-center rounded-md bg-zinc-900 border border-zinc-800 text-zinc-100">
            <Server className="h-4 w-4" />
          </div>
          <div>
            <div className="flex items-center gap-2">
              <span className="text-sm font-bold tracking-tight text-white">ChronosOps</span>
              <span className="rounded bg-zinc-900 border border-zinc-800 px-1.5 py-0.5 text-[10px] font-medium text-zinc-400">
                FalkorDB
              </span>
            </div>
            <p className="text-[11px] text-zinc-500 leading-none mt-0.5">
              Incident Intelligence Platform
            </p>
          </div>
        </div>

        {/* 3-Page Navigation Tabs */}
        <nav className="flex items-center gap-1 rounded-lg bg-zinc-950 border border-zinc-800 p-1">
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
                className={`flex items-center gap-2 rounded-md px-3.5 py-1.5 text-xs font-medium transition-colors ${
                  isActive
                    ? 'bg-white text-black font-semibold shadow-sm'
                    : 'text-zinc-400 hover:text-zinc-100 hover:bg-zinc-900'
                }`}
              >
                <Icon className={`h-3.5 w-3.5 ${isActive ? 'text-black' : 'text-zinc-400'}`} />
                <span>{tab.label}</span>
              </button>
            );
          })}
        </nav>

        {/* System & Tenant Status Badges */}
        <div className="flex items-center gap-2 text-[11px]">
          <div className="flex items-center gap-1.5 rounded-md border border-zinc-800 bg-zinc-950 px-2.5 py-1 text-zinc-300">
            <span className="h-1.5 w-1.5 rounded-full bg-emerald-500" />
            <span className="text-zinc-500">Tenant:</span>
            <span className="font-semibold text-zinc-200 uppercase tracking-wide">
              {identity?.tenant_id ?? 'acme'}
            </span>
          </div>

          <div className="flex items-center gap-1.5 rounded-md border border-zinc-800 bg-zinc-950 px-2 py-1 text-zinc-400">
            <Shield className="h-3 w-3 text-zinc-500" />
            <span className="text-[10px] font-medium">Safe Mode</span>
          </div>
        </div>
      </div>
    </header>
  );
};
