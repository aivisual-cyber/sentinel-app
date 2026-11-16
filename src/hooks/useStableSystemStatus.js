import { useState, useCallback, useEffect, useRef } from 'react';
import { useTauriCommand, useTauriEvent } from './useTauriApi';
import { useDelayedLoading } from './useDelayedLoading';

/**
 * Enhanced system status hook with anti-flashing and stale-while-revalidate pattern
 * Designed for industrial fire safety systems where stable UI is critical
 */
export const useStableSystemStatus = () => {
  const [currentStatus, setCurrentStatus] = useState({
    cameras: 0,
    totalCameras: 0,
    activeAlerts: 0,
    uptime: 0,
    // Safety system: never assume protection is active until the backend proves it
    systemHealth: 'unknown',
    detectionEngine: 'unknown',
    cameraNetwork: 'unknown',
    alertSystem: 'unknown',
    backendRunning: false,
    pythonPid: null
  });

  const [isRefreshing, setIsRefreshing] = useState(false);
  const [lastSuccessfulData, setLastSuccessfulData] = useState(null);
  const [lastUpdateTime, setLastUpdateTime] = useState(Date.now());
  
  const intervalRef = useRef(null);
  const errorCountRef = useRef(0);
  const lastSuccessRef = useRef(Date.now());
  const debounceRef = useRef(null);
  const healthStabilityRef = useRef({ consecutive: 0, lastHealth: 'optimal' });
  
  // Use delayed loading to prevent flashing
  const { shouldShowLoading, isStale } = useDelayedLoading(isRefreshing, {
    delay: 200,        // Don't show loading for fast responses
    minDuration: 600,  // Keep loading visible long enough to be meaningful
    enableStaleData: true
  });
  
  // Tauri commands
  const { execute: getSystemStatus } = useTauriCommand('get_system_status');
  const { execute: getCameraFeeds } = useTauriCommand('get_camera_feeds');
  const { execute: getDashboardData } = useTauriCommand('get_dashboard_data');

  // Stable health change function - prevents oscillation
  const getStableHealth = useCallback((proposedHealth, currentHealth) => {
    const stability = healthStabilityRef.current;
    
    if (proposedHealth === currentHealth) {
      // No change - reset stability counter
      stability.consecutive = 0;
      stability.lastHealth = proposedHealth;
      return proposedHealth;
    }
    
    if (proposedHealth === stability.lastHealth) {
      // Same as last proposed change - increment counter
      stability.consecutive += 1;
    } else {
      // Different proposed change - reset counter
      stability.consecutive = 1;
      stability.lastHealth = proposedHealth;
    }
    
    // Only allow health changes after 3 consecutive readings
    // Exception: Always allow optimal → warning/critical for safety
    // Degradations (anything -> critical/warning) and the first real reading apply immediately
    if (stability.consecutive >= 3 || proposedHealth === 'critical' || currentHealth === 'unknown' ||
        (currentHealth === 'optimal' && proposedHealth !== 'optimal')) {
      return proposedHealth;
    }
    
    // Not enough consecutive readings - keep current health
    return currentHealth;
  }, []);

  // Debounced status update function - prevents rapid state changes
  const updateStatusDebounced = useCallback((updates) => {
    if (debounceRef.current) {
      clearTimeout(debounceRef.current);
    }
    
    debounceRef.current = setTimeout(() => {
      setCurrentStatus(current => {
        const newStatus = { ...current, ...updates };
        
        // Apply stable health logic to prevent oscillation
        if (updates.systemHealth && updates.systemHealth !== current.systemHealth) {
          newStatus.systemHealth = getStableHealth(updates.systemHealth, current.systemHealth);
        }
        
        // Only update if there's a meaningful change
        const hasSignificantChange = 
          current.systemHealth !== newStatus.systemHealth ||
          current.detectionEngine !== newStatus.detectionEngine ||
          current.backendRunning !== newStatus.backendRunning ||
          current.alertSystem !== newStatus.alertSystem ||
          Math.abs(current.cameras - newStatus.cameras) > 0 ||
          Math.abs(current.activeAlerts - newStatus.activeAlerts) > 0;
        
        if (hasSignificantChange) {
          setLastUpdateTime(Date.now());
          return newStatus;
        }
        
        return current;
      });
    }, 150); // Slightly longer debounce for industrial stability
  }, [getStableHealth]);

  // Listen for real-time updates
  useTauriEvent('real-time-update', useCallback((event) => {
    const data = event.payload;
    if (data && data.alerts) {
      // Update active alerts count
      const activeAlerts = data.alerts.recent_alerts 
        ? data.alerts.recent_alerts.filter(alert => alert.status === 'active').length
        : 0;

      // A dashboard payload proves the detection backend is up
      updateStatusDebounced({
        activeAlerts,
        backendRunning: true,
        detectionEngine: 'online',
        alertSystem: 'ready'
      });

      // Reset error tracking on successful real-time update
      errorCountRef.current = 0;
      lastSuccessRef.current = Date.now();
    }
  }, [updateStatusDebounced]));

  // Helper function to map backend health status to frontend format
  const mapHealthStatus = (backendHealth) => {
    switch (backendHealth) {
      case 'healthy':
      case 'optimal': return 'optimal';
      case 'warning': return 'warning';
      case 'error':
      case 'critical': return 'critical';
      default: return 'unknown';
    }
  };

  // Enhanced refresh function with better error handling
  const refreshSystemStatus = useCallback(async (isManualRefresh = false) => {
    // Don't set loading state for background refreshes unless it's been a while
    const timeSinceLastUpdate = Date.now() - lastUpdateTime;
    const shouldShowRefreshingState = isManualRefresh || timeSinceLastUpdate > 30000;
    
    if (shouldShowRefreshingState) {
      setIsRefreshing(true);
    }

    try {
      // Parallel requests for better performance, with more graceful error handling
      const [systemData, cameraData, dashboardData] = await Promise.all([
        getSystemStatus().catch(err => {
          console.log('System status not available yet (normal during startup):', err.message);
          return null;
        }),
        getCameraFeeds().catch(err => {
          console.log('Camera feeds not available yet (normal without backend):', err.message);
          return null;
        }),
        getDashboardData().catch(err => {
          console.log('Dashboard data not available yet (normal without backend):', err.message);
          return null;
        })
      ]);

      // Build updates object
      const updates = {};
      let hasValidData = false;

      if (systemData) {
        updates.backendRunning = !!systemData.backend_running;
        updates.pythonPid = systemData.python_pid;
        updates.uptime = systemData.uptime || 0;
        // Health comes from the backend's real /api/health probe (via Rust get_system_status)
        updates.detectionEngine = systemData.backend_running ? 'online' : 'offline';
        updates.alertSystem = systemData.backend_running ? 'ready' : 'offline';
        updates.systemHealth = systemData.backend_running ? 'optimal' : 'critical';
        hasValidData = true;
      }

      // Camera information
      if (cameraData) {
        updates.cameras = cameraData.active_cameras || 0;
        updates.cameraNetwork = cameraData.active_cameras > 0 ? 'online' : 'offline';
        hasValidData = true;
      }

      // Alert information from dashboard
      if (dashboardData && dashboardData.alerts) {
        updates.activeAlerts = dashboardData.alerts.recent_alerts 
          ? dashboardData.alerts.recent_alerts.filter(alert => alert.status === 'active').length
          : 0;
        if (cameraData && cameraData.total_cameras > 0 && cameraData.active_cameras < cameraData.total_cameras) {
          updates.systemHealth = updates.systemHealth === 'critical' ? 'critical' : 'warning';
        }
        hasValidData = true;
      }

      // Overall system health assessment based on API response success
      // If we got ANY valid data, the backend is responding and functional
      if (hasValidData) {
        // Backend is responding, so core systems are online
        if (!updates.systemHealth) {
          updates.systemHealth = 'unknown';
        }
      }
      // Note: We don't set critical here anymore - only sustained API failures can do that

      if (hasValidData) {
        // Store successful data for stale-while-revalidate
        setLastSuccessfulData({ ...currentStatus, ...updates });
        
        // Apply updates through the stable update function
        updateStatusDebounced(updates);

        // Reset error tracking
        errorCountRef.current = 0;
        lastSuccessRef.current = Date.now();
        setLastUpdateTime(Date.now());
      } else {
        // All requests failed - but this might be normal if backend isn't running yet
        // Don't immediately mark as error, especially during startup
        const timeSinceLastSuccess = Date.now() - lastSuccessRef.current;
        
        if (timeSinceLastSuccess > 15000) { // No backend for 15 s: detection is NOT protecting anything
          console.warn('No backend responses for 15+ seconds - marking detection offline');
          errorCountRef.current += 1;
          updateStatusDebounced({ systemHealth: 'critical', detectionEngine: 'offline',
                                  alertSystem: 'offline', backendRunning: false });
        } else {
          console.log('Backend not responding yet, but this is normal during startup');
        }
      }

    } catch (error) {
      console.error('System status refresh failed:', error);
      errorCountRef.current += 1;
      
      const timeSinceLastSuccess = Date.now() - lastSuccessRef.current;
      
      // Very conservative approach to critical status
      // Only mark as critical after sustained, severe failures
      if (errorCountRef.current >= 3 && timeSinceLastSuccess > 15000) { // detection offline
        updateStatusDebounced({
          systemHealth: 'critical',
          detectionEngine: 'offline',
          cameraNetwork: 'offline', 
          alertSystem: 'offline'
        });
      } else if (errorCountRef.current >= 5 && timeSinceLastSuccess > 120000) { // 2+ minutes of failures
        updateStatusDebounced({
          systemHealth: 'warning'
        });
      }
      // For fewer errors or recent failures, keep current state (stale-while-revalidate)
      // For first 2 errors or recent failures, keep current state (stale-while-revalidate)
    } finally {
      setIsRefreshing(false);
    }
  }, [getSystemStatus, getCameraFeeds, getDashboardData, currentStatus, lastUpdateTime]);

  // Always call the latest refresh function without re-running the polling effect.
  // (Depending on refreshSystemStatus/currentStatus re-fired the effect on every state
  //  change -> immediate refetch -> state change: an unbounded request loop once real data arrived.)
  const refreshRef = useRef(refreshSystemStatus);
  refreshRef.current = refreshSystemStatus;
  const healthRef = useRef(currentStatus.systemHealth);
  healthRef.current = currentStatus.systemHealth;

  // Periodic status refresh with smart intervals (mount once)
  useEffect(() => {
    // Initial status check - but give the app time to load first
    const initialDelay = setTimeout(() => {
      refreshRef.current(true);
    }, 1000); // Wait 1 second before first check

    // Adaptive refresh interval based on system health
    const getRefreshInterval = () => {
      switch (healthRef.current) {
        case 'critical': return 5000;   // Check every 5s when critical
        case 'warning': return 8000;    // Check every 8s when warning
        case 'optimal': return 20000;   // Check every 20s when optimal (longer for efficiency)
        default: return 15000;          // Default 15s
      }
    };

    const scheduleNextRefresh = () => {
      if (intervalRef.current) {
        clearTimeout(intervalRef.current);
      }
      
      intervalRef.current = setTimeout(() => {
        refreshRef.current(false); // Background refresh
        scheduleNextRefresh();
      }, getRefreshInterval());
    };

    // Start periodic refresh after initial delay
    const periodicStart = setTimeout(() => {
      scheduleNextRefresh();
    }, 2000);
    
    return () => {
      clearTimeout(initialDelay);
      clearTimeout(periodicStart);
      if (intervalRef.current) {
        clearTimeout(intervalRef.current);
      }
      if (debounceRef.current) {
        clearTimeout(debounceRef.current);
      }
    };
  }, []);

  // Manual status update function (for compatibility)
  const updateStatus = useCallback((updates) => {
    updateStatusDebounced(updates);
  }, [updateStatusDebounced]);

  return {
    systemStatus: currentStatus,
    updateStatus,
    refreshSystemStatus: () => refreshSystemStatus(true),
    // Loading state info
    isRefreshing: shouldShowLoading,
    isStale,
    lastUpdateTime,
    // Debug info
    _debug: {
      errorCount: errorCountRef.current,
      timeSinceLastSuccess: Date.now() - lastSuccessRef.current,
      hasStaleData: !!lastSuccessfulData
    }
  };
};