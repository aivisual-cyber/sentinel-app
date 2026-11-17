import { useState, useCallback, useEffect, useRef } from 'react';
import { useTauriCommand, useTauriApi } from './useTauriApi';

const PREVIEW_FPS = 12;          // live view; detection runs separately at its own rate (2 FPS)
const POLL_MIN_MS = 1000 / PREVIEW_FPS;
const STATUS_REFRESH_MS = 3000;  // while any camera is still connecting
const IDLE_REFRESH_MS = 10000;

export const useCameraManager = () => {
  const [cameras, setCameras] = useState([]);
  const [frames, setFrames] = useState({});
  const [isLoading, setIsLoading] = useState(false);
  const [error, setError] = useState(null);
  const frameUpdateRef = useRef(null);
  const { api } = useTauriApi();
  const streamUrlFor = api && api.cameraStreamUrl;  // browser mode: MJPEG stream, no polling
  
  // Tauri commands
  const { execute: getCameraFeeds } = useTauriCommand('get_camera_feeds');
  const { execute: getCameraFrame } = useTauriCommand('get_camera_frame');
  const { execute: discoverCameras } = useTauriCommand('discover_cameras');
  const { execute: addCamera } = useTauriCommand('add_camera');
  const { execute: testCamera } = useTauriCommand('test_camera');
  const { execute: removeCamera } = useTauriCommand('remove_camera');

  // Fetch camera list and status
  const refreshCameras = useCallback(async ({ silent = false } = {}) => {
    try {
      if (!silent) setIsLoading(true);
      setError(null);
      const data = await getCameraFeeds();
      
      if (data && data.cameras) {
        const formattedCameras = data.cameras.map(camera => ({
          id: camera.id,
          name: camera.name || `Camera ${camera.id}`,
          location: camera.location || 'Unknown',
          status: camera.status === 'active' ? 'online' : 
                  camera.status === 'inactive' ? 'offline' : 'warning',
          lastFrameTime: camera.last_frame_time || 0,
          fps: camera.fps || 0,
          resolution: camera.resolution || '1920x1080'
        }));
        
        setCameras(formattedCameras);
      }
    } catch (error) {
      console.error('Failed to refresh cameras:', error);
      setError(`Camera system error: ${error.message || 'Unable to connect to camera backend'}`);
      setCameras([]); // No fallback cameras - show real error state
    } finally {
      setIsLoading(false);
    }
  }, [getCameraFeeds]);

  // Get current frame for a specific camera
  const getCameraCurrentFrame = useCallback(async (cameraId) => {
    try {
      const frameData = await getCameraFrame({ camera_id: cameraId });
      
      if (frameData && frameData.frame) {
        const frameUrl = `data:image/jpeg;base64,${frameData.frame}`;
        
        setFrames(current => ({
          ...current,
          [cameraId]: {
            url: frameUrl,
            timestamp: frameData.timestamp,
            cameraId: frameData.camera_id
          }
        }));
        
        return frameUrl;
      }
    } catch (error) {
      console.error(`Failed to get frame for camera ${cameraId}:`, error);
      return null;
    }
  }, [getCameraFrame]);

  // Live preview for all online cameras.
  // Browser mode: one MJPEG stream per camera (smooth ~12 FPS, no JSON/base64 polling).
  // Tauri shell: sequential polling as fast as frames arrive (never overlapping requests).
  const startFrameUpdates = useCallback(() => {
    if (frameUpdateRef.current) {
      frameUpdateRef.current.stop();
      frameUpdateRef.current = null;
    }
    const onlineCameras = cameras.filter(camera => camera.status === 'online');
    if (streamUrlFor) {
      setFrames(current => {
        const next = {};
        for (const camera of onlineCameras) {
          next[camera.id] = current[camera.id]?.stream
            ? current[camera.id]
            : { url: streamUrlFor(camera.id, { fps: PREVIEW_FPS }), stream: true, cameraId: camera.id };
        }
        return next;
      });
      frameUpdateRef.current = { stop: () => {} };
      return;
    }
    let active = true;
    const loop = async () => {
      while (active) {
        const started = Date.now();
        for (const camera of onlineCameras) {
          if (!active) return;
          await getCameraCurrentFrame(camera.id);
        }
        const wait = Math.max(10, POLL_MIN_MS - (Date.now() - started));
        await new Promise(resolve => setTimeout(resolve, wait));
      }
    };
    if (onlineCameras.length) loop();
    frameUpdateRef.current = { stop: () => { active = false; } };
  }, [cameras, getCameraCurrentFrame, streamUrlFor]);

  // Stop frame updates
  const stopFrameUpdates = useCallback(() => {
    if (frameUpdateRef.current) {
      frameUpdateRef.current.stop();
      frameUpdateRef.current = null;
    }
  }, []);

  // Discover new cameras on the network
  const discoverNewCameras = useCallback(async (timeout = 5) => {
    try {
      setIsLoading(true);
      const data = await discoverCameras({ timeout });
      
      if (data && data.cameras) {
        return data.cameras.map(camera => ({
          ip: camera.ip,
          type: camera.type,
          rtspUrl: camera.rtsp_url,
          discoveredTime: camera.discovered_time
        }));
      }
      
      return [];
    } catch (error) {
      console.error('Failed to discover cameras:', error);
      return [];
    } finally {
      setIsLoading(false);
    }
  }, [discoverCameras]);

  // Add a new camera
  const addNewCamera = useCallback(async (cameraConfig) => {
    try {
      const { cameraId, rtspUrl, username, password, name, location, fps } = cameraConfig;

      const result = await addCamera({
        camera_id: cameraId,
        rtsp_url: rtspUrl,
        username,
        password,
        name: name || null,
        location: location || null,
        fps: Number.isFinite(fps) ? fps : 15
      });
      
      if (result && result.success) {
        // Refresh camera list to include the new camera
        await refreshCameras();
        return { success: true, message: result.message };
      } else {
        return { success: false, message: (result && result.error) || 'Failed to add camera (see backend log)' };
      }
    } catch (error) {
      console.error('Failed to add camera:', error);
      return { success: false, message: error.toString() };
    }
  }, [addCamera, refreshCameras]);

  // Test camera connection
  const testCameraConnection = useCallback(async (cameraConfig) => {
    try {
      const { cameraId, rtspUrl, username, password } = cameraConfig;
      
      console.log('Testing camera connection:', { cameraId, rtspUrl });
      
      const result = await testCamera({
        camera_id: cameraId || 'test',
        rtsp_url: rtspUrl,
        username,
        password
      });
      
      console.log('Camera test result:', result);
      
      return {
        success: result?.success || false,
        message: result?.message || 'Unknown error'
      };
    } catch (error) {
      console.error('Failed to test camera:', error);
      return { success: false, message: error.toString() };
    }
  }, [testCamera]);

  // Remove a camera
  const removeCameraById = useCallback(async (cameraId) => {
    try {
      const result = await removeCamera({ camera_id: cameraId });
      
      if (result && result.success) {
        // Refresh camera list to remove the camera
        await refreshCameras();
        return { success: true, message: result.message };
      } else {
        return { success: false, message: 'Failed to remove camera' };
      }
    } catch (error) {
      console.error('Failed to remove camera:', error);
      return { success: false, message: error.toString() };
    }
  }, [removeCamera, refreshCameras]);

  // Initialize cameras on mount, then keep status fresh: quickly while a camera is still
  // connecting (so its live view appears as soon as the stream is up), slowly otherwise.
  useEffect(() => {
    refreshCameras();
  }, [refreshCameras]);

  const anyConnecting = cameras.some(c => c.status !== 'online');
  useEffect(() => {
    const id = setInterval(() => refreshCameras({ silent: true }), anyConnecting ? STATUS_REFRESH_MS : IDLE_REFRESH_MS);
    return () => clearInterval(id);
  }, [refreshCameras, anyConnecting]);

  // Start frame updates when cameras are available
  useEffect(() => {
    if (cameras.length > 0) {
      startFrameUpdates();
    } else {
      // Stop updates when no cameras are available
      stopFrameUpdates();
    }
    
    // Cleanup on unmount or when effect re-runs
    return () => {
      stopFrameUpdates();
    };
  }, [cameras, startFrameUpdates, stopFrameUpdates]);

  // Get stats for summary display
  const getCameraStats = useCallback(() => {
    const online = cameras.filter(c => c.status === 'online').length;
    const warning = cameras.filter(c => c.status === 'warning').length;
    const offline = cameras.filter(c => c.status === 'offline').length;
    const total = cameras.length;

    return { online, warning, offline, total };
  }, [cameras]);

  return {
    cameras,
    frames,
    isLoading,
    error,
    refreshCameras,
    getCameraCurrentFrame,
    startFrameUpdates,
    stopFrameUpdates,
    discoverNewCameras,
    addNewCamera,
    testCameraConnection,
    removeCameraById,
    getCameraStats
  };
};