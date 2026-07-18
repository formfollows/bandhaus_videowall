# Roadmap

## Intelligent Live Switching

### BPM-Aware Camera Automation
- Build a frontend that allows the operator to tap the current BPM.
- Use the detected tempo as the foundation for automated scene timing and camera switching.

### Song Scene Automation
- Define reusable song sections such as:
  - Verse
  - Chorus
  - Solo
  - Bridge
  - Outro Chorus
- Each scene contains its own camera switching logic and timing.

### Manual Override
- Allow manual camera switching at any time.
- After a manual intervention, the automation seamlessly continues from the current song position instead of restarting.

---

## Break Scene Editor

Create a visual editor for intermission scenes.

Features:
- Drag & drop timeline
- Arrange multiple videos
- Control playback order
- Reusable presets for different events

---

## Global Video Library

Create a centralized media management system backed by an S3 bucket.

Features:
- Automatic detection of newly uploaded videos
- Background download to all Bandhaus systems
- Automatic FFmpeg transcoding into optimized playback formats
- Metadata generation and preview thumbnails

---

## AI Content Pipeline

Build an automated pipeline for creating branded visual content.

### Claude Creative Agent
A Claude Desktop agent with full project context:
- Bandhaus branding
- Logos
- Color palette
- Typography
- Layout guidelines
- Artist and event knowledge

### Image Generation
- Generate artwork using the Higgsfield MCP integration.
- Produce images that already follow the Bandhaus design language.

### Canva Integration
- Import generated assets into Canva.
- Convert designs into editable templates using Magic Layers.
- Add presentation-style animations and transitions while keeping the design editable for humans.

The goal is to create a semi-automated workflow that can produce high-quality event visuals within minutes instead of hours.