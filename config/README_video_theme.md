# Video Theme Configuration

The `video_theme.json` file allows you to customize the visual appearance of your station's video stream, including fonts, visualizers, and layout settings.

## Configuration Structure

### Fonts

Configure different fonts for each text element in the video overlay:

```json
"fonts": {
    "artist": {
        "path": "/app/fonts/Poppins-Bold.ttf",
        "size": 42,
        "color": "white",
        "shadow_color": "0x000000AA",
        "shadow_offset": {"x": 3, "y": 3},
        "box_color": "0x00000088",
        "box_border": 10
    },
    "album": { ... },
    "dj_name": { ... },
    "clock": { ... },
    "facts": { ... }
}
```

**Font Properties:**

- `path`: Path to the font file (mounted from `/app/fonts/`)
- `size`: Font size
- `color`: Text color (name or hex)
- `shadow_color`: Shadow color with transparency
- `shadow_offset`: Shadow position {"x": 3, "y": 3}
- `box_color`: Background box color with transparency
- `box_border`: Box border width

### Visualizer

Choose from 4 different visualizer modes:

```json
"visualizer": {
    "mode": "spectrum",
    "available_modes": ["spectrum", "vectorscope", "nebula", "grid"],
    "spectrum": {
        "display_mode": "combined",
        "slide_mode": "scroll"
    }
}
```

**Available Visualizers:**

- `spectrum`: Audio frequency spectrum analyzer
- `vectorscope`: Lissajous patterns showing stereo phase
- `nebula`: Flowing, cloud-like patterns
- `grid`: Grid-based visualization

**Spectrum Options:**

- `display_mode`: "combined" or "separate" channels
- `slide_mode`: "scroll" or "fullframe" animation

### Layout

Control positioning and spacing:

```json
"layout": {
    "margins": {
        "side": 80,
        "top": 160,
        "bottom": 80
    },
    "spacing": {
        "main": 160,
        "sub": 90
    },
    "max_text_width": 1060
}
```

### Layout

1. Place font files in the `radio-server/fonts/` directory
2. Update the font paths in `video_theme.json`
3. Restart the radio server: `docker compose restart radio-server`

## Examples

### Use Different Visualizer

```json
"visualizer": {
    "mode": "vectorscope"
}
```

### Custom Font Sizes

```json
"fonts": {
    "artist": {
        "path": "/app/fonts/Poppins-Bold.ttf",
        "size": 48
    }
}
```

### Tighter Layout

```json
"layout": {
    "margins": {"side": 60, "top": 120, "bottom": 60},
    "spacing": {"main": 120, "sub": 70}
}
```

## Docker Integration

The fonts directory is automatically mounted in Docker:

- Host: `./radio-server/fonts/`
- Container: `/app/fonts/`

## Configuration Reloading

Changes to the video theme configuration require a restart of the radio server:

```bash
docker compose restart radio-server
```

The system will log the loaded theme configuration on startup.
