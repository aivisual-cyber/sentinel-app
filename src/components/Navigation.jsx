import React from 'react';
import { Command, Grid3x3, AlertTriangle, BarChart3, Settings } from 'lucide-react';

const NavigationButton = ({ isActive, view, icon: Icon, label, onClick }) => (
  <button
    onClick={() => onClick(view)}
    className={`w-full flex items-center space-x-3 px-3 py-2 rounded-lg text-sm font-medium transition-all duration-200 ${
      isActive
        ? 'bg-gray-800 text-gray-50 shadow-lg'
        : 'text-gray-300 hover:text-gray-100 hover:bg-gray-800/50'
    }`}
  >
    <Icon className="w-4 h-4" />
    <span>{label}</span>
  </button>
);

const StatusItem = ({ label, status, value }) => {
  const statusColors = {
    online: 'success',
    active: 'success', 
    ready: 'success',
    optimal: 'success',
    offline: 'error',
    unknown: 'gray',
    starting: 'accent',
    warning: 'warning',
    critical: 'error'
  };
  
  const color = statusColors[status?.toLowerCase()] || 'gray'; // unknown is never shown as healthy
  
  return (
    <div className="flex justify-between items-center">
      <span className="text-sm text-gray-300">{label}</span>
      <div className="flex items-center space-x-2">
        <div className={`w-2 h-2 bg-${color}-500 rounded-full`}></div>
        <span className={`text-xs font-mono text-${color}-400`}>{value}</span>
      </div>
    </div>
  );
};

const Navigation = ({ currentView, onViewChange, systemStatus }) => {
  const operationsItems = [
    { view: 'command', label: 'Command Center', icon: Command },
    { view: 'surveillance', label: 'Surveillance Grid', icon: Grid3x3 },
    { view: 'incidents', label: 'Incident Management', icon: AlertTriangle },
    { view: 'analytics', label: 'Analytics Center', icon: BarChart3 }
  ];

  const configItems = [
    { view: 'settings', label: 'Detection Settings', icon: Settings }
  ];

  return (
    <nav className="w-64 glass border-r border-gray-800 p-4 space-y-2">
      <div className="mb-6">
        <h2 className="text-xs font-semibold text-gray-300 uppercase tracking-wider mb-3">
          OPERATIONS
        </h2>
        <div className="space-y-1">
          {operationsItems.map((item) => (
            <NavigationButton
              key={item.view}
              isActive={currentView === item.view}
              view={item.view}
              icon={item.icon}
              label={item.label}
              onClick={onViewChange}
            />
          ))}
        </div>
      </div>

      <div className="mb-6">
        <h2 className="text-xs font-semibold text-gray-300 uppercase tracking-wider mb-3">
          CONFIGURATION
        </h2>
        <div className="space-y-1">
          {configItems.map((item) => (
            <NavigationButton
              key={item.view}
              isActive={currentView === item.view}
              view={item.view}
              icon={item.icon}
              label={item.label}
              onClick={onViewChange}
            />
          ))}
        </div>
      </div>

      {/* System Status Panel */}
      <div className="command-panel rounded-lg mt-8">
        <h3 className="text-xs font-semibold text-gray-300 uppercase tracking-wider mb-3">
          SYSTEM STATUS
        </h3>
        <div className="space-y-3">
          <StatusItem 
            label="Detection Engine" 
            status={systemStatus?.detectionEngine || 'unknown'}
            value={(systemStatus?.detectionEngine || 'unknown').toUpperCase().replace('ONLINE', 'ACTIVE')}
          />
          <StatusItem 
            label="Camera Grid" 
            status={systemStatus?.backendRunning ? (systemStatus?.cameraNetwork || 'unknown') : 'offline'}
            value={systemStatus?.backendRunning ? `${systemStatus?.cameras || 0} STREAMING` : 'OFFLINE'}
          />
          <StatusItem
            label="Inference"
            status={systemStatus?.inference ? (String(systemStatus.inference.device).startsWith('GPU') ? 'online' : 'warning') : 'unknown'}
            value={systemStatus?.inference
              ? (String(systemStatus.inference.device).startsWith('GPU')
                ? `GPU · ${(String(systemStatus.inference.device).match(/via (\w+)/) || [])[1] || ''}` : 'CPU')
              : 'UNKNOWN'}
          />
          <StatusItem 
            label="Alert System" 
            status={systemStatus?.alertSystem || 'unknown'}
            value={(systemStatus?.alertSystem || 'unknown').toUpperCase()}
          />
        </div>
      </div>
    </nav>
  );
};

export default React.memo(Navigation);