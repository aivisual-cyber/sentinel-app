import React, { useMemo } from 'react';
import { motion } from 'framer-motion';
import MetricCard from '../shared/MetricCard';
import ThresholdPanel from '../shared/ThresholdPanel';
import ActivityFeed from '../shared/ActivityFeed';

const CommandCenter = ({ alerts, systemStatus, isVisible, connectionStatus }) => {
  // Memoized calculations for performance
  const activeAlertsCount = useMemo(() => 
    alerts.filter(alert => alert.status === 'active').length,
    [alerts]
  );

  const detectionOnline = !!systemStatus?.backendRunning;

  const threatLevel = useMemo(() => {
    if (!detectionOnline) return { level: 'UNKNOWN', color: 'emergency', description: 'Detection offline - NOT monitoring' };
    if (activeAlertsCount === 0) return { level: 'LOW', color: 'safety', description: 'No active threats detected' };
    if (activeAlertsCount < 3) return { level: 'MEDIUM', color: 'warning', description: 'Monitoring active alerts' };
    return { level: 'HIGH', color: 'emergency', description: 'Multiple threats detected' };
  }, [activeAlertsCount, detectionOnline]);

  const metrics = [
    {
      title: 'THREAT LEVEL',
      value: threatLevel.level,
      color: threatLevel.color,
      description: threatLevel.description,
      indicator: true
    },
    {
      title: 'ACTIVE CAMERAS',
      value: systemStatus.cameras,
      color: 'fire',
      description: detectionOnline ? `${systemStatus.cameras} streaming` : 'Detection offline',
      indicator: true
    },
    {
      title: 'ACTIVE ALERTS',
      value: activeAlertsCount,
      color: 'warning',
      description: 'Unacknowledged, last 24 h',
      indicator: true
    },
    {
      title: 'DETECTION',
      value: detectionOnline ? 'ONLINE' : 'OFFLINE',
      color: detectionOnline ? 'safety' : 'emergency',
      description: detectionOnline ? 'Backend health check passing' : 'NOT monitoring - start the backend',
      indicator: true
    }
  ];

  const containerVariants = {
    hidden: { opacity: 0 },
    visible: {
      opacity: 1,
      transition: { staggerChildren: 0.1 }
    }
  };

  const itemVariants = {
    hidden: { opacity: 0, y: 20 },
    visible: { opacity: 1, y: 0 }
  };

  return (
    <motion.div 
      initial="hidden"
      animate="visible"
      variants={containerVariants}
      className="p-8 space-y-8 overflow-y-auto h-full"
    >
      {/* Page Header */}
      <motion.div variants={itemVariants} className="mb-8">
        <h1 className="text-3xl font-bold gradient-text mb-2">Fire Command Center</h1>
        <p className="text-gray-400">Real-time fire detection monitoring and system control</p>
      </motion.div>

      {/* Critical Metrics Row */}
      <motion.div variants={itemVariants} className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-4 gap-6">
        {metrics.map((metric, index) => (
          <MetricCard key={index} {...metric} />
        ))}
      </motion.div>

      {/* Main Content Grid */}
      <div className="grid grid-cols-1 lg:grid-cols-2 gap-8">
        {/* Detection Thresholds Panel */}
        <motion.div variants={itemVariants}>
          <ThresholdPanel />
        </motion.div>

        {/* Live Activity Feed */}
        <motion.div variants={itemVariants}>
          <ActivityFeed 
            alerts={alerts}
            isVisible={isVisible}
            maxItems={8}
            connectionStatus={connectionStatus}
          />
        </motion.div>
      </div>
    </motion.div>
  );
};

export default React.memo(CommandCenter);