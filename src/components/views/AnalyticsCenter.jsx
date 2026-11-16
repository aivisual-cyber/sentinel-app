import React, { useState, useEffect, useCallback } from 'react';
import { useTauriCommand } from '../../hooks/useTauriApi';

const PerformanceMetric = ({ label, value, color }) => (
  <div>
    <div className="flex justify-between text-sm mb-1">
      <span className="text-gray-400">{label}</span>
      <span className={`text-${color}-400 font-command`}>{value}</span>
    </div>
    <div className="w-full bg-command-700 rounded-full h-2">
      <div 
        className={`bg-${color}-500 h-2 rounded-full`}
        style={{ width: typeof value === 'string' ? '85%' : `${value}%` }}
      ></div>
    </div>
  </div>
);

const SystemMetric = ({ label, value, color }) => (
  <div className="flex justify-between items-center">
    <span className="text-gray-400">{label}</span>
    <span className={`text-${color}-400 font-command`}>{value}</span>
  </div>
);

const StatusItem = ({ label, status, color }) => (
  <div className="flex items-center justify-between">
    <span className="text-sm text-gray-300">{label}</span>
    <div className="flex items-center space-x-2">
      <div className={`w-2 h-2 bg-${color}-500 rounded-full`}></div>
      <span className={`text-xs font-command text-${color}-400`}>{status}</span>
    </div>
  </div>
);

const MetricItem = ({ label, value, status }) => {
  const colors = {
    excellent: 'emerald',
    good: 'safety',
    warning: 'warning',
    critical: 'emergency'
  };
  const color = colors[status] || 'gray';
  
  return (
    <div className="flex items-center justify-between">
      <span className="text-sm text-gray-300">{label}</span>
      <span className={`text-xs font-command text-${color}-400`}>{value}</span>
    </div>
  );
};

const AnalyticsCenter = ({ connectionStatus }) => {
  const [analyticsData, setAnalyticsData] = useState({
    performanceMetrics: [
      { label: 'Accuracy Rate', value: 'Loading...', color: 'gray' },
      { label: 'Response Time', value: 'Loading...', color: 'gray' },
      { label: 'False Positive Rate', value: 'Loading...', color: 'gray' }
    ],
    systemMetrics: [
      { label: 'CPU Usage', value: 'Loading...', color: 'gray' },
      { label: 'Memory Usage', value: 'Loading...', color: 'gray' },
      { label: 'Network Latency', value: 'Loading...', color: 'gray' },
      { label: 'Frames Processed', value: 'Loading...', color: 'gray' }
    ]
  });

  const [alertStats, setAlertStats] = useState({
    totalAlerts: 0,
    falsePositives: 0,
    averageResponseTime: 0,
    uptime: 0
  });

  const [systemHealthData, setSystemHealthData] = useState({
    operationalStatus: [
      { label: 'Detection Engine', status: 'CHECKING...', color: 'gray' },
      { label: 'Camera Network', status: 'CHECKING...', color: 'gray' },
      { label: 'Alert System', status: 'CHECKING...', color: 'gray' },
      { label: 'Database', status: 'CHECKING...', color: 'gray' }
    ],
    performanceMetrics: [
      { label: 'CPU Usage', value: 'Loading...', status: 'unknown' },
      { label: 'Memory', value: 'Loading...', status: 'unknown' },
      { label: 'Storage', value: 'Loading...', status: 'unknown' },
      { label: 'Backend memory', value: 'Loading...', status: 'unknown' }
    ]
  });

  // Tauri commands
  const { execute: getDashboardData } = useTauriCommand('get_dashboard_data');
  const { execute: getSystemStatus } = useTauriCommand('get_system_status');
  const { execute: getSystemMetrics } = useTauriCommand('get_system_metrics');

  const fetchAnalyticsData = useCallback(async () => {
    try {
      const [dashboardData, systemStatus, systemMetrics] = await Promise.all([
        getDashboardData(),
        getSystemStatus(),
        getSystemMetrics()
      ]);

      // Real backend fields: statistics = {P1:{count,avg_confidence}, P2:…, P4:…, total}
      const stats = dashboardData?.alerts?.statistics || {};
      const totalAlerts = stats.total || 0;
      const count = (lvl) => stats[lvl]?.count || 0;
      const avg = (lvl) => (stats[lvl]?.avg_confidence ? `${Math.round(stats[lvl].avg_confidence * 100)}% avg` : '');
      const cpuUsage = systemMetrics?.cpu?.usage_percent || 0;
      const memoryUsage = (systemMetrics?.memory?.used_gb || 0).toFixed(1);
      const det = systemMetrics?.detection_performance || {};
      const totalFrames = det.frames_processed || 0;

      // Accuracy / false-positive rate need operator-verified ground truth, which is not collected yet:
      // show measured quantities only (never fabricated percentages).
      const newPerformanceMetrics = [
        { label: 'P1 alerts (24 h)', value: `${count('P1')} ${avg('P1')}`.trim(), color: count('P1') ? 'red' : 'emerald' },
        { label: 'P2 alerts (24 h)', value: `${count('P2')} ${avg('P2')}`.trim(), color: count('P2') ? 'yellow' : 'emerald' },
        { label: 'P4 events (24 h)', value: `${count('P4')} ${avg('P4')}`.trim(), color: 'gray' }
      ];

      const newSystemMetrics = [
        { 
          label: 'Frames Processed', 
          value: totalFrames.toLocaleString(), 
          color: 'white' 
        },
        {
          label: 'Confirmed detections',
          value: (det.detections_made || 0).toLocaleString(),
          color: 'white'
        },
        { 
          label: 'GPU Usage', 
          value: systemMetrics?.gpu?.usage_percent ? `${Math.round(systemMetrics.gpu.usage_percent)}%` : 'N/A', 
          color: systemMetrics?.gpu?.usage_percent ? (systemMetrics.gpu.usage_percent < 70 ? 'emerald' : systemMetrics.gpu.usage_percent < 85 ? 'yellow' : 'red') : 'gray' 
        },
        { 
          label: 'GPU Memory', 
          value: systemMetrics?.gpu?.memory_used_mb ? `${Math.round(systemMetrics.gpu.memory_used_mb)}MB` : 'N/A', 
          color: systemMetrics?.gpu?.memory_used_mb ? 'safety' : 'gray' 
        },
        { 
          label: 'Process Memory', 
          value: systemMetrics?.memory?.process_memory_mb ? `${Math.round(systemMetrics.memory.process_memory_mb)}MB` : 'N/A', 
          color: 'white' 
        }
      ];

      setAnalyticsData({
        performanceMetrics: newPerformanceMetrics,
        systemMetrics: newSystemMetrics
      });

      setAlertStats({
        totalAlerts,
        falsePositives: 0,
        averageResponseTime: 0,
        uptime: systemMetrics?.system?.uptime_hours || 0
      });

      // Update operational status from System Health
      let newOperationalStatus = [
        { 
          label: 'Detection Engine', 
          status: systemStatus?.backend_running ? 'ONLINE' : 'OFFLINE', 
          color: systemStatus?.backend_running ? 'emerald' : 'red' 
        },
        { 
          label: 'Camera Network', 
          status: `${dashboardData?.alerts?.system_status?.total_cameras ?? 0} CONFIGURED`,
          color: (dashboardData?.alerts?.system_status?.total_cameras ?? 0) > 0 ? 'emerald' : 'yellow' 
        },
        { 
          label: 'Alert System', 
          status: dashboardData?.alerts ? 'ONLINE' : 'OFFLINE', 
          color: dashboardData?.alerts ? 'emerald' : 'red' 
        },
        { 
          label: 'Database', 
          status: systemStatus?.backend_running ? 'ONLINE' : 'OFFLINE', 
          color: systemStatus?.backend_running ? 'emerald' : 'red' 
        }
      ];

      // Use real system metrics for performance metrics
      const storageUsage = Math.round(systemMetrics?.disk?.percent || 0);
      
      let systemPerformanceMetrics = [
        { 
          label: 'CPU Usage', 
          value: `${Math.round(cpuUsage)}%`, 
          status: cpuUsage < 70 ? 'good' : cpuUsage < 85 ? 'warning' : 'critical' 
        },
        { 
          label: 'Memory', 
          value: `${memoryUsage}GB`, 
          status: parseFloat(memoryUsage) < 4 ? 'good' : parseFloat(memoryUsage) < 6 ? 'warning' : 'critical' 
        },
        { 
          label: 'Storage', 
          value: `${storageUsage}%`, 
          status: storageUsage < 70 ? 'good' : storageUsage < 85 ? 'warning' : 'critical' 
        },
        {
          // backend latency figure is an estimate from packet counts, not a measurement - not shown
          label: 'Backend memory',
          value: `${Math.round(systemMetrics?.memory?.process_memory_mb || 0)} MB`,
          status: 'good'
        }
      ];

      setSystemHealthData({
        operationalStatus: newOperationalStatus,
        performanceMetrics: systemPerformanceMetrics
      });

    } catch (error) {
      console.error('Failed to fetch analytics data:', error);
      
      // Set error state with user feedback
      setAnalyticsData(prev => ({
        performanceMetrics: prev.performanceMetrics.map(item => ({
          ...item,
          value: connectionStatus?.isConnected === false ? 'Offline' : 'Error',
          color: 'red'
        })),
        systemMetrics: prev.systemMetrics.map(item => ({
          ...item,
          value: connectionStatus?.isConnected === false ? 'Offline' : 'Error',
          color: 'red'
        }))
      }));
      
      setSystemHealthData(prev => ({
        ...prev,
        operationalStatus: prev.operationalStatus.map(item => ({
          ...item,
          status: 'ERROR',
          color: 'red'
        }))
      }));
    }
  }, [getDashboardData, getSystemStatus, getSystemMetrics]);

  // Update data on mount and periodically
  useEffect(() => {
    fetchAnalyticsData();
    
    // Update every 45 seconds (offset from SystemHealth)
    const interval = setInterval(fetchAnalyticsData, 45000);
    
    return () => clearInterval(interval);
  }, [fetchAnalyticsData]);

  return (
    <div className="p-6">
      <div className="mb-6">
        <h2 className="text-2xl font-bold text-white mb-2">ANALYTICS CENTER</h2>
        <p className="text-gray-400">Performance metrics and insights</p>
        {connectionStatus && !connectionStatus.isConnected && (
          <div className="bg-emergency-500/20 border border-emergency-500 rounded-lg p-3 mt-4">
            <div className="flex items-center space-x-2">
              <div className="w-2 h-2 bg-emergency-500 rounded-full animate-pulse"></div>
              <span className="text-emergency-400 text-sm font-semibold">LIMITED DATA AVAILABLE</span>
            </div>
            <p className="text-emergency-300 text-xs mt-1">
              Analytics unavailable due to backend connection issues
            </p>
          </div>
        )}
      </div>
      
      <div className="grid grid-cols-2 gap-6">
        <div className="command-panel rounded-xl p-6">
          <h3 className="text-lg font-semibold text-white mb-4">DETECTION PERFORMANCE</h3>
          <div className="space-y-4">
            {analyticsData.performanceMetrics.map((metric, index) => (
              <PerformanceMetric key={index} {...metric} />
            ))}
          </div>
        </div>
        
        <div className="command-panel rounded-xl p-6">
          <h3 className="text-lg font-semibold text-white mb-4">DETECTION METRICS</h3>
          <div className="space-y-4">
            {analyticsData.systemMetrics.map((metric, index) => (
              <SystemMetric key={index} {...metric} />
            ))}
          </div>
        </div>
      </div>
      
      <div className="grid grid-cols-3 gap-6 mt-6">
        <div className="command-panel rounded-xl p-6">
          <h3 className="text-lg font-semibold text-emerald-400 mb-4">OPERATIONAL STATUS</h3>
          <div className="space-y-3">
            {systemHealthData.operationalStatus.map((item, index) => (
              <StatusItem key={index} {...item} />
            ))}
          </div>
        </div>
        
        <div className="command-panel rounded-xl p-6">
          <h3 className="text-lg font-semibold text-safety-400 mb-4">SYSTEM PERFORMANCE</h3>
          <div className="space-y-3">
            {systemHealthData.performanceMetrics.map((item, index) => (
              <MetricItem key={index} {...item} />
            ))}
          </div>
        </div>
        
        <div className="command-panel rounded-xl p-6">
          <h3 className="text-lg font-semibold text-warning-400 mb-4">QUICK STATS</h3>
          <div className="space-y-3">
            <div className="flex justify-between items-center">
              <span className="text-gray-300">Total Alerts</span>
              <span className="text-xs font-command text-white">{alertStats.totalAlerts}</span>
            </div>
            <div className="flex justify-between items-center">
              <span className="text-gray-300">Backend uptime</span>
              <span className="text-xs font-command text-emerald-400">{alertStats.uptime.toFixed(1)} h</span>
            </div>
            <div className="flex justify-between items-center">
              <span className="text-gray-300">Accuracy / false-positive rate</span>
              <span className="text-xs font-command text-gray-400">needs operator verification data</span>
            </div>
          </div>
        </div>
      </div>
    </div>
  );
};

export default React.memo(AnalyticsCenter);