"""
Configuration Management System
Handles YAML configuration loading, validation, and real-time updates
"""

import yaml
import os
from pathlib import Path
from typing import Dict, Any, Optional
import logging
from dataclasses import dataclass
from datetime import datetime
import tempfile

try:
    from .. import paths
except ImportError:
    import paths

THRESHOLD_NAMES = ('immediate_alert', 'review_queue', 'log_only')

DEFAULT_YAML = open(Path(__file__).resolve().parent.parent.parent / 'config' / 'detection_config.yaml.template').read() \
    if (Path(__file__).resolve().parent.parent.parent / 'config' / 'detection_config.yaml.template').exists() else ''

@dataclass
class DetectionConfig:
    """Detection configuration structure"""
    immediate_alert: float = 0.75
    review_queue: float = 0.55
    log_only: float = 0.35
    fog_adjustment: float = -0.05
    sunset_hours: list = None
    adaptive_enabled: bool = True
    learning_window_days: int = 7
    max_auto_adjustment: float = 0.05
    temporal_window: int = 5
    temporal_min_hits: int = 3
    model_path: str = "rabahdev_best.pt"

@dataclass
class SystemConfig:
    """System configuration structure"""
    max_concurrent_cameras: int = 10
    rtsp_timeout: int = 5
    frame_rate: int = 2
    detection_latency_target: float = 2.0
    log_retention_days: int = 30
    video_retention_hours: int = 48

@dataclass
class AlertConfig:
    """Alert configuration structure"""
    enabled: bool = True
    sms_enabled: bool = False
    email_enabled: bool = False
    desktop_notifications: bool = True
    cooldown_seconds: float = 60.0

class ConfigManager:
    """Manages application configuration with hot-reload capability"""
    
    def __init__(self, config_path: str = None):
        # Always resolve from SENTINEL_HOME, never from the process cwd (the Tauri shell
        # starts the backend in src-tauri/, which used to load a different config file).
        self.config_path = paths.resolve(config_path) if config_path else paths.DETECTION_CONFIG
        
        self.logger = logging.getLogger(__name__)
        self._config_cache = {}
        self._last_modified = 0
        
        self.logger.info(f"Config manager using path: {self.config_path.absolute()}")
        
        # Load initial configuration
        self.reload_config()
    
    def reload_config(self) -> bool:
        """Reload configuration from file if changed"""
        try:
            if not self.config_path.exists():
                self.logger.warning(f"Config file not found: {self.config_path}")
                self._create_default_config()
            
            # Check if file was modified
            current_modified = self.config_path.stat().st_mtime
            if current_modified <= self._last_modified:
                return False  # No changes
            
            # Load configuration
            with open(self.config_path, 'r') as f:
                raw_config = yaml.safe_load(f)
            
            # Validate and structure configuration
            self._config_cache = self._validate_config(raw_config)
            self._last_modified = current_modified
            
            self.logger.info(f"Configuration reloaded from {self.config_path}")
            return True
            
        except Exception as e:
            self.logger.error(f"Failed to reload config: {e}")
            if not self._config_cache:
                # Use defaults if no config loaded yet
                self._config_cache = self._default_config()
            return False
    
    def _validate_config(self, raw_config: dict) -> dict:
        """Validate and structure configuration"""
        config = {}
        
        # Detection configuration
        detection_raw = raw_config.get('detection', {})
        thresholds = detection_raw.get('thresholds', {})
        environmental = detection_raw.get('environmental', {})
        adaptive = detection_raw.get('adaptive', {})
        temporal = detection_raw.get('temporal', {})
        model = detection_raw.get('model', {})
        
        config['detection'] = DetectionConfig(
            immediate_alert=thresholds.get('immediate_alert', 0.95),
            review_queue=thresholds.get('review_queue', 0.85),
            log_only=thresholds.get('log_only', 0.70),
            fog_adjustment=environmental.get('fog_adjustment', -0.05),
            sunset_hours=environmental.get('sunset_hours', [17, 19]),
            adaptive_enabled=adaptive.get('enabled', True),
            learning_window_days=adaptive.get('learning_window_days', 7),
            max_auto_adjustment=adaptive.get('max_auto_adjustment', 0.05),
            temporal_window=int(temporal.get('window', 5)),
            temporal_min_hits=int(temporal.get('min_hits', 3)),
            model_path=str(model.get('path', 'rabahdev_best.pt')),
        )
        
        # Validate thresholds: known names, numeric, inside (0, 1), and ordered
        for key in thresholds:
            if key not in THRESHOLD_NAMES:
                raise ValueError(f"Unknown threshold '{key}'")
        det_config = config['detection']
        for name in THRESHOLD_NAMES:
            v = getattr(det_config, name)
            if not isinstance(v, (int, float)) or not 0.0 < float(v) < 1.0:
                raise ValueError(f"Threshold {name} must be in (0, 1), got {v!r}")
        if not 1 <= det_config.temporal_min_hits <= det_config.temporal_window <= 30:
            raise ValueError("temporal: require 1 <= min_hits <= window <= 30")
        if not (det_config.log_only <= det_config.review_queue <= det_config.immediate_alert):
            raise ValueError("Thresholds must be: log_only <= review_queue <= immediate_alert")
        
        # System configuration  
        system_raw = raw_config.get('system', {})
        cameras_raw = raw_config.get('cameras', {})
        
        config['system'] = SystemConfig(
            max_concurrent_cameras=cameras_raw.get('max_concurrent', 10),
            rtsp_timeout=cameras_raw.get('rtsp_timeout', 5),
            frame_rate=cameras_raw.get('frame_rate', 2),
            detection_latency_target=system_raw.get('detection_latency_target', 2.0),
            log_retention_days=system_raw.get('log_retention_days', 30),
            video_retention_hours=system_raw.get('video_retention_hours', 48)
        )
        
        # Alert configuration
        alerts_raw = raw_config.get('alerts', {})
        config['alerts'] = AlertConfig(
            enabled=alerts_raw.get('enabled', True),
            sms_enabled=alerts_raw.get('sms_enabled', False),
            email_enabled=alerts_raw.get('email_enabled', False),
            desktop_notifications=alerts_raw.get('desktop_notifications', True),
            cooldown_seconds=float(alerts_raw.get('cooldown_seconds', 60)),
        )
        
        return config
    
    def _default_config(self) -> dict:
        """Generate default configuration"""
        return {
            'detection': DetectionConfig(),
            'system': SystemConfig(),
            'alerts': AlertConfig()
        }
    
    def _create_default_config(self):
        """Create default configuration file"""
        os.makedirs(self.config_path.parent, exist_ok=True)
        
        default_yaml = (paths.SENTINEL_HOME / "config" / "detection_config.yaml.template").read_text() \
            if (paths.SENTINEL_HOME / "config" / "detection_config.yaml.template").exists() else DEFAULT_YAML
        
        with open(self.config_path, 'w') as f:
            f.write(default_yaml)
        
        self.logger.info(f"Created default configuration: {self.config_path}")
    
    def get_detection_config(self) -> DetectionConfig:
        """Get detection configuration"""
        self.reload_config()  # Check for updates
        return self._config_cache.get('detection', DetectionConfig())
    
    def get_system_config(self) -> SystemConfig:
        """Get system configuration"""
        self.reload_config()
        return self._config_cache.get('system', SystemConfig())
    
    def get_alert_config(self) -> AlertConfig:
        """Get alert configuration"""
        self.reload_config()
        return self._config_cache.get('alerts', AlertConfig())
    
    def update_threshold(self, threshold_name: str, value: float) -> bool:
        """Update a detection threshold and save to file"""
        if threshold_name not in THRESHOLD_NAMES:
            self.logger.error(f"Rejected unknown threshold name {threshold_name!r}")
            return False
        try:
            value = float(value)
            # Load current config
            with open(self.config_path, 'r') as f:
                config = yaml.safe_load(f) or {}
            
            # Update threshold
            if 'detection' not in config:
                config['detection'] = {}
            if 'thresholds' not in config['detection']:
                config['detection']['thresholds'] = {}
            
            config['detection']['thresholds'][threshold_name] = value
            
            # Validate updated config
            self._validate_config(config)
            
            # Save atomically (a crash mid-write must not corrupt the config)
            self._atomic_write(yaml.safe_dump(config, default_flow_style=False, indent=2, sort_keys=False))
            
            self.logger.info(f"Updated {threshold_name} to {value}")
            return True
            
        except Exception as e:
            self.logger.error(f"Failed to update threshold {threshold_name}: {e}")
            return False
    
    def _atomic_write(self, text: str) -> None:
        fd, tmp = tempfile.mkstemp(dir=str(self.config_path.parent), prefix='.detection_config.')
        with os.fdopen(fd, 'w') as f:
            f.write(text)
        os.replace(tmp, self.config_path)

    def get_config_summary(self) -> dict:
        """Get summary of current configuration for display"""
        detection = self.get_detection_config()
        system = self.get_system_config()
        alerts = self.get_alert_config()
        
        config_data = {
            'detection_thresholds': {
                'immediate_alert': detection.immediate_alert,
                'review_queue': detection.review_queue,
                'log_only': detection.log_only
            },
            'temporal': {'window': detection.temporal_window, 'min_hits': detection.temporal_min_hits},
            'model_path': detection.model_path,
            'system_settings': {
                'max_cameras': system.max_concurrent_cameras,
                'frame_rate': system.frame_rate,
                'latency_target': system.detection_latency_target
            },
            'alert_settings': {
                'enabled': alerts.enabled,
                'desktop_notifications': alerts.desktop_notifications,
                'cooldown_seconds': alerts.cooldown_seconds,
                # Not implemented in this codebase - reported honestly to the UI:
                'sms_implemented': False,
                'email_implemented': False
            },
            'last_updated': datetime.fromtimestamp(self._last_modified).isoformat()
        }
        
        self.logger.debug(f"Config summary: {config_data}")
        return config_data

# Global config manager instance
config_manager = ConfigManager()