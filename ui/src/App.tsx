import React, { useEffect, useState } from 'react';
import { Navigation, ActiveTab } from './components/Navigation';
import { TraceDrawer } from './components/TraceDrawer';
import { SimulationPage } from './pages/SimulationPage';
import { MemoryPage } from './pages/MemoryPage';
import { CompanyBrainPage } from './pages/CompanyBrainPage';
import { IdentityState, TraceLog } from './types';

export default function App() {
  const [activeTab, setActiveTab] = useState<ActiveTab>('simulation');
  const [identity, setIdentity] = useState<IdentityState | null>(null);
  const [traces, setTraces] = useState<TraceLog[]>([]);

  // Synchronize hash with active tab
  useEffect(() => {
    const handleHashChange = () => {
      const hash = window.location.hash.replace('#', '') as ActiveTab;
      if (['simulation', 'memory', 'company-brain'].includes(hash)) {
        setActiveTab(hash);
      }
    };

    if (window.location.hash) {
      handleHashChange();
    } else {
      window.location.hash = 'simulation';
    }

    window.addEventListener('hashchange', handleHashChange);
    return () => window.removeEventListener('hashchange', handleHashChange);
  }, []);

  // Fetch server identity
  useEffect(() => {
    fetch('/api/identity')
      .then((res) => (res.ok ? res.json() : null))
      .then((data) => {
        if (data) setIdentity(data);
      })
      .catch(() => {});
  }, []);

  const handleAddTrace = (trace: TraceLog) => {
    setTraces((prev) => [trace, ...prev]);
  };

  const handleClearTraces = () => {
    setTraces([]);
  };

  return (
    <div className="flex h-screen w-screen flex-col overflow-hidden bg-slate-950 font-sans text-slate-100">
      {/* Top Application Bar */}
      <Navigation
        activeTab={activeTab}
        onTabChange={setActiveTab}
        identity={identity}
      />

      {/* 3 Dedicated Demo Surfaces */}
      <div className="flex-1 min-h-0 overflow-hidden relative">
        {activeTab === 'simulation' && (
          <SimulationPage onAddTrace={handleAddTrace} />
        )}
        {activeTab === 'memory' && (
          <MemoryPage onAddTrace={handleAddTrace} />
        )}
        {activeTab === 'company-brain' && (
          <CompanyBrainPage onAddTrace={handleAddTrace} />
        )}
      </div>

      {/* Global Glass-Box Server Trace Drawer */}
      <TraceDrawer traces={traces} onClear={handleClearTraces} />
    </div>
  );
}
