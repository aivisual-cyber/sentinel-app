# Fire Detection System - Development Guide

## CRITICAL LEGAL DISCLAIMER
**This system is a SUPPLEMENTARY early warning tool only.**
- Does NOT replace UL-listed fire detection systems
- Must NEVER be used as primary fire detection
- NOT for use in sleeping areas without certified systems
- Always maintain certified fire alarm systems

## Project Overview
Open-source supplementary fire detection using existing RGB cameras to provide early warning alerts. Enhances, not replaces, traditional fire safety systems through automated 24/7 monitoring.

## Core Architecture - Simplified for Reliability

### Single Algorithm Approach
- **YOLOv8 Visual Pattern Recognition** on RGB camera feeds
- **Completely offline operation** - no internet dependency for detection
- **Configurable threshold system** via YAML config files
- **Real-time processing** optimized for standard security cameras

### Key Design Principles
- **Safety over features** - conservative detection thresholds
- **Offline-first** - complete functionality without internet
- **Configurable** - threshold tuning without code changes
- **Reliable** - simple architecture, minimal failure points
- **Supplementary positioning** - always emphasizes additional safety layer

## Technical Stack (Production-Ready)
- **Application**: Tauri (single binary, cross-platform)
- **Detection Engine**: Python 3.10+ with PyTorch + YOLOv8
- **Computer Vision**: OpenCV for camera integration
- **Database**: Embedded SQLite for local storage
- **Configuration**: YAML-based threshold management
- **Platform**: Windows 10/11 Pro or Linux

## Hardware Requirements
- Business-grade desktop PC (16GB RAM, 500GB SSD)
- NVIDIA RTX 3060+ GPU (required for real-time processing)
- 1-2 hour UPS backup power
- Dual network connectivity (ethernet + cellular)
- Existing RGB security cameras (RTSP/ONVIF)

## Detection Logic
```yaml
# Configurable thresholds
immediate_alert: 0.95    # P1 - dispatch immediately  
review_queue: 0.85       # P2 - human verification
log_only: 0.70          # P4 - data collection

# Environmental adjustments
fog_adjustment: -0.05    # Reduce sensitivity in poor visibility
sunset_hours: [17, 19]   # Higher thresholds during challenging light
```

## Development Priorities

### Core Detection System
1. **Camera Integration** - RTSP stream processing with OpenCV
2. **YOLOv8 Implementation** - Fire/smoke pattern recognition
3. **Configuration System** - YAML-based threshold management
4. **Alert Generation** - Local notifications and logging
5. **Basic GUI** - Tauri-based monitoring interface

### Success Criteria
- Process 5-10 camera feeds simultaneously
- <2 second detection latency
- Configurable confidence thresholds
- Local alert generation (SMS, email when network available)
- 24/7 operation with graceful failure handling

## Compliance & Safety

### Standards Adherence
- NFPA 1221 (Emergency Communications) guidelines
- Local fire safety regulations compliance
- Privacy law compliance (data retention limits)
- Equipment certification where required

### Documentation Requirements
- Installation and configuration procedures
- Operator training materials
- Incident reporting templates
- Performance metrics and audit trails

## Development Commands

### Testing
- Run all tests: `python -m pytest tests/`
- Compare models on real images: `python docs/analysis/bench_models.py <pos_root> <neg_dir> <neg_dir2> name=model.pt`
- Performance benchmarks: `python scripts/benchmark.py`
- Configuration validation: `python scripts/validate_config.py`

### Deployment
- Build Tauri application: `cargo tauri build`
- Package for distribution: `cargo tauri bundle`
- System health check: `curl http://127.0.0.1:8765/api/health`

## Current Status - VERIFIED WORKING PIPELINE, NOT YET SITE-VALIDATED

> The feature lists below were written before an end-to-end review (docs/ANALYSIS_AND_PLAN.md) that found the
> detection→alert chain broken; it is now fixed and covered by tests. Treat items not listed as verified in
> README "Current status" (e.g. network failover, sunset/fog compensation, field-test suite) as unimplemented.
> Never claim "production ready" for life safety: run a site trial and measure false alarms first.
>
> Key facts for development: paths resolve from SENTINEL_HOME (backend/paths.py); model + class names +
> thresholds + temporal confirmation live in config/detection_config.yaml; external alerts in
> config/notifications.yaml (secrets via env vars); Tauri commands use snake_case args; run `pytest tests/`.

### Original roadmap status (historical)

### ✅ Phase 1 Features (Foundation)
- **Complete Tauri GUI** - Professional fire safety command center interface
- **YOLOv8 Detection Engine** - Python backend with asyncio processing
- **YAML Configuration System** - Hot-reload threshold management (P1/P2/P4 levels)
- **SQLite Alert Management** - Local logging with notification interface
- **Multi-Camera Simulation** - Test feeds for development/demo
- **Real-time Dashboard** - Live activity feed, system health monitoring
- **Professional UI/UX** - TailwindCSS v3 with fire safety color scheme

### ✅ Phase 2 Features (Production Deployment)
- **RTSP Camera Integration** - Real camera feeds with ONVIF discovery and auto-configuration
- **Network Redundancy** - Automatic ethernet/wifi/cellular failover with monitoring
- **Professional Alert System** - SMS/Email notifications with Twilio integration and retry logic
- **Performance Optimization** - Multi-camera concurrent processing with GPU acceleration
- **Custom Model Training** - Fire-specific YOLOv8 training pipeline with dataset management
- **Production Installers** - Automated Linux/Windows deployment with systemd services
- **Field Testing Suite** - Comprehensive calibration, validation, and threshold optimization
- **Offline Operation** - Complete functionality during network outages with local caching

### 🏗️ Production Architecture
```
sentinel/
├── backend/                    # Production detection engine
│   ├── detection/             # Enhanced fire detection system
│   │   ├── fire_detector.py   # RTSP-integrated YOLOv8 detection
│   │   ├── rtsp_manager.py    # Real camera management & ONVIF discovery
│   │   └── model_trainer.py   # Custom fire model training pipeline
│   ├── config/                # Advanced configuration management
│   │   └── camera_config.py   # Camera discovery, setup & validation
│   ├── alerts/                # Professional alert notification system
│   │   └── notification_system.py # SMS/Email with redundancy & rate limiting
│   └── utils/                 # Production utilities
│       ├── network_monitor.py # Network failover & connectivity monitoring
│       ├── performance_optimizer.py # Multi-camera optimization & GPU acceleration
│       └── video_simulator.py # Development testing framework
├── scripts/                   # Production deployment & testing
│   ├── install.sh            # Professional Linux installer with systemd
│   ├── install.ps1           # Professional Windows installer with services
│   └── field_test_suite.py   # Comprehensive field testing & calibration
├── src/                      # Tauri frontend (Phase 1 - still active)
├── src-tauri/               # Rust application wrapper
└── config/                  # Production configuration templates
```

### 🚀 Production Deployment Commands

**Linux (Recommended):**
```bash
# Download and run production installer
sudo ./scripts/install.sh

# Configure system
sudo nano /etc/sentinel/cameras.yaml
sudo nano /etc/sentinel/alerts.yaml
sudo nano /etc/sentinel/recipients.yaml

# Start production service
sudo systemctl start sentinel-fire-detection
sudo systemctl enable sentinel-fire-detection
```

**Windows:**
```powershell
# Run Windows installer as Administrator
.\scripts\install.ps1

# Service management
Start-Service -Name "SentinelFireDetection"
```

**Development Mode:**
```bash
# Traditional development setup (Phase 1 still supported)
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt
npm install
npm run tauri dev
```

### 🎯 Production Features Completed
- ✅ **Real RTSP Integration** - Live camera feeds replacing simulation
- ✅ **Custom Fire Model Training** - Specialized YOLOv8 with fire-specific datasets
- ✅ **Network Failover** - Automatic switching between network interfaces
- ✅ **Professional Alerts** - SMS/Email with external service integration
- ✅ **Performance Optimization** - Multi-camera concurrent processing (10+ cameras)
- ✅ **Production Installers** - One-command Linux/Windows deployment
- ✅ **Field Testing** - Comprehensive calibration and validation framework
- ✅ **Offline Operation** - Complete functionality without internet dependency

### 📊 Production Performance Metrics
- **Detection Latency**: <2 seconds per frame (target met)
- **Concurrent Cameras**: 10+ simultaneous feeds supported
- **System Uptime**: >99% with automatic recovery
- **Alert Delivery**: <30 seconds SMS/Email with retry logic
- **False Positive Rate**: <2% (configurable thresholds)
- **Network Failover**: <30 seconds automatic switching
- **GPU Acceleration**: 5x faster processing vs CPU-only

### 🏭 Production Deployment Checklist
- ✅ Professional Linux/Windows installers
- ✅ Systemd service integration
- ✅ Automatic camera discovery (ONVIF)
- ✅ SMS/Email alert configuration
- ✅ Network redundancy setup
- ✅ Performance optimization
- ✅ Field testing suite
- ✅ Comprehensive documentation

---

**Remember**: This is a supplementary safety system. Every implementation must emphasize its role as an additional layer of protection, never a replacement for certified fire detection systems.